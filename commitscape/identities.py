"""
Author identities: merging people who committed under several names, renaming them,
and excluding accounts (bots, imports) from the visualizations.

An aliases file is JSON:
    {
      "aliases": {"jdoe": "Jane Doe", "Jane D.": "Jane Doe"},   # original name -> final name
      "exclude": ["dependabot[bot]"]                             # original names to leave out
    }
Original names are the author names git reports (after the repository's .mailmap).
"""
import json
import re
import unicodedata

BOT_RE = re.compile(r"\[bot\]|^bot|bot$|\bbot\b|github actions|^renovate", re.I)
GITHUB_NOREPLY_RE = re.compile(r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", re.I)
GENERIC_LOCALS = {"admin", "root", "info", "dev", "developer", "git", "github", "noreply", "no-reply",
                  "user", "mail", "contact", "hello", "support", "test", "me", "build", "ci"}


def is_bot(name):
    return bool(BOT_RE.search(name or ""))


def load_aliases(path):
    """Reads an aliases file. Returns (aliases dict, exclude set)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return clean_aliases(data)


def clean_aliases(data):
    """Validates the aliases structure (from a file or the web page). Returns (aliases, exclude)."""
    if not isinstance(data, dict):
        raise ValueError("aliases must be a JSON object with 'aliases' and/or 'exclude'")
    raw = data.get("aliases") or {}
    excl = data.get("exclude") or []
    if not isinstance(raw, dict) or not isinstance(excl, list):
        raise ValueError("'aliases' must be an object and 'exclude' a list")
    aliases = {str(k): str(v).strip() for k, v in raw.items() if str(v).strip() and str(v).strip() != str(k)}
    return aliases, {str(x) for x in excl}


def save_aliases(path, aliases, exclude):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"aliases": dict(sorted(aliases.items())), "exclude": sorted(exclude)}, f, indent=2,
                  ensure_ascii=False)
        f.write("\n")


def resolve_users(users, aliases=None, exclude=None, include_bots=False):
    """Maps each original user to a final, merged identity.

    Returns (final_users, mapping, dropped_bots): final_users is a list of {"name"}, mapping maps
    an original user index to a final index (excluded users are absent), and dropped_bots holds the
    original indices left out for being bots. A user that has been given an alias is never treated
    as a bot, so a misdetected human can be kept by naming them.
    """
    aliases, exclude = aliases or {}, exclude or set()
    final_users, by_name, mapping, dropped_bots = [], {}, {}, set()
    for i, u in enumerate(users):
        name = u["name"]
        if name in exclude:
            continue
        if not include_bots and name not in aliases and is_bot(name):
            dropped_bots.add(i)
            continue
        final = aliases.get(name, name)
        if final not in by_name:
            by_name[final] = len(final_users)
            final_users.append({"name": final})
        mapping[i] = by_name[final]
    return final_users, mapping, dropped_bots


def _fold(s):
    """Lowercase, accents removed."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _keys(user):
    """Identity keys: two authors sharing any key are probably the same person."""
    name = _fold(user["name"])
    tokens = re.findall(r"[a-z0-9]+", name)
    keys = set()
    if tokens:
        keys.add(("name", " ".join(sorted(tokens))))       # "Doe, Jane" == "jane doe"
        compact = "".join(tokens)
        if len(compact) >= 4:
            keys.add(("compact", compact))                  # "Jane Doe" == "janedoe"
    for email in user.get("emails") or []:
        e = _fold(email.strip())
        keys.add(("email", e))
        m = GITHUB_NOREPLY_RE.match(e)
        local = m.group(1) if m else e.split("@", 1)[0]
        local_compact = "".join(re.findall(r"[a-z0-9]+", local))
        if len(local_compact) >= 4 and local_compact not in GENERIC_LOCALS:
            keys.add(("compact", local_compact))            # jane.doe@x == "Jane Doe" == janedoe login
    return keys


def suggest_groups(users, counts=None):
    """Groups of original user indices that look like the same person.

    `counts` (index -> commits) picks the suggested final name: the variant with most commits.
    Returns [{"members": [idx, ...], "name": suggested final name, "reasons": [...]}].
    """
    counts = counts or {}
    parent = list(range(len(users)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner = {}
    keys = [set() if is_bot(u["name"]) else _keys(u) for u in users]
    for i, ks in enumerate(keys):
        for key in ks:
            if key in owner:
                a, b = find(owner[key]), find(i)
                if a != b:
                    parent[b] = a
            else:
                owner[key] = i
    groups = {}
    for i in range(len(users)):
        groups.setdefault(find(i), []).append(i)
    labels = {"email": "same email", "name": "same name", "compact": "name matches name/email"}
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        # A reason applies when at least two members share a key of that kind.
        seen, why = {}, set()
        for m in members:
            for kind, value in keys[m]:
                if seen.setdefault((kind, value), m) != m:
                    why.add(labels[kind])
        # Prefer a full "First Last" name over handles, then the most-used variant.
        best = max(members, key=lambda m: (" " in users[m]["name"].strip(), counts.get(m, 0),
                                           len(users[m]["name"])))
        out.append({"members": sorted(members, key=lambda m: -counts.get(m, 0)),
                    "name": users[best]["name"],
                    "reasons": sorted(why)})
    out.sort(key=lambda g: -sum(counts.get(m, 0) for m in g["members"]))
    return out
