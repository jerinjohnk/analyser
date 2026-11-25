use ::git2::{DiffOptions, Repository};
use anyhow::Result;
use chrono::{FixedOffset, TimeZone};
use indicatif::ProgressBar;

use crate::analysis::{Analysis, Change, Commit};
use crate::errors::AnalysisError;
use crate::git::{Analyser, parse_stat_line};

pub struct Git2Analyser {
	repo: Repository,
}

impl Git2Analyser {
	pub fn new() -> Result<Git2Analyser> {
		let repo = Repository::discover(".").map_err(|e| AnalysisError::Git {
			message: format!("Can't find Git repository: {e}"),
		})?;

		Ok(Git2Analyser { repo })
	}
}

impl Analyser for Git2Analyser {
	fn get_path(&self) -> Result<std::path::PathBuf> {
		let path = self.repo.workdir().ok_or_else(|| AnalysisError::Git {
			message: "Repository is bare (has no working directory)".to_owned(),
		})?;
		Ok(path.to_path_buf())
	}

	fn get_commit_count(&self) -> Result<u64> {
		// Walk the same commits as `analyse` and leave out merge commits, so the
		// count matches what is actually attributed to authors.
		let mut revwalk = self.repo.revwalk()?;
		revwalk.set_sorting(git2::Sort::NONE)?;
		revwalk.push_head()?;
		let count = revwalk
			.filter_map(|id| id.ok())
			.filter_map(|id| self.repo.find_commit(id).ok())
			.filter(|commit| commit.parent_count() <= 1)
			.count();
		Ok(count as u64)
	}

	fn get_current_revision(&self) -> Result<String> {
		let rev = self.repo.revparse_single("HEAD")?;

		Ok(rev.id().to_string())
	}

	fn analyse(&self, data: &mut Analysis, pb: &ProgressBar) -> Result<()> {
		let mut diffopts = DiffOptions::new();

		let mut revwalk = self.repo.revwalk()?;
		revwalk.push_head()?;

		for id in revwalk {
			let Ok(id) = id else { continue };
			let Ok(commit) = self.repo.find_commit(id) else {
				continue;
			};
			if commit.parent_count() > 1 {
				// merge commit
				continue;
			}

			let sig = commit.author();
			let user_index = data.get_user_index(&String::from_utf8_lossy(sig.name_bytes()));
			let user = &mut data.users[user_index];
			user.contributions += 1;
			user.add_email(&String::from_utf8_lossy(sig.email_bytes()));

			let git_time = sig.when();
			let time = FixedOffset::east_opt(git_time.offset_minutes() * 60)
				.and_then(|tz| tz.timestamp_opt(git_time.seconds(), 0).single())
				.ok_or(AnalysisError::Parse {
					message: "Invalid commit time",
				})?;

			let a = match commit.parent_count() {
				1 => Some(commit.parent(0)?.tree()?),
				_ => None,
			};
			let b = commit.tree()?;

			let diff = self
				.repo
				.diff_tree_to_tree(a.as_ref(), Some(&b), Some(&mut diffopts))?;

			// Split the message into words (letters, digits and underscores).
			let message = String::from_utf8_lossy(commit.message_raw_bytes());
			let words =
				data.filter_stop_words(message.split(|c: char| !(c.is_alphanumeric() || c == '_')));

			let mut c = Commit {
				id: id.to_string(),
				time,
				user: user_index,
				files: vec![],
				words,
			};

			let stats = diff.stats()?;
			let buf = stats.to_buf(git2::DiffStatsFormat::NUMBER, 0)?;
			for line in std::str::from_utf8(&buf)?.lines() {
				let (path, additions, deletions) = parse_stat_line(line, false)?;
				c.files.push(Change {
					file: data.get_file_index(path),
					additions,
					deletions,
				});
			}

			data.commits.push(c);
			pb.inc(1);
		}

		Ok(())
	}
}
