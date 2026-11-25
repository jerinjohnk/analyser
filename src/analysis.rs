use chrono::{DateTime, FixedOffset, NaiveDate};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

#[derive(Debug, Clone, Serialize)]
pub struct User {
	pub name: String,
	pub contributions: usize,
	/// Every email this author committed with (after .mailmap), to help spot duplicates.
	pub emails: Vec<String>,
}

impl User {
	pub fn new(name: String) -> User {
		User {
			name,
			contributions: 0,
			emails: vec![],
		}
	}

	pub fn add_email(&mut self, email: &str) {
		let email = email.trim();
		if !email.is_empty() && !self.emails.iter().any(|e| e.eq_ignore_ascii_case(email)) {
			self.emails.push(email.to_string());
		}
	}
}

#[derive(Debug, Clone, Serialize)]
pub struct Change {
	#[serde(rename = "File")]
	pub file: usize,
	#[serde(rename = "Additions")]
	pub additions: usize,
	#[serde(rename = "Deletions")]
	pub deletions: usize,
}

pub fn serialize_date<S>(date: &DateTime<FixedOffset>, serializer: S) -> Result<S::Ok, S::Error>
where
	S: serde::ser::Serializer,
{
	let s = date.to_rfc3339();
	serializer.serialize_str(&s)
}

#[derive(Debug, Serialize)]
pub struct Commit {
	pub id: String,
	#[serde(serialize_with = "serialize_date")]
	pub time: DateTime<FixedOffset>,
	pub user: usize,
	pub files: Vec<Change>,
	pub words: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct Language {
	pub name: String,
	pub bytes: usize,
}

/// Display settings chosen by the user, read by the SVG renderer.
#[derive(Debug, Default, Serialize)]
pub struct Meta {
	/// Project title shown on every visualization.
	pub title: String,
	/// Inclusive date range (YYYY-MM-DD) the visualizations cover.
	pub from: String,
	pub to: String,
}

#[derive(Debug, Serialize)]
pub struct Analysis {
	pub commits: Vec<Commit>,
	#[serde(skip)]
	pub files_map: HashMap<String, usize>,
	pub files: Vec<String>,
	#[serde(skip)]
	pub users_map: HashMap<String, usize>,
	pub users: Vec<User>,
	pub languages: Vec<Language>,
	pub revision: String,
	#[serde(rename = "totalBytes")]
	pub total_bytes: usize,
	pub meta: Meta,
}

impl Analysis {
	pub fn new(revision: &str) -> Analysis {
		Analysis {
			commits: vec![],
			files_map: HashMap::new(),
			files: vec![],
			users_map: HashMap::new(),
			users: vec![],
			languages: vec![],
			revision: revision.to_owned(),
			total_bytes: 0,
			meta: Meta::default(),
		}
	}

	pub fn get_file_index(&mut self, path: &str) -> usize {
		if let Some(&index) = self.files_map.get(path) {
			return index;
		}
		let index = self.files.len();
		self.files.push(path.to_string());
		self.files_map.insert(path.to_string(), index);
		index
	}

	pub fn get_user_index(&mut self, name: &str) -> usize {
		if let Some(&index) = self.users_map.get(name) {
			return index;
		}
		let index = self.users.len();
		self.users.push(User::new(name.to_string()));
		self.users_map.insert(name.to_string(), index);
		index
	}

	/// Returns the dates of the oldest and newest commits, if there are any.
	pub fn date_bounds(&self) -> Option<(NaiveDate, NaiveDate)> {
		self.commits
			.iter()
			.map(|c| c.time.date_naive())
			.fold(None, |bounds, d| {
				Some(match bounds {
					None => (d, d),
					Some((min, max)) => (d.min(min), d.max(max)),
				})
			})
	}

	/// Keeps only commits whose author date falls within `from..=to`
	/// and recounts each user's contributions accordingly.
	pub fn retain_range(&mut self, from: NaiveDate, to: NaiveDate) {
		self.commits.retain(|c| {
			let d = c.time.date_naive();
			d >= from && d <= to
		});
		for user in &mut self.users {
			user.contributions = 0;
		}
		for commit in &self.commits {
			self.users[commit.user].contributions += 1;
		}
		self.meta.from = from.to_string();
		self.meta.to = to.to_string();
	}

	/// Lowercases the words worth showing: at least two characters, at least one
	/// letter, and not a stop word.
	pub fn filter_stop_words<'a, I>(&self, words: I) -> Vec<String>
	where
		I: Iterator<Item = &'a str>,
	{
		words
			.filter(|word| word.len() >= 2 && word.chars().any(char::is_alphabetic))
			.map(str::to_lowercase)
			.filter(|word| !STOP_WORDS.contains(word.as_str()))
			.collect()
	}
}

/// Common English words and commit-message boilerplate left out of the word clouds.
static STOP_WORDS: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
	HashSet::from_iter([
		"i",
		"me",
		"my",
		"myself",
		"we",
		"us",
		"our",
		"ours",
		"ourselves",
		"you",
		"your",
		"yours",
		"yourself",
		"yourselves",
		"he",
		"him",
		"his",
		"himself",
		"she",
		"her",
		"hers",
		"herself",
		"it",
		"its",
		"itself",
		"they",
		"them",
		"their",
		"theirs",
		"themselves",
		"what",
		"which",
		"who",
		"whom",
		"whose",
		"this",
		"that",
		"these",
		"those",
		"am",
		"is",
		"are",
		"was",
		"were",
		"be",
		"been",
		"being",
		"have",
		"has",
		"had",
		"having",
		"do",
		"does",
		"did",
		"doing",
		"will",
		"would",
		"should",
		"can",
		"could",
		"ought",
		"i'm",
		"you're",
		"he's",
		"she's",
		"it's",
		"we're",
		"they're",
		"i've",
		"you've",
		"we've",
		"they've",
		"i'd",
		"you'd",
		"he'd",
		"she'd",
		"we'd",
		"they'd",
		"i'll",
		"you'll",
		"he'll",
		"she'll",
		"we'll",
		"they'll",
		"isn't",
		"aren't",
		"wasn't",
		"weren't",
		"hasn't",
		"haven't",
		"hadn't",
		"doesn't",
		"don't",
		"didn't",
		"won't",
		"wouldn't",
		"shan't",
		"shouldn't",
		"can't",
		"cannot",
		"couldn't",
		"mustn't",
		"let's",
		"that's",
		"who's",
		"what's",
		"here's",
		"there's",
		"when's",
		"where's",
		"why's",
		"how's",
		"a",
		"an",
		"the",
		"and",
		"but",
		"if",
		"or",
		"because",
		"as",
		"until",
		"while",
		"of",
		"at",
		"by",
		"for",
		"with",
		"about",
		"against",
		"between",
		"into",
		"through",
		"during",
		"before",
		"after",
		"above",
		"below",
		"to",
		"from",
		"up",
		"upon",
		"down",
		"in",
		"out",
		"on",
		"off",
		"over",
		"under",
		"again",
		"further",
		"then",
		"once",
		"here",
		"there",
		"when",
		"where",
		"why",
		"how",
		"all",
		"any",
		"both",
		"each",
		"few",
		"more",
		"most",
		"other",
		"some",
		"such",
		"no",
		"nor",
		"not",
		"only",
		"own",
		"same",
		"so",
		"than",
		"too",
		"very",
		"say",
		"says",
		"said",
		"shall",
		"add",
		"fix",
		"remove",
		"change",
		"improve",
		"use",
		"test",
		"tests",
		"update",
		"move",
		"support",
		"implement",
		"readme",
		"fixes",
		"fixed",
		"make",
		"warning",
		"error",
		"pull",
		"merge",
		"merged",
		"request",
		"generate",
		"group",
		"panic",
		"doc",
		"chore",
		"issue",
		"increase",
		"version",
		"new",
		"master",
		"branch",
		"bug",
		"release",
		"bump",
		"commit",
		"refactor",
		"prepare",
		"github.com",
		"added",
		"git",
		"patch",
		"patches",
		"patched",
		"need",
		"needed",
		"roll",
		"back",
		"instead",
		"whos",
		"ups",
		"downs",
		"ins",
		"outs",
		"ons",
		"offs",
		"overs",
		"unders",
		"wheres",
		"others",
		"owns",
		"adds",
		"removes",
		"changes",
		"improves",
		"uses",
		"updates",
		"moves",
		"supports",
		"implements",
		"readmes",
		"makes",
		"warnings",
		"errors",
		"pulls",
		"merges",
		"requests",
		"generates",
		"groups",
		"panics",
		"docs",
		"chores",
		"issues",
		"increases",
		"versions",
		"masters",
		"bugs",
		"releases",
		"bumps",
		"commits",
		"refactors",
		"prepares",
		"news",
		"needs",
		"rolls",
	])
});
