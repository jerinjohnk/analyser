#!/usr/bin/env bash
# Commitscape launcher: visualize a local Git repository's history as SVG posters.
# Uses the Rust CLI when cargo is available, otherwise the pure-Python version.
# Everything runs locally; no network access is needed (apart from cargo
# fetching crates on the very first build).
#
#   ./run.sh                                   # interactive
#   ./run.sh --target ~/code/app --from 2025-01-01 --to 2025-12-31 --title "App 2025"

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COMMITSCAPE_HOME="$HERE"

if ! command -v git > /dev/null 2>&1; then
	echo "commitscape needs git on your PATH." >&2
	exit 1
fi
if ! command -v python3 > /dev/null 2>&1; then
	echo "commitscape needs python3 (3.9+) on your PATH to render the SVGs." >&2
	exit 1
fi

if command -v cargo > /dev/null 2>&1; then
	cargo build --quiet --release --manifest-path "$HERE/Cargo.toml"
	exec "$HERE/target/release/commitscape" "$@"
fi

echo "cargo not found, using the Python version." >&2
exec env PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" python3 -m commitscape "$@"
