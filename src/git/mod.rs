use anyhow::Result;
use indicatif::ProgressBar;

#[cfg(not(feature = "git-libgit2"))]
mod cli;
#[cfg(feature = "git-libgit2")]
mod git2;

use crate::analysis::Analysis;
use crate::errors::AnalysisError;
#[cfg(not(feature = "git-libgit2"))]
use crate::git::cli::CliAnalyser as ConcreteAnalyser;
#[cfg(feature = "git-libgit2")]
use crate::git::git2::Git2Analyser as ConcreteAnalyser;

pub trait Analyser {
	fn get_path(&self) -> Result<std::path::PathBuf>;
	fn get_commit_count(&self) -> Result<u64>;
	fn get_current_revision(&self) -> Result<String>;
	fn analyse(&self, data: &mut Analysis, pb: &ProgressBar) -> Result<()>;
}

pub fn get_analyser() -> Result<Box<dyn Analyser>> {
	Ok(Box::new(ConcreteAnalyser::new()?))
}

/// Parses a numstat count; binary files show "-" and count as no change.
pub fn parse_num_changes(s: &str) -> usize {
	s.trim().parse::<usize>().unwrap_or(0)
}

fn malformed(line: &str) -> anyhow::Error {
	AnalysisError::Git {
		message: format!("Malformed stat line: {line:?}"),
	}
	.into()
}

/// Parses one stat line into (path, additions, deletions). With `hard_tabs` the line is
/// `git log --numstat` output ("adds\tdels\tpath"), otherwise libgit2's fixed-width
/// format (8 columns for additions, 8 for deletions, then the path).
pub fn parse_stat_line(line: &str, hard_tabs: bool) -> Result<(&str, usize, usize)> {
	let (path, additions_str, deletions_str) = if hard_tabs {
		let mut parts = line.splitn(3, '\t');
		match (parts.next(), parts.next(), parts.next()) {
			(Some(additions), Some(deletions), Some(path)) => (path, additions, deletions),
			_ => return Err(malformed(line)),
		}
	} else {
		if line.len() < 16 || !line.is_char_boundary(16) {
			return Err(malformed(line));
		}
		let (additions_str, rest) = line.split_at(8);
		let (deletions_str, path) = rest.split_at(8);
		(path, additions_str, deletions_str)
	};

	Ok((
		path,
		parse_num_changes(additions_str),
		parse_num_changes(deletions_str),
	))
}
