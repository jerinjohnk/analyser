use anyhow::Result;
use chrono::DateTime;
use log::warn;
use pbr::ProgressBar;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use subprocess::{Exec, Redirection};

use crate::analysis::{Analysis, Change, Commit};
use crate::errors::AnalysisError;
use crate::git::{parse_stat_line, Analyser};

fn call_git(args: Vec<&'static str>) -> Result<String> {
	let mut command = Exec::cmd("git");
	for arg in &args {
		command = command.arg(arg);
	}

	let capture = command
		.stdout(Redirection::Pipe)
		.stderr(Redirection::Pipe)
		.capture()?;

	if capture.success() {
		Ok(capture.stdout_str().trim().to_string())
	} else {
		Err(AnalysisError::Git {
			message: capture.stderr_str().trim().to_string(),
		}
		.into())
	}
}

fn call_git_stream(args: Vec<&'static str>) -> Result<Box<dyn std::io::Read>> {
	let mut command = Exec::cmd("git");
	for arg in &args {
		command = command.arg(arg);
	}

	Ok(Box::new(command.stream_stdout()?))
}

/// Maps each non-merge commit hash (all refs) to its stable patch id. Commits with no
/// diff (e.g. empty commits) have no patch id and are left out.
fn patch_ids() -> Result<HashMap<String, String>> {
	let log = Exec::cmd("git").args(&[
		"log",
		"--all",
		"--no-merges",
		"-p",
		"--no-renames",
		"--format=commit %H",
	]);
	let capture = (log | Exec::cmd("git").args(&["patch-id", "--stable"]))
		.stdout(Redirection::Pipe)
		.capture()?;
	if !capture.success() {
		return Err(AnalysisError::Git {
			message: "git patch-id failed".to_string(),
		}
		.into());
	}

	Ok(capture
		.stdout_str()
		.lines()
		.filter_map(|line| line.split_once(' '))
		.map(|(pid, sha)| (sha.to_string(), pid.to_string()))
		.collect())
}

/// Removes copies of the same commit made by rebase or cherry-pick (they live on other
/// refs under new hashes, e.g. a feature branch left behind after "rebase and merge").
/// Copies share the patch, the author and the author date; of each set the one reachable
/// from HEAD is kept, otherwise the oldest in log order.
fn drop_rebased_copies(data: &mut Analysis) -> Result<()> {
	let ids = patch_ids()?;
	let head_list = call_git(vec!["rev-list", "--no-merges", "HEAD"])?;
	let on_head: HashSet<&str> = head_list.split_whitespace().collect();

	// (patch id, user, author time) -> index of the commit kept
	let mut keep: HashMap<(&str, usize, i64, i32), usize> = HashMap::new();
	let key_of = |c: &Commit| {
		ids.get(&c.id).map(|pid| {
			(
				pid.as_str(),
				c.user,
				c.time.timestamp(),
				c.time.offset().local_minus_utc(),
			)
		})
	};
	for (i, c) in data.commits.iter().enumerate() {
		if let Some(key) = key_of(c) {
			// Log order is newest first, so a later index is the older copy.
			let replace = match keep.get(&key) {
				None => true,
				Some(&j) => on_head.contains(c.id.as_str()) || !on_head.contains(data.commits[j].id.as_str()),
			};
			if replace {
				keep.insert(key, i);
			}
		}
	}

	let drop: Vec<bool> = data
		.commits
		.iter()
		.enumerate()
		.map(|(i, c)| key_of(c).map_or(false, |key| keep[&key] != i))
		.collect();
	for (i, c) in data.commits.iter().enumerate() {
		if drop[i] {
			data.users[c.user].contributions -= 1;
		}
	}
	let mut i = 0;
	data.commits.retain(|_| {
		i += 1;
		!drop[i - 1]
	});

	Ok(())
}

struct LossyLineReader {
	br: BufReader<Box<dyn std::io::Read>>,
}

impl LossyLineReader {
	pub fn new(stream: Box<dyn std::io::Read>) -> Self {
		LossyLineReader {
			br: BufReader::new(stream),
		}
	}
}

impl Iterator for LossyLineReader {
	type Item = String;

	fn next(&mut self) -> Option<String> {
		let mut buf = vec![];
		if let Ok(len) = self.br.read_until(b'\n', &mut buf) {
			return match len {
				0 => None,
				1 => Some("".to_string()),
				_ => match std::str::from_utf8(&buf[0..len - 1]) {
					Ok(line) => Some(line.to_string()),
					Err(e) => {
						warn!("Invalid UTF-8: {:?}", e);
						Some("".to_string())
					}
				},
			};
		}

		None
	}
}

pub struct CliAnalyser {}

impl CliAnalyser {
	pub fn new() -> Result<CliAnalyser> {
		call_git(vec!["--version"])?;
		Ok(CliAnalyser {})
	}
}

impl Analyser for CliAnalyser {
	fn get_path(&self) -> Result<std::path::PathBuf> {
		let out = call_git(vec!["rev-parse", "--show-toplevel"])?;
		Ok(std::path::PathBuf::from(out.trim_end()))
	}

	fn get_commit_count(&self) -> Result<u64> {
		let count_str = call_git(vec!["rev-list", "--count", "--all", "--no-merges"])?;

		Ok(count_str.parse::<u64>()?)
	}

	fn get_current_revision(&self) -> Result<String> {
		let rev = call_git(vec!["rev-parse", "HEAD"])?;

		Ok(rev)
	}

	fn analyse(&self, data: &mut Analysis, pb: &mut ProgressBar<std::io::Stdout>) -> Result<()> {
		let log_stream = call_git_stream(vec![
			"log",
			"--all",
			"--numstat",
			"--date=rfc2822",
			"--use-mailmap",
			"--pretty=format:#%H#%ad#%aE#%aN#%f",
			"--no-renames",
			"--no-expand-tabs",
			"--no-merges",
			"--encoding=UTF-8",
		])?;

		let log_reader = LossyLineReader::new(log_stream);

		let mut commit = None;
		for line in log_reader {
			if line.len() < 2 {
				if let Some(commit) = commit.take() {
					data.commits.push(commit);
				}
				continue;
			}

			if let Some(marker) = line.strip_prefix('#') {
				pb.inc();

				// Commits without file changes (e.g. empty commits) have no numstat block,
				// so the next header can arrive with no blank line in between.
				if let Some(previous) = commit.take() {
					data.commits.push(previous);
				}

				let mut parts = marker.splitn(4, '#');

				let id = parts.next().ok_or(AnalysisError::Parse {
					message: "Missing commit hash",
				})?;

				let time_str = parts.next().ok_or(AnalysisError::Parse {
					message: "Missing time",
				})?;

				let time = DateTime::parse_from_rfc2822(time_str)?;

				let author_email = parts.next().unwrap_or("");

				// %f (the subject slug) never contains '#', so split it off from the right:
				// that keeps author names containing '#' intact.
				let rest = parts.next().unwrap_or("Unknown");
				let (author_name, words) = match rest.rsplit_once('#') {
					Some((name, words)) => (name, data.filter_stop_words(words.split('-'))),
					None => (rest, vec![]),
				};

				let user_index = data.get_user_index(author_name);
				data.users[user_index].contributions += 1;
				data.users[user_index].add_email(author_email);

				commit = Some(Commit {
					id: id.to_string(),
					time,
					user: user_index,
					files: vec![],
					words,
				});
			} else if let Some(ref mut commit) = commit {
				let (path, additions, deletions) = parse_stat_line(&line, true)?;
				commit.files.push(Change {
					file: data.get_file_index(path),
					additions,
					deletions,
				});
			}
		}

		if let Some(commit) = commit.take() {
			data.commits.push(commit);
		}

		drop_rebased_copies(data)

	}
}
