use thiserror::Error;

#[derive(Error, Debug)]
pub enum AnalysisError {
	#[error("Git client error ({message})")]
	Git { message: String },
	#[error("Parse error ({message})")]
	Parse { message: &'static str },
}

#[derive(Error, Debug)]
pub enum UserError {
	#[error("Invalid target directory ({message})")]
	InvalidTarget { message: String },
	#[error("This repository has no commits to visualize")]
	EmptyRepo,
	#[error("Invalid date {value:?}, expected YYYY-MM-DD")]
	InvalidDate { value: String },
	#[error("The start date {from} is after the end date {to}")]
	InvalidRange { from: String, to: String },
	#[error(
		"No commits found between {from} and {to} (this repository has commits from {first} to {last}; use --from/--to)"
	)]
	NoCommitsInRange {
		from: String,
		to: String,
		first: String,
		last: String,
	},
}
