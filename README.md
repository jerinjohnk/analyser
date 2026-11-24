# Commitscape

Turn a Git repository's history into poster-style SVG visualizations, **entirely offline**.
Pick a repository and a date range, tidy up who's who (people who committed under several names),
and Commitscape renders five designs. There are no accounts, uploads or server calls: the web
interface is served from your own machine and only listens on `127.0.0.1`.

## Requirements

| Tool | Needed for | Check | Install (macOS / Debian-Ubuntu) |
|---|---|---|---|
| `git` | reading the repository | `git --version` | `xcode-select --install` / `sudo apt install git` |
| Python 3.9+ | the web interface and the SVGs (standard library only, no `pip install`) | `python3 --version` | `brew install python` / `sudo apt install python3` |
| Rust (optional) | the faster native terminal version | `cargo --version` | `brew install rust` / [rustup.rs](https://rustup.rs) |

## Quick start

```sh
git clone <this-repository-url> commitscape
cd commitscape
./run.sh
```

This starts Commitscape at **http://127.0.0.1:8765** and opens it in your browser
(Ctrl+C in the terminal stops it). Then:

1. **Repository**: press **Browse…** to pick the project folder in the system folder dialog (Finder on
   macOS; zenity/kdialog on Linux; the folder dialog on Windows; an in-page folder browser elsewhere),
   or type its path (e.g. `~/code/my-app`) and press **Load**.
2. **Date range**: the default is chosen for you (see [below](#default-date-range)); pick another preset
   (This year so far, Last year, Last 12 months, All time) or set custom dates. The page shows how many
   commits and authors fall in the range.
3. **Authors**: merge people who committed under several names and choose the name shown on the
   designs (see [Merging authors](#merging-authors)).
4. **File groups**: decide how files are grouped (e.g. *TS*, *Tests*, *Native*) and which are
   left out (lockfiles, generated code); see [File groups](#file-groups).
5. **Generate**: set the title, choose designs and press **Generate visualizations**. The SVGs appear on
   the page with *Open* and *Download* links and are saved to `./commitscape-output/<title>/`.

`./run.sh web --port 9000 --out ~/Desktop/viz --no-browser` changes the port, output folder or skips
opening the browser. If port 8765 is busy, the next free port is used.

## Default date range

Unless you pick something else:

* **From 1 July onwards**: 1 January of the current year → today.
* **Before 1 July**: the whole of last year (1 January → 31 December).

So on 24 Nov 2026 the default is 2026-01-01 → 2026-11-24, and on 15 Mar 2026 it's 2025-01-01 → 2025-12-31.
The same default applies to the web page and both terminal versions.

## Merging authors

People often commit under several names (`jdoe`, `Jane Doe`, `Jane D.`) or with a typo, and every
variant would otherwise appear as a separate person. In the **Authors** step:

* **Possible duplicates** lists identities that look like the same person: same email, same name in a
  different order or case, a name matching an email or GitHub login. Check the suggested final name
  and press **Merge**, or **Dismiss** it.
* **Merge anyone**: tick the authors, type the final name (or accept the suggestion) and press **Merge**.
* **Rename** a person by editing the name at the top of their card; it renames everyone in that group.
* **Split** takes one identity back out of a merged group.
* **Include** unticked leaves an author out of the designs entirely. Bots (dependabot, CI, release bots)
  start unticked; tick them to include them.
* Each identity shows its emails and commit counts to help you decide.

The mapping is remembered per repository (in `~/.commitscape/aliases/`) and loaded automatically next
time. **Export mapping** downloads it as a JSON file to share with teammates; **Import mapping** loads one.
The file looks like this, using the author names git reports:

```json
{
  "aliases": { "jdoe": "Jane Doe", "Jane D.": "Jane Doe" },
  "exclude": ["dependabot[bot]", "Release Bot"]
}
```

Tip: to fix names for *every* Git tool, not just Commitscape, add a
[`.mailmap`](https://git-scm.com/docs/gitmailmap) to the repository; Commitscape reads it too.

## File groups

The posters show a **share of lines changed** per group of files, and files in excluded groups are left
out of every design (line counts, bubble sizes, file counts, the directory and file-chord designs).
Groups come from two layers, checked in this order:

1. **Path rules**: an ordered list of `name → patterns`. A file goes to the first rule it matches.
   Patterns are globs on the path inside the repository: `*.test.*` matches a file name anywhere,
   `android/**` a folder at the root, `**/__tests__/**` a folder at any depth, `*.{ts,tsx}` either extension.
2. **Languages**: every other file is grouped by language (from its extension), and languages can be
   merged like authors: select TSX, TypeScript and JavaScript and **Merge** them as "JS / TS".

Untick **Include** on a rule or group to leave its files out. The preview bar shows the result live.

**Presets ("learnings")** make this reusable:

Both built-in presets group by **area of work** with path rules, so they carry over between projects,
and only the app's own code is grouped by language:

| Group | Matches (abridged) | |
|---|---|---|
| Generated | lockfiles, `*.min.js`, `*.map`, `*.snap`, `generated/`, `dist/`, `build/`, `Pods/`, `*.pbxproj` | excluded |
| Vendor | `vendor/`, `third_party/`, `external/` | excluded |
| AI & IDE | `.cursor/`, `.claude/`, `.kiro/`, `.agents/`, `*.mdc`, `AGENTS.md`, `CLAUDE.md`, `.vscode/`, `.idea/` | excluded |
| Tests | `__tests__/`, `test/`, `tests/`, `e2e/`, `fixtures/`, `*.test.*`, `*.spec.*`, `*_test.*`, native unit tests, test configs | |
| Stories | `*.stories.*`, `.storybook/`, `storybook/` | |
| Tooling | `.github/`, `ci/`, `Jenkinsfile*`, `fastlane/`, `.husky/`, `scripts/`, `tools/`, `Dockerfile`, `patches/`, `*.patch` | |
| Config | dotfiles (`.gitignore`, `.env`, `.*rc`, `.nvmrc`…), `*.config.js/ts`, plus JSON/YAML/TOML/XML files | |
| Assets | images, fonts, audio/video, Lottie, binaries (`.aar`, `.jar`, `.keystore`…) | |
| Docs | Markdown, MDX, reStructuredText, text | |

* **Default** (any project): the groups above, then one group per language, with TSX counted as
  TypeScript, JSX as JavaScript and CSS/SCSS/Less as *Styles*.
* **React Native**: the same, plus one *Native* group for Android (Kotlin/Java) and iOS (Swift/Obj-C) code,
  matched by folder (`android/`, `ios/` at any depth, `*-ios/`) **and** by file type (`*.kt`, `*.java`,
  `*.gradle`, `*.swift`, `*.m`, `*.h`, `*.podspec`, `*.storyboard`…), so native code in `modules/` and
  `plugins/` is caught too. TypeScript (*TS*) and JavaScript (*JS*) stay separate; Shell, Python, Ruby and
  Groovy count as *Tooling*. Picked automatically for repositories with `package.json`, `android/` and `ios/`.
* **Backend** (Java/Spring, Kotlin, .NET, Go, NestJS…): splits code **by layer** so hand-written logic
  stands apart from boilerplate: *API* (controllers, JAX-RS `*Resource(s)`, filters, interceptors,
  security), *Services* (services, clients, processors, adapters, schedulers), *DAO* (dao, repositories,
  mappers, migrations, SQL), *Models* (model/dto/entity/request/response/enum packages and classes),
  *Config* (config packages and `*Config` classes, `*.properties`, `application*.yml`, `web.xml`, keys),
  *Utils*, and *Content* (JSON under `resources/`, templates, i18n). *Tooling* adds build files
  (`pom.xml`, Gradle, `*.csproj`, shell scripts). **Excluded by default**: *Generated* (adds `target/`,
  `*.class`, `generated-sources/`), *Vendor* (adds `lib/`, `*.jar`), *AI & IDE* (adds `.classpath`,
  `.project`, `.springBeans`, `*.iml`) and *Data* (master-data and seed dumps such as `*master*.json`).
  Picked automatically for repositories with `pom.xml`, Gradle build files, `go.mod` or a `.csproj`.
* **Save as preset** stores your current setup in `~/.commitscape/presets/` so it's offered for every
  repository; **Export** / **Import** move it between machines and teammates.

Each repository remembers its own groups (`~/.commitscape/filegroups/`). The terminal version takes the
same file, or a preset name, with `--file-groups`:

```json
{
  "name": "Our RN app",
  "rules": [
    {"name": "Tests & stories", "patterns": ["**/__tests__/**", "*.test.*", "*.stories.*"]},
    {"name": "iOS", "patterns": ["ios/**"]}
  ],
  "languages": {"TSX": "JS / TS", "TypeScript": "JS / TS", "JavaScript": "JS / TS"},
  "exclude": ["Generated"]
}
```

## Terminal version

Everything is also available without the browser, which is handy for scripts and CI:

```sh
./run.sh cli                    # asks for the path, dates and title
./run.sh --target ~/code/my-app --yes                                   # all defaults
./run.sh --target ~/code/my-app --from 2025-01-01 --to 2025-12-31 --title "My App 2025" \
         --aliases my-app-aliases.json --file-groups react-native       # mapping + file groups
```

It uses the Rust build when `cargo` is installed (built on first run, under a minute; crates are
downloaded from crates.io once) and the pure-Python version otherwise (`python3 -m commitscape`).
A session looks like this; press Enter to accept the default in brackets:

```text
Path to the local Git repository [.]: ~/code/my-app
[1/4]   Reading commits from /Users/you/code/my-app...
        Found 4467 commits from 2018-12-21 to 2026-11-23.
[2/4]   Choose what to visualize
Start date (YYYY-MM-DD) [2026-01-01]:
End date (YYYY-MM-DD) [2026-11-24]:
Project title for the visualizations [my-app]: My App 2026
[3/4]   Analysed 731 commits between 2026-01-01 and 2026-11-24 -> ./commitscape-output/my-app-2026/analysis.json
[4/4]   Rendering SVGs...
Done. Open ./commitscape-output/my-app-2026/index.html to see them all.
```

| Option | Meaning |
|---|---|
| `-t, --target <path>` | Repository to visualize (any folder inside it works) |
| `--from <date>` / `--to <date>` | Inclusive date range, `YYYY-MM-DD`; see [Default date range](#default-date-range) |
| `--title <text>` | Title printed on the visualizations; defaults to the repository folder name |
| `--aliases <file>` | Author mapping JSON (as exported from the web page) |
| `--file-groups <file\|preset>` | File groups JSON (as exported) or a preset: `default`, `react-native`, `backend`; default: detected |
| `-o, --out <dir>` | Output directory; defaults to `./commitscape-output/<title>` |
| `-y, --yes` | Use the default for every question not given as an option |
| `--designs a,b` | Render only these designs (see [the designs](#the-designs)) |
| `--include-bots` | Keep commits from dependabot, CI bots, etc. (excluded by default) |
| `--no-render` | Only write `analysis.json` |
| `-h, --help` | Show all options |

**Other ways to run the terminal version:**

```sh
cargo run --release -- --target ~/code/my-app          # cargo directly
cargo install --path . && commitscape -t ~/code/my-app # installed binary (keep this checkout in place,
                                                       # or set COMMITSCAPE_HOME to its location)
python3 -m commitscape -t ~/code/my-app                # Python only, from this directory
```

**Re-render an existing analysis** without reading the repository again, e.g. with a new title,
mapping or subset of designs:

```sh
python3 -m commitscape.render commitscape-output/my-app-2026/analysis.json \
    --out commitscape-output/my-app-2026 --title "My App · 2026" --aliases my-app-aliases.json
```

## Output

```text
commitscape-output/my-app-2026/
├── index.html        gallery of all designs
├── week-circles.svg
├── user-bubbles.svg
├── trunk.svg
├── cross.svg
├── core.svg
├── analysis.json     the extracted history for the chosen range
├── aliases.json      the author mapping used (web interface only)
└── file-groups.json  the file groups used (web interface only)
```

## The designs

| Design | What it shows |
|---|---|
| `week-circles` | Every commit on a 7-day clock: angle = weekday and hour, distance from centre = time within the range |
| `user-bubbles` | One row per top-10 committer (plus everyone else) with their commits over time |
| `trunk` | Nested contours showing how much each top contributor shaped each era of the project |
| `cross` | Ribbons connecting each file group and module (e.g. *Services › health*) to the authors who changed it |
| `core` | A ring of file groups, modules and files, with chords between files that change in the same commit |

In `cross` and `core`, the outer level is the [file group](#file-groups) and the inner level is the
**module**: the first folder that says what the code is about. Structural folders (`src/main/java`,
`resources/jsons`, `screens`, `components`…), the Java package prefix (`com/<company>/<app>`) and layer
names (`services`, `model`, `controller`…) are skipped, and an umbrella folder holding a large share
(e.g. `product`) is split into its sub-folders (`term`, `health`, `motor`…).

In the bubble designs, size is lines changed and colour is the additions/deletions ratio:
teal = more lines added, sand = about the same, red = more lines deleted.

Commits by bots are left out of the visualizations unless you include them, so the terminal's
"Analysed N commits" can be higher than the count printed on the posters.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Couldn't read the Git history …` / `not a git repository` | The path must point at a folder inside a Git repository. |
| `No commits found between …` / "No commits in this range" | Widen the date range; the message shows the repository's first and latest commit dates. |
| The browser didn't open | Open the address printed in the terminal (`http://127.0.0.1:8765/` by default). |
| Browse… shows no dialog | Check behind the browser window. On Linux install `zenity` or `kdialog`; otherwise the in-page folder browser is used. |
| A language is mislabelled or a folder is in the wrong group | Add a path rule for it in **File groups** (rules win over languages), or merge the language into the right group. |
| "Invalid session; reload the page" | The server was restarted; reload the page. |
| `couldn't find the commitscape renderer` | Run from this checkout, or set `COMMITSCAPE_HOME=/path/to/commitscape`. |
| `running python3 … (Python 3.9+ is required to render)` | Install Python 3, or point `PYTHON=/path/to/python3` at it. |
| No prompts appear in a script or CI | Prompts need a terminal; pass the answers as options or use `--yes`. |
| A machine without the `git` command | Build with libgit2 instead: `cargo build --release --no-default-features --features git-libgit2`. |
| Empty posters and "every commit in this range is from a bot" | Only bots committed in that range; widen it or include the bots. |
| Labels missing on `core.svg` in some image viewers | Text along curves needs a browser; open the SVG or `index.html` in Chrome, Safari or Firefox. |

## How it works

```
git log ──► analyser ──► analysis.json ──► author mapping ──► commitscape/render.py ──► *.svg
             (Rust: src/, or Python: commitscape/analyse.py)
```

* [commitscape/web.py](commitscape/web.py) and [commitscape/web/index.html](commitscape/web/index.html): the
  web interface. A local-only server (standard library) and a single page with no external resources.
  API calls need a per-session token and a localhost `Host`, so other websites can't use it.
* [commitscape/identities.py](commitscape/identities.py): merging/renaming/excluding authors and duplicate suggestions.
* [commitscape/filegroups.py](commitscape/filegroups.py): path rules, language merges, presets and exclusions for files.
* [commitscape/render.py](commitscape/render.py): the SVG renderers.
* [src/main.rs](src/main.rs): the Rust terminal version: prompts, date filtering, launching the renderer.
* [src/git/cli.rs](src/git/cli.rs): the read-only `git log` command that is parsed ([src/git/git2.rs](src/git/git2.rs)
  does the same through libgit2). [commitscape/analyse.py](commitscape/analyse.py) is the Python equivalent and
  produces identical `analysis.json`.

Commitscape only runs read-only git commands (`log`, `rev-parse`, `rev-list`). It writes to the output
directory and, for the web interface, the remembered author mappings, file groups and presets in
`~/.commitscape/`.

## License

AGPL-3.0, see [LICENSE.txt](LICENSE.txt). Commitscape is a modified version of an AGPL-3.0
analyser originally written by Arpad Ray.
