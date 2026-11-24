#!/usr/bin/env bash
# Commitscape launcher: visualize a local Git repository's history as SVG posters.
# Everything runs locally; no network access is needed (apart from cargo
# fetching crates on the very first CLI build).
#
#   ./run.sh                       # web interface in your browser (default)
#   ./run.sh web --port 9000       # web interface with options (see ./run.sh web --help)
#   ./run.sh cli                   # interactive terminal version
#   ./run.sh --target ~/code/app --from 2025-01-01 --to 2025-12-31 --title "App 2025"   # CLI with options

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COMMITSCAPE_HOME="$HERE"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v git > /dev/null 2>&1; then
	echo "commitscape needs git on your PATH." >&2
	exit 1
fi
if ! command -v python3 > /dev/null 2>&1; then
	echo "commitscape needs python3 (3.9+) on your PATH." >&2
	exit 1
fi

# Web interface: no arguments, or "web [options]".
if [ $# -eq 0 ] || [ "$1" = "web" ]; then
	[ $# -gt 0 ] && shift
	exec python3 -m commitscape.web "$@"
fi

# Terminal version: "cli [options]" or any options.
[ "$1" = "cli" ] && shift
if command -v cargo > /dev/null 2>&1; then
	cargo build --quiet --release --manifest-path "$HERE/Cargo.toml"
	exec "$HERE/target/release/commitscape" "$@"
fi
echo "cargo not found, using the Python version." >&2
exec python3 -m commitscape "$@"
