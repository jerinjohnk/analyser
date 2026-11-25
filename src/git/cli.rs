use anyhow::{Context, Result};
use chrono::DateTime;
use indicatif::ProgressBar;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::thread;

use crate::analysis::{Analysis, Change, Commit};
use crate::errors::AnalysisError;
use crate::git::{Analyser, parse_stat_line};

fn git_error(message: impl Into<String>) -> anyhow::Error {
	AnalysisError::Git {
		message: message.into(),
	}
	.into()
}

/// Runs git and returns its trimmed stdout, or its stderr as the error.
fn call_git(args: &[&str]) -> Result<String> {
	let output = Command::new("git")
		.args(args)
		.stdin(Stdio::null())
		.output()
		.context("running git (is it installed and on your PATH?)")?;

	if output.status.success() {
		Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
	} else {
		Err(git_error(String::from_utf8_lossy(&output.stderr).trim()))
	}
}

/// Starts git with its stdout piped back to us; stderr goes straight to the terminal.
fn spawn_git(args: &[&str], stdin: Stdio) -> Result<Child> {
	Command::new("git")
		.args(args)
		.stdin(stdin)
		.stdout(Stdio::piped())
		.spawn()
		.context("running git (is it installed and on your PATH?)")
}

/// Waits for a streamed git process and turns a non-zero exit into an error.
fn finish(mut child: Child, what: &str) -> Result<()> {
	let status = child.wait()?;
	if status.success() {
		Ok(())
	} else {
		Err(git_error(format!("{what} exited with {status}")))
	}
}

/// Maps each non-merge commit hash (all refs) to its stable patch id. Commits with no
/// diff (e.g. empty commits) have no patch id and are left out.
fn patch_ids() -> Result<HashMap<String, String>> {
	let mut log = spawn_git(
		&[
			"log",
			"--all",
			"--no-merges",
			"-p",
			"--no-renames",
			"--format=commit %H",
		],
		Stdio::null(),
	)?;
	let log_out = log.stdout.take().expect("stdout is piped");
	let mut patch_id = spawn_git(&["patch-id", "--stable"], Stdio::from(log_out))?;

	// patch-id's output is small (one line per commit) but read it on its own thread
	// anyway, so neither process can block on a full pipe while we wait.
	let mut out = patch_id.stdout.take().expect("stdout is piped");
	let reader = thread::spawn(move || {
		let mut buf = String::new();
		out.read_to_string(&mut buf).map(|_| buf)
	});
	finish(log, "git log")?;
	finish(patch_id, "git patch-id")?;
	let output = reader
		.join()
		.map_err(|_| git_error("reading git patch-id output failed"))??;

	Ok(output
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
	let head_list = call_git(&["rev-list", "--no-merges", "HEAD"])?;
	let on_head: HashSet<&str> = head_list.split_whitespace().collect();

	// (patch id, user, author time, offset) -> index of the commit kept
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
	let keys: Vec<_> = data.commits.iter().map(key_of).collect();
	let mut keep: HashMap<_, usize> = HashMap::with_capacity(keys.len());
	for (i, key) in keys.iter().enumerate() {
		let Some(key) = key else { continue };
		// Log order is newest first, so a later index is the older copy.
		let replace = match keep.get(key) {
			None => true,
			Some(&j) => {
				on_head.contains(data.commits[i].id.as_str())
					|| !on_head.contains(data.commits[j].id.as_str())
			}
		};
		if replace {
			keep.insert(*key, i);
		}
	}

	let drop: Vec<bool> = keys
		.iter()
		.enumerate()
		.map(|(i, key)| key.is_some_and(|key| keep[&key] != i))
		.collect();
	for (c, _) in data.commits.iter().zip(&drop).filter(|(_, d)| **d) {
		data.users[c.user].contributions -= 1;
	}
	let mut dropped = drop.into_iter();
	data.commits.retain(|_| !dropped.next().unwrap_or(false));

	Ok(())
}

pub struct CliAnalyser {}

impl CliAnalyser {
	pub fn new() -> Result<CliAnalyser> {
		call_git(&["--version"])?;
		Ok(CliAnalyser {})
	}
}

impl Analyser for CliAnalyser {
	fn get_path(&self) -> Result<std::path::PathBuf> {
		Ok(call_git(&["rev-parse", "--show-toplevel"])?.into())
	}

	fn get_commit_count(&self) -> Result<u64> {
		Ok(call_git(&["rev-list", "--count", "--all", "--no-merges"])?.parse()?)
	}

	fn get_current_revision(&self) -> Result<String> {
		call_git(&["rev-parse", "HEAD"])
	}

	fn analyse(&self, data: &mut Analysis, pb: &ProgressBar) -> Result<()> {
		let mut child = spawn_git(
			&[
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
			],
			Stdio::null(),
		)?;
		let mut reader = BufReader::new(child.stdout.take().expect("stdout is piped"));

		let mut commit: Option<Commit> = None;
		let mut buf = Vec::new();
		loop {
			buf.clear();
			if reader.read_until(b'\n', &mut buf)? == 0 {
				break;
			}
			if buf.last() == Some(&b'\n') {
				buf.pop();
			}
			// Paths aren't always valid UTF-8; keep the line rather than lose the commit.
			let line = String::from_utf8_lossy(&buf);

			if line.len() < 2 {
				if let Some(commit) = commit.take() {
					data.commits.push(commit);
				}
				continue;
			}

			if let Some(marker) = line.strip_prefix('#') {
				pb.inc(1);

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
				let user = &mut data.users[user_index];
				user.contributions += 1;
				user.add_email(author_email);

				commit = Some(Commit {
					id: id.to_string(),
					time,
					user: user_index,
					files: vec![],
					words,
				});
			} else if let Some(commit) = commit.as_mut() {
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
		finish(child, "git log")?;

		drop_rebased_copies(data)
	}
}
