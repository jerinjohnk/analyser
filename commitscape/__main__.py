"""
Commitscape, Python-only entry point: `python3 -m commitscape`.

Same flow as the Rust CLI, for machines without a Rust toolchain:
asks for the repository path, date range and project title, then writes
analysis.json and the SVGs. Everything runs locally.
"""
import argparse
import json
import os
import re
import sys
from datetime import date

from .analyse import analyse, date_bounds, retain_range
from .render import RENDERERS, Data, render_all


def ask(prompt, default, assume_yes):
    if assume_yes:
        return default
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_date(prompt, default, assume_yes):
    while True:
        value = ask(prompt, default.isoformat(), assume_yes)
        try:
            return date.fromisoformat(value)
        except ValueError:
            if assume_yes:
                sys.exit(f"Error: invalid date {value!r}, expected YYYY-MM-DD")
            print(f"  {value!r} isn't a valid date, use YYYY-MM-DD.")


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "project"


def main():
    ap = argparse.ArgumentParser(prog="commitscape", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--target", help="path to the local Git repository")
    ap.add_argument("--from", dest="start", type=date.fromisoformat, help="first day to include (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", type=date.fromisoformat, help="last day to include (YYYY-MM-DD)")
    ap.add_argument("--title", help="project title shown on the visualizations")
    ap.add_argument("-o", "--out", help="output directory (default ./commitscape-output/<title>)")
    ap.add_argument("-y", "--yes", action="store_true", help="accept defaults for anything not given")
    ap.add_argument("--no-render", action="store_true", help="only write analysis.json")
    ap.add_argument("--designs", help=f"comma-separated, default all: {','.join(RENDERERS)}")
    ap.add_argument("--include-bots", action="store_true", help="keep dependabot/CI bot commits")
    a = ap.parse_args()

    target = os.path.expanduser(a.target or ask("Path to the local Git repository", ".", a.yes))
    if not os.path.isdir(target):
        sys.exit(f"Error: invalid target directory ({target})")

    print("[1/4]\tReading commits...")
    try:
        data, root = analyse(target)
    except Exception as e:  # git missing, not a repository, ...
        sys.exit(f"Error: couldn't read the Git history of {target}: {e}")
    bounds = date_bounds(data)
    if not bounds:
        sys.exit("Error: this repository has no commits to visualize")
    print(f"\tFound {len(data['commits'])} commits from {bounds[0]} to {bounds[1]}.")

    print("[2/4]\tChoose what to visualize")
    start = a.start or ask_date("Start date (YYYY-MM-DD)", bounds[0], a.yes)
    end = a.end or ask_date("End date (YYYY-MM-DD)", bounds[1], a.yes)
    if start > end:
        sys.exit(f"Error: the start date {start} is after the end date {end}")
    retain_range(data, start, end)
    if not data["commits"]:
        sys.exit(f"Error: no commits found between {start} and {end}")
    data["meta"]["title"] = a.title or ask("Project title for the visualizations", os.path.basename(root), a.yes)

    out = os.path.abspath(a.out or os.path.join("commitscape-output", slugify(data["meta"]["title"])))
    os.makedirs(out, exist_ok=True)
    json_path = os.path.join(out, "analysis.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"[3/4]\tAnalysed {len(data['commits'])} commits between {start} and {end} -> {json_path}")

    if a.no_render:
        print("[4/4]\tSkipping rendering (--no-render).")
        return
    print("[4/4]\tRendering SVGs...")
    designs = [n.strip() for n in a.designs.split(",") if n.strip()] if a.designs else None
    render_all(Data(data, None, a.include_bots), out, designs)
    print(f"\nDone. Open {os.path.join(out, 'index.html')} to see them all.")


if __name__ == "__main__":
    main()
