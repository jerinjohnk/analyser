mod analysis;
mod errors;
mod git;
mod languages;

use anyhow::{Context, Result};
use chrono::NaiveDate;
use clap::Parser;
use dialoguer::Input;
use pbr::ProgressBar;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::analysis::{Analysis, Language};
use crate::errors::UserError;
use crate::git::get_analyser as get_git_analyser;
use crate::languages::{get_language_index, LANG_NAMES};

const NUM_STEPS: u8 = 4;
const DATE_FORMAT: &str = "%Y-%m-%d";

/// Turn a local Git repository's history into poster-style SVG visualizations.
///
/// Everything runs locally: the repository is read with git, the analysis is
/// written to a JSON file and the SVGs are rendered by the bundled Python renderer.
/// Any option left out is asked for interactively.
#[derive(Parser, Debug)]
#[command(name = "commitscape", version, about, long_about = None)]
struct Args {
	/// Path to the local Git repository to visualize.
	#[arg(short, long)]
	target: Option<String>,

	/// First day to include (YYYY-MM-DD). Defaults to the first commit.
	#[arg(long)]
	from: Option<String>,

	/// Last day to include (YYYY-MM-DD). Defaults to the latest commit.
	#[arg(long)]
	to: Option<String>,

	/// Project title shown on the visualizations.
	#[arg(long)]
	title: Option<String>,

	/// Output directory for analysis.json and the SVGs.
	/// Defaults to ./commitscape-output/<title>.
	#[arg(short, long)]
	out: Option<PathBuf>,

	/// Accept the default for every question that wasn't given as an option.
	#[arg(short = 'y', long)]
	yes: bool,

	/// Only write analysis.json, don't render SVGs.
	#[arg(long)]
	no_render: bool,

	/// Comma-separated designs to render (default: all).
	/// Available: week-circles, user-bubbles, trunk, cross, core.
	#[arg(long)]
	designs: Option<String>,

	/// Keep commits by bots such as dependabot.
	#[arg(long)]
	include_bots: bool,
}

/// Asks a question with a default answer, unless `--yes` was given.
fn ask(args: &Args, prompt: &str, default: String) -> Result<String> {
	if args.yes {
		return Ok(default);
	}
	let answer: String = Input::new()
		.with_prompt(prompt)
		.default(default.clone())
		.interact_text()?;
	let answer = answer.trim().to_string();
	Ok(if answer.is_empty() { default } else { answer })
}

/// Asks for a date, re-asking until the answer is a valid YYYY-MM-DD.
fn ask_date(args: &Args, prompt: &str, default: NaiveDate) -> Result<NaiveDate> {
	let default = default.format(DATE_FORMAT).to_string();
	if args.yes {
		return parse_date(&default);
	}
	let answer: String = Input::new()
		.with_prompt(prompt)
		.default(default)
		.validate_with(|input: &String| -> Result<(), String> {
			NaiveDate::parse_from_str(input.trim(), DATE_FORMAT)
				.map(|_| ())
				.map_err(|_| format!("{:?} isn't a valid date, use YYYY-MM-DD", input.trim()))
		})
		.interact_text()?;
	parse_date(&answer)
}

fn parse_date(value: &str) -> Result<NaiveDate> {
	NaiveDate::parse_from_str(value.trim(), DATE_FORMAT).map_err(|_| {
		UserError::InvalidDate {
			value: value.to_string(),
		}
		.into()
	})
}

fn expand_home(path: &str) -> PathBuf {
	match (path.strip_prefix("~/"), std::env::var_os("HOME")) {
		(Some(rest), Some(home)) => PathBuf::from(home).join(rest),
		_ => PathBuf::from(path),
	}
}

fn slugify(title: &str) -> String {
	let slug: String = title
		.chars()
		.map(|c| if c.is_alphanumeric() { c.to_ascii_lowercase() } else { '-' })
		.collect();
	let slug = slug
		.split('-')
		.filter(|s| !s.is_empty())
		.collect::<Vec<_>>()
		.join("-");
	if slug.is_empty() {
		"project".to_string()
	} else {
		slug
	}
}

fn step(n: u8, message: &str) {
	println!("[{}/{}]\t{}", n, NUM_STEPS, message);
}

/// Designs the renderer knows; keep in sync with RENDERERS in commitscape/render.py.
const DESIGNS: [&str; 5] = ["week-circles", "user-bubbles", "trunk", "cross", "core"];

fn run() -> Result<()> {
	let args = Args::parse();
	let launch_dir = std::env::current_dir()?;

	// Reject unknown design names before spending time on the analysis.
	if let Some(designs) = &args.designs {
		let unknown: Vec<&str> = designs
			.split(',')
			.map(str::trim)
			.filter(|d| !d.is_empty() && !DESIGNS.contains(d))
			.collect();
		if !unknown.is_empty() {
			anyhow::bail!(
				"unknown design(s): {}. Available: {}",
				unknown.join(", "),
				DESIGNS.join(", ")
			);
		}
	}

	// 1. Which repository?
	let target = match &args.target {
		Some(t) => t.clone(),
		None => ask(&args, "Path to the local Git repository", ".".to_string())?,
	};
	std::env::set_current_dir(expand_home(&target)).map_err(|e| UserError::InvalidTarget {
		message: format!("{}: {}", target, e),
	})?;
	let git = get_git_analyser()?;
	let repo_root = git.get_path()?;
	std::env::set_current_dir(&repo_root)?;

	// 2. Read the full history once.
	step(1, &format!("Reading commits from {}...", repo_root.display()));
	let num_commits = git.get_commit_count()?;
	let mut pb = ProgressBar::new(num_commits);
	let rev = git.get_current_revision()?;
	let mut analysis = Analysis::new(&rev);
	git.analyse(&mut analysis, &mut pb)?;
	pb.finish_println("");

	let (first, last) = analysis.date_bounds().ok_or(UserError::EmptyRepo {})?;
	println!(
		"\tFound {} commits from {} to {}.",
		analysis.commits.len(),
		first.format(DATE_FORMAT),
		last.format(DATE_FORMAT)
	);

	// 3. Date range and title.
	step(2, "Choose what to visualize");
	let from = match &args.from {
		Some(v) => parse_date(v)?,
		None => ask_date(&args, "Start date (YYYY-MM-DD)", first)?,
	};
	let to = match &args.to {
		Some(v) => parse_date(v)?,
		None => ask_date(&args, "End date (YYYY-MM-DD)", last)?,
	};
	if from > to {
		return Err(UserError::InvalidRange {
			from: from.to_string(),
			to: to.to_string(),
		}
		.into());
	}
	analysis.retain_range(from, to);
	if analysis.commits.is_empty() {
		return Err(UserError::NoCommitsInRange {
			from: from.to_string(),
			to: to.to_string(),
		}
		.into());
	}

	let default_title = repo_root
		.file_name()
		.map(|n| n.to_string_lossy().into_owned())
		.unwrap_or_else(|| "project".to_string());
	analysis.meta.title = match &args.title {
		Some(t) => t.clone(),
		None => ask(&args, "Project title for the visualizations", default_title)?,
	};

	// 4. Language stats from the current checkout.
	add_languages(&mut analysis);

	// 5. Write the analysis.
	let out_dir = match &args.out {
		Some(p) => launch_dir.join(p),
		None => launch_dir
			.join("commitscape-output")
			.join(slugify(&analysis.meta.title)),
	};
	std::fs::create_dir_all(&out_dir)
		.with_context(|| format!("creating output directory {}", out_dir.display()))?;
	let json_path = out_dir.join("analysis.json");
	std::fs::write(&json_path, serde_json::to_vec(&analysis)?)?;
	step(
		3,
		&format!(
			"Analysed {} commits between {} and {} -> {}",
			analysis.commits.len(),
			analysis.meta.from,
			analysis.meta.to,
			json_path.display()
		),
	);

	// 6. Render.
	if args.no_render {
		step(4, "Skipping rendering (--no-render).");
		return Ok(());
	}
	step(4, "Rendering SVGs...");
	render(&args, &json_path, &out_dir)?;
	println!("\nDone. Open {} to see them all.", out_dir.join("index.html").display());
	Ok(())
}

/// Sums file sizes per language, based on file extensions of every file in the history
/// that still exists in the working tree.
fn add_languages(analysis: &mut Analysis) {
	let language_index = get_language_index();
	let mut languages: HashMap<usize, usize> = HashMap::new();
	let mut total_bytes: u64 = 0;
	for path_str in &analysis.files {
		let path = Path::new(&path_str);
		if let Ok(metadata) = std::fs::metadata(path) {
			let len = metadata.len();
			total_bytes += len;
			if let Some(ext) = path.extension() {
				let ext_str = ext.to_string_lossy().into_owned();
				if let Some(i) = language_index.get::<str>(&ext_str) {
					*languages.entry(*i).or_insert(0) += len as usize;
				}
			}
		}
	}
	analysis.total_bytes = total_bytes as usize;
	for (language_index, bytes) in languages {
		analysis.languages.push(Language {
			name: LANG_NAMES[language_index].to_string(),
			bytes,
		});
	}
}

/// Finds the directory containing the `commitscape` Python package:
/// $COMMITSCAPE_HOME, then next to the executable's parents, then the source checkout.
fn renderer_home() -> Option<PathBuf> {
	let has_pkg = |p: &Path| p.join("commitscape").join("render.py").is_file();
	if let Some(home) = std::env::var_os("COMMITSCAPE_HOME") {
		let p = PathBuf::from(home);
		if has_pkg(&p) {
			return Some(p);
		}
	}
	if let Ok(exe) = std::env::current_exe() {
		if let Some(found) = exe.ancestors().skip(1).find(|p| has_pkg(p)) {
			return Some(found.to_path_buf());
		}
	}
	let src = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
	if has_pkg(&src) {
		return Some(src);
	}
	None
}

fn render(args: &Args, json_path: &Path, out_dir: &Path) -> Result<()> {
	let home = renderer_home().context(
		"couldn't find the commitscape renderer; set COMMITSCAPE_HOME to the project directory",
	)?;
	let python = std::env::var("PYTHON").unwrap_or_else(|_| "python3".to_string());
	let mut cmd = Command::new(&python);
	cmd.current_dir(&home)
		.arg("-m")
		.arg("commitscape.render")
		.arg(json_path)
		.arg("--out")
		.arg(out_dir);
	if let Some(designs) = &args.designs {
		cmd.arg("--designs").arg(designs);
	}
	if args.include_bots {
		cmd.arg("--include-bots");
	}
	let status = cmd
		.status()
		.with_context(|| format!("running {} (Python 3.9+ is required to render)", python))?;
	if !status.success() {
		anyhow::bail!("the renderer exited with {}", status);
	}
	Ok(())
}

fn main() {
	let mut log_builder = env_logger::Builder::from_default_env();
	log_builder.target(env_logger::Target::Stderr);
	log_builder.init();

	if let Err(e) = run() {
		if let Some(ue) = e.downcast_ref::<UserError>() {
			eprintln!("Error: {}", ue);
		} else {
			eprintln!("Error: {:#}", e);
		}
		std::process::exit(1);
	}
}
