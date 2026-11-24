#!/usr/bin/env python3
"""
Commitscape analyser (Python): reads a local Git repository's history with `git log`
and produces the same analysis JSON as the Rust CLI (src/main.rs + src/git/cli.rs).
Stop words and the language table are read from src/ so both analysers agree.

Usage:
  python3 -m commitscape.analyse --target /path/to/repo --out analysis.json \
      [--from 2025-01-01] [--to 2025-12-31] [--title "My project"]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_WORDS = None


def load_stop_words():
    """Reads the stop-word list from src/analysis.rs so both analysers agree."""
    src = open(os.path.join(HERE, "..", "src", "analysis.rs"), encoding="utf-8").read()
    body = src[src.index("fn get_stop_words"):]
    return set(re.findall(r'\("([^"]+)", 0\)', body))


def load_languages():
    """Reads LANG_NAMES and the extension index from src/languages.rs."""
    src = open(os.path.join(HERE, "..", "src", "languages.rs"), encoding="utf-8").read()
    names_block = src[src.index("LANG_NAMES"):src.index("];")]
    names = re.findall(r'^\s*"((?:[^"\\]|\\.)*)",', names_block, re.M)
    index_block = src[src.index("fn get_language_index"):]
    ext_map = {ext: names[int(i)] for ext, i in re.findall(r'\("([^"]+)",\s*(\d+)\)', index_block)}
    return ext_map


def filter_words(words):
    out = []
    for w in words:
        if len(w) < 2 or not any(c.isalpha() for c in w):
            continue
        lw = w.lower()
        if lw not in STOP_WORDS:
            out.append(lw)
    return out


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def analyse(target):
    """Reads the full history. Returns (analysis dict, repo root)."""
    global STOP_WORDS
    STOP_WORDS = load_stop_words()
    root = git("rev-parse", "--show-toplevel", cwd=target).decode().strip()
    rev = git("rev-parse", "HEAD", cwd=root).decode().strip()
    log = git("log", "--all", "--numstat", "--date=rfc2822", "--use-mailmap",
              "--pretty=format:#%H#%ad#%aE#%aN#%f", "--no-renames", "--no-expand-tabs",
              "--no-merges", "--encoding=UTF-8", cwd=root).decode("utf-8", "replace")

    files, files_map, users, users_map, commits = [], {}, [], {}, []

    def file_idx(p):
        if p not in files_map:
            files_map[p] = len(files)
            files.append(p)
        return files_map[p]

    def user_idx(n):
        if n not in users_map:
            users_map[n] = len(users)
            users.append({"name": n, "contributions": 0, "emails": []})
        return users_map[n]

    commit = None
    for line in log.split("\n"):
        if len(line) < 2:
            if commit:
                commits.append(commit)
            commit = None
            continue
        if line.startswith("#"):
            if commit:
                commits.append(commit)
            # #hash#date#email#name#subject-slug; the slug never contains '#', so split it
            # off from the right to keep author names containing '#' intact.
            parts = line[1:].split("#", 3)
            t = parsedate_to_datetime(parts[1])
            email = parts[2].strip() if len(parts) > 2 else ""
            name, _, slug = (parts[3] if len(parts) > 3 else "Unknown").rpartition("#")
            if not name:
                name, slug = slug, ""
            u = user_idx(name)
            users[u]["contributions"] += 1
            if email and email.lower() not in (e.lower() for e in users[u]["emails"]):
                users[u]["emails"].append(email)
            words = filter_words(slug.split("-")) if slug else []
            commit = {"id": parts[0], "time": t.isoformat(), "user": u, "files": [], "words": words}
        elif commit is not None:
            cols = line.split("\t", 2)
            if len(cols) != 3:
                continue
            add = 0 if cols[0] == "-" else int(cols[0] or 0)
            dele = 0 if cols[1] == "-" else int(cols[1] or 0)
            commit["files"].append({"File": file_idx(cols[2]), "Additions": add, "Deletions": dele})
    if commit:
        commits.append(commit)

    ext_map = load_languages()
    langs, total = {}, 0
    for p in files:
        full = os.path.join(root, p)
        if os.path.isfile(full):
            size = os.path.getsize(full)
            total += size
            ext = os.path.splitext(p)[1].lstrip(".")
            if ext in ext_map:
                langs[ext_map[ext]] = langs.get(ext_map[ext], 0) + size

    return {
        "commits": commits,
        "files": files,
        "users": users,
        "languages": [{"name": k, "bytes": v} for k, v in sorted(langs.items(), key=lambda kv: -kv[1])],
        "revision": rev,
        "totalBytes": total,
        "meta": {"title": os.path.basename(root), "from": "", "to": ""},
    }, root


def default_range(today=None):
    """This year so far once it's more than six months old (from 1 July), otherwise all of last year."""
    today = today or date.today()
    if today >= date(today.year, 7, 1):
        return date(today.year, 1, 1), today
    return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)


def date_bounds(data):
    """(first, last) commit dates, or None for an empty history."""
    days = [c["time"][:10] for c in data["commits"]]
    return (date.fromisoformat(min(days)), date.fromisoformat(max(days))) if days else None


def retain_range(data, start, end):
    """Keeps commits whose author date is within start..end (inclusive) and recounts contributions."""
    data["commits"] = [c for c in data["commits"] if start <= date.fromisoformat(c["time"][:10]) <= end]
    for u in data["users"]:
        u["contributions"] = 0
    for c in data["commits"]:
        data["users"][c["user"]]["contributions"] += 1
    data["meta"]["from"] = start.isoformat()
    data["meta"]["to"] = end.isoformat()
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=".")
    ap.add_argument("--out", default="analysis.json")
    ap.add_argument("--from", dest="start", type=date.fromisoformat,
                    help="first day (YYYY-MM-DD); default: 1 Jan this year from July on, else 1 Jan last year")
    ap.add_argument("--to", dest="end", type=date.fromisoformat,
                    help="last day (YYYY-MM-DD); default: today from July on, else 31 Dec last year")
    ap.add_argument("--title", help="project title shown on the visualizations")
    a = ap.parse_args()
    data, _ = analyse(a.target)
    bounds = date_bounds(data)
    if not bounds:
        sys.exit("This repository has no commits to visualize")
    default_start, default_end = default_range()
    retain_range(data, a.start or default_start, a.end or default_end)
    if a.title:
        data["meta"]["title"] = a.title
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"{len(data['commits'])} commits, {len(data['users'])} users, {len(data['files'])} files -> {a.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
