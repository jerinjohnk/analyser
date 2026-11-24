# Commitscape

Turn a Git repository's history into poster-style SVG visualizations, **entirely offline**.
Commitscape reads the repository with `git`, asks which dates to cover and what title to print, and
renders five designs plus an `index.html` gallery. There are no accounts, uploads or server calls.

## Quick start

```sh
./run.sh
```

You'll be asked for:

1. **Path to the local Git repository** (default: current directory)
2. **Start date** and **end date** (`YYYY-MM-DD`; defaults to the first and latest commit)
3. **Project title** shown on every visualization (default: the repository folder name)

The SVGs go to `./commitscape-output/<title>/`, so open `index.html` there to browse them.

Every question can also be passed as an option to run without prompts:

```sh
./run.sh --target ~/code/my-app --from 2025-01-01 --to 2025-12-31 --title "My App · 2025"
./run.sh --target ~/code/my-app --yes          # accept all defaults
```

| Option | Meaning |
|---|---|
| `-t, --target <path>` | Repository to visualize |
| `--from <date>` / `--to <date>` | Inclusive date range, `YYYY-MM-DD` |
| `--title <text>` | Title printed on the visualizations |
| `-o, --out <dir>` | Output directory (default `./commitscape-output/<title>`) |
| `-y, --yes` | Use the default for anything not given |
| `--designs a,b` | Render only some designs |
| `--include-bots` | Keep commits from dependabot, CI bots, etc. (excluded by default) |
| `--no-render` | Only write `analysis.json` |

## The designs

| Design | What it shows |
|---|---|
| `week-circles` | Every commit on a 7-day clock: angle = weekday and hour, distance from centre = time within the range |
| `user-bubbles` | One row per top-10 committer (plus everyone else) with their commits over time |
| `trunk` | Nested contours showing how much each top contributor shaped each era of the project |
| `cross` | Ribbons connecting directories to the authors who changed them |
| `core` | A ring of directories and files, with chords between files that change in the same commit |

In the bubble designs, size is lines changed and colour is the additions/deletions ratio:
teal = more lines added, sand = about the same, red = more lines deleted.

## Requirements

- `git`
- Python 3.9+ (the renderer uses only the standard library)
- Optional: Rust (`cargo`) for the faster native analyser. `run.sh` uses it when available and
  falls back to the pure-Python version (`python3 -m commitscape`) otherwise.

## How it works

```
git log ──► analyser (Rust: src/, or Python: commitscape/analyse.py) ──► analysis.json ──► commitscape/render.py ──► *.svg
```

* [src/main.rs](src/main.rs): the CLI: prompts, date filtering, writing `analysis.json`, launching the renderer.
* [src/git/cli.rs](src/git/cli.rs): the `git log` command that is parsed. [src/git/git2.rs](src/git/git2.rs) does
  the same through libgit2 (`cargo build --release --no-default-features --features git-libgit2`) for machines without `git`.
* [src/analysis.rs](src/analysis.rs): the analysis structure written to `analysis.json`.
* [commitscape/render.py](commitscape/render.py): the SVG renderers.
* [commitscape/\_\_main\_\_.py](commitscape/__main__.py): the Python-only interactive entry point.

If you run the compiled binary from somewhere other than this checkout, set `COMMITSCAPE_HOME`
to this directory so it can find the renderer.

## License

AGPL-3.0, see [LICENSE.txt](LICENSE.txt). Commitscape is a modified version of an AGPL-3.0
analyser originally written by Arpad Ray.
