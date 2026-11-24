"""
Commitscape web interface: `python3 -m commitscape.web` (or `./run.sh`).

Serves a single page on http://127.0.0.1:<port> to pick a repository, a date range and how
authors should be merged/renamed, then renders the SVGs. Everything stays on this machine:
the server only listens on the loopback interface and makes no outbound connections.

Safety: every /api call must carry the random per-session token embedded in the page and a
localhost Host header, so other websites open in the same browser can't drive the server.
"""
import argparse
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from contextlib import redirect_stdout
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .analyse import analyse, date_bounds, default_range, retain_range
from .filegroups import PRESETS, Classifier, breakdown, clean_profile, detect_preset, save_profile
from .identities import clean_aliases, is_bot, load_aliases, save_aliases, suggest_groups
from .render import RENDERERS, Data, render_all

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")
HOME_STORE = os.path.join(os.path.expanduser("~"), ".commitscape")
PRESET_STORE = os.path.join(HOME_STORE, "presets")
MAX_BODY = 10 * 1024 * 1024


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"


def store_path(root, kind="aliases"):
    """Where a repository's author mapping ("aliases") or file groups ("filegroups") are remembered."""
    digest = hashlib.sha1(root.encode()).hexdigest()[:10]
    return os.path.join(HOME_STORE, kind, f"{slugify(os.path.basename(root))}-{digest}.json")


def list_presets():
    """Built-in file-group presets plus the ones saved from the page (~/.commitscape/presets)."""
    out = [{"id": key, "name": p["name"], "builtin": True, "profile": clean_profile(p)} for key, p in PRESETS.items()]
    if os.path.isdir(PRESET_STORE):
        for fname in sorted(os.listdir(PRESET_STORE)):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(PRESET_STORE, fname), encoding="utf-8") as f:
                        prof = clean_profile(json.load(f))
                except (OSError, ValueError):
                    continue
                out.append({"id": "user:" + fname[:-5], "name": prof["name"], "builtin": False, "profile": prof})
    return out


def native_folder_picker(start):
    """Opens the operating system's "choose folder" dialog on this machine.

    Returns ("ok", path), ("cancelled", None) or ("unsupported", reason). Blocks until the user
    answers, which is fine: each request runs in its own thread.
    """
    start = start if os.path.isdir(start) else os.path.expanduser("~")
    prompt = "Choose the folder of your Git project"
    if sys.platform == "darwin":
        # `tell me to activate` brings the dialog in front of the browser without needing
        # permission to control another app. The start folder is passed as an argument, not
        # spliced into the script.
        script = ['on run argv',
                  'tell me to activate',
                  f'return POSIX path of (choose folder with prompt "{prompt}" '
                  'default location (POSIX file (item 1 of argv)))',
                  'end run']
        cmd = ["osascript"] + [a for line in script for a in ("-e", line)] + [start]
    elif sys.platform.startswith("win"):
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.Description = '{prompt}'; $d.SelectedPath = $args[0];"
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps, start]
    else:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return "unsupported", "no graphical display"
        if shutil.which("zenity"):
            cmd = ["zenity", "--file-selection", "--directory", f"--title={prompt}",
                   f"--filename={os.path.join(start, '')}"]
        elif shutil.which("kdialog"):
            cmd = ["kdialog", "--getexistingdirectory", start, "--title", prompt]
        else:
            return "unsupported", "install zenity or kdialog for a native folder picker"
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        return "unsupported", str(e)
    path = res.stdout.strip()
    if res.returncode != 0 or not path:
        return "cancelled", None
    return "ok", path.rstrip("/\\") or path


def list_dirs(path):
    """Sub-folders of `path` for the in-page folder browser, flagging Git repositories."""
    path = os.path.realpath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise ApiError(f"Folder not found: {path}")
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "path": e.path,
                                        "git": os.path.exists(os.path.join(e.path, ".git"))})
                except OSError:
                    continue
    except PermissionError:
        raise ApiError(f"No permission to open {path}")
    entries.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None,
            "git": os.path.exists(os.path.join(path, ".git")), "home": os.path.expanduser("~"),
            "entries": entries[:1000], "truncated": len(entries) > 1000}


class ApiError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, out_root, port):
        self.token = secrets.token_urlsafe(24)
        os.makedirs(out_root, exist_ok=True)
        self.out_root = os.path.realpath(out_root)  # realpath: /out/ containment check compares realpaths
        self.port = port
        self.cache = {}  # repo root -> full analysis
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ API

    def load(self, body):
        path = os.path.expanduser(str(body.get("path") or "").strip() or ".")
        if not os.path.isdir(path):
            raise ApiError(f"Folder not found: {path}")
        try:
            data, root = analyse(path)
        except subprocess.CalledProcessError as e:  # e.g. not a repository
            detail = (e.stderr or b"").decode("utf-8", "replace").strip().removeprefix("fatal: ")
            raise ApiError(f"Couldn't read the Git history of {path}: {detail or 'git failed'}")
        except FileNotFoundError:
            raise ApiError("git isn't installed or isn't on PATH.")
        if not data["commits"]:
            raise ApiError("This repository has no commits to visualize.")
        with self.lock:
            self.cache[root] = data

        days = [[] for _ in data["users"]]
        for c in data["commits"]:
            days[c["user"]].append(c["time"][:10])
        authors = [{"name": u["name"], "emails": u.get("emails", []), "bot": is_bot(u["name"]),
                    "days": sorted(d)} for u, d in zip(data["users"], days)]
        counts = {i: len(d) for i, d in enumerate(days)}
        groups_saved, detected = None, detect_preset(data["files"])
        if os.path.exists(store_path(root, "filegroups")):
            try:
                with open(store_path(root, "filegroups"), encoding="utf-8") as f:
                    groups_saved = clean_profile(json.load(f))
            except (OSError, ValueError):
                groups_saved = None
        saved = None
        if os.path.exists(store_path(root)):
            try:
                aliases, exclude = load_aliases(store_path(root))
                saved = {"aliases": aliases, "exclude": sorted(exclude)}
            except (OSError, ValueError):
                saved = None
        first, last = date_bounds(data)
        start, end = default_range()
        return {
            "root": root,
            "name": os.path.basename(root),
            "revision": data["revision"][:8],
            "bounds": [first.isoformat(), last.isoformat()],
            "defaultRange": [start.isoformat(), end.isoformat()],
            "today": date.today().isoformat(),
            "authors": authors,
            "suggestions": suggest_groups(data["users"], counts),
            "saved": saved,
            "savedPath": store_path(root),
            "designs": list(RENDERERS),
            "fileGroups": groups_saved or clean_profile(PRESETS[detected]),
            "fileGroupsSaved": groups_saved is not None,
            "detectedPreset": detected,
            "presets": list_presets(),
        }

    def _range_commits(self, body):
        """The cached analysis restricted to the requested range (a copy)."""
        root = body.get("root")
        with self.lock:
            full = self.cache.get(root)
        if full is None:
            raise ApiError("Load the repository first.")
        try:
            start = date.fromisoformat(body.get("from", ""))
            end = date.fromisoformat(body.get("to", ""))
        except ValueError:
            raise ApiError("Dates must be YYYY-MM-DD.")
        if start > end:
            raise ApiError("The start date is after the end date.")
        data = dict(full, commits=list(full["commits"]), users=[dict(u) for u in full["users"]],
                    meta=dict(full["meta"]))
        retain_range(data, start, end)
        return root, data

    def files(self, body):
        """Live preview for the File groups step: lines/files per group, rule and language."""
        _, data = self._range_commits(body)
        try:
            profile = clean_profile(body.get("fileGroups") or {})
        except ValueError as e:
            raise ApiError(str(e))
        excluded_authors = {str(x) for x in body.get("excludeAuthors") or []}
        names = [u["name"] for u in data["users"]]
        commits = [c for c in data["commits"] if names[c["user"]] not in excluded_authors]
        clf = Classifier(profile)
        groups, rules, langs = breakdown(commits, data["files"], clf)
        lang_rows = [{"name": k, "group": profile["languages"].get(k, k), **v} for k, v in langs.items()]
        return {
            "groups": sorted(({"name": k, "excluded": k in clf.exclude, **v} for k, v in groups.items()),
                             key=lambda g: -g["lines"]),
            "rules": [{"name": r["name"], **rules.get(r["name"], {"lines": 0, "files": 0})} for r in profile["rules"]],
            "languages": sorted(lang_rows, key=lambda l: -l["lines"]),
        }

    def save_preset(self, body):
        try:
            profile = clean_profile(body.get("profile") or {})
        except ValueError as e:
            raise ApiError(str(e))
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError("Give the preset a name.")
        profile["name"] = name
        os.makedirs(PRESET_STORE, exist_ok=True)
        save_profile(os.path.join(PRESET_STORE, slugify(name) + ".json"), profile)
        return {"presets": list_presets(), "id": "user:" + slugify(name)}

    def pick_folder(self, body):
        status, value = native_folder_picker(os.path.expanduser(str(body.get("start") or "~")))
        if status == "ok":
            return {"path": value}
        if status == "cancelled":
            return {"cancelled": True}
        return {"unsupported": True, "reason": value}

    def list_folder(self, body):
        return list_dirs(str(body.get("path") or "~"))

    def render(self, body):
        root, data = self._range_commits(body)
        try:
            aliases, exclude = clean_aliases({"aliases": body.get("aliases") or {},
                                              "exclude": body.get("exclude") or []})
            profile = clean_profile(body.get("fileGroups")) if body.get("fileGroups") else None
        except ValueError as e:
            raise ApiError(str(e))
        designs = body.get("designs") or list(RENDERERS)
        unknown = [d for d in designs if d not in RENDERERS]
        if unknown:
            raise ApiError(f"Unknown design(s): {', '.join(unknown)}")
        title = str(body.get("title") or "").strip() or os.path.basename(root)

        data["meta"]["title"] = title
        # The page decides about bots explicitly through `exclude`.
        d = Data(data, None, True, aliases, exclude, profile)
        if not d.commits:
            raise ApiError("No commits left in this range after excluding authors.")

        out = os.path.join(self.out_root, slugify(title))
        os.makedirs(out, exist_ok=True)
        with redirect_stdout(io.StringIO()):
            render_all(d, out, designs)
        with open(os.path.join(out, "analysis.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        save_aliases(os.path.join(out, "aliases.json"), aliases, exclude)
        os.makedirs(os.path.dirname(store_path(root)), exist_ok=True)
        save_aliases(store_path(root), aliases, exclude)
        if profile:
            save_profile(os.path.join(out, "file-groups.json"), profile)
            os.makedirs(os.path.dirname(store_path(root, "filegroups")), exist_ok=True)
            save_profile(store_path(root, "filegroups"), profile)

        rel = os.path.relpath(out, self.out_root).replace(os.sep, "/")
        return {
            "outDir": out,
            "files": [{"design": n, "url": f"/out/{rel}/{n}.svg"} for n in designs],
            "index": f"/out/{rel}/index.html",
            "aliasesFile": f"/out/{rel}/aliases.json",
            "fileGroupsFile": f"/out/{rel}/file-groups.json" if profile else None,
            "stats": {"commits": len(d.commits), "people": len(d.user_commits)},
        }

    # ------------------------------------------------------------------ HTTP

    def allowed_host(self, host):
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "commitscape"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, obj):
            self.send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not app.allowed_host(self.headers.get("Host", "")):
                return self.send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            path = unquote(urlsplit(self.path).path)
            if path in ("/", "/index.html"):
                with open(PAGE, encoding="utf-8") as f:
                    page = f.read().replace("__COMMITSCAPE_TOKEN__", app.token)
                csp = ("default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'; "
                       "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                return self.send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8",
                                 {"Content-Security-Policy": csp})
            if path.startswith("/out/"):
                target = os.path.realpath(os.path.join(app.out_root, path[len("/out/"):]))
                if not target.startswith(app.out_root + os.sep) or not os.path.isfile(target):
                    return self.send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
                ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
                with open(target, "rb") as f:
                    return self.send(HTTPStatus.OK, f.read(), ctype)
            self.send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

        def do_POST(self):
            if not app.allowed_host(self.headers.get("Host", "")):
                return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden host"})
            if not secrets.compare_digest(self.headers.get("X-Commitscape-Token", ""), app.token):
                return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid session; reload the page."})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                route = {"/api/load": app.load, "/api/render": app.render, "/api/pick-folder": app.pick_folder,
                         "/api/list-dirs": app.list_folder, "/api/files": app.files,
                         "/api/presets/save": app.save_preset}.get(urlsplit(self.path).path)
                if route is None:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
                self.send_json(HTTPStatus.OK, route(body))
            except ApiError as e:
                self.send_json(e.status, {"error": str(e)})
            except json.JSONDecodeError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            except Exception as e:  # report, don't crash the server
                print(f"error handling {self.path}: {e!r}", file=sys.stderr)
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Unexpected error: {e}"})

    return Handler


def main():
    ap = argparse.ArgumentParser(prog="commitscape web", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765, next free if busy)")
    ap.add_argument("-o", "--out", default="commitscape-output", help="where generated SVGs are written")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    a = ap.parse_args()

    server = None
    for port in range(a.port, a.port + 20):
        try:
            app = App(a.out, port)
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"Error: no free port between {a.port} and {a.port + 19}")
    url = f"http://127.0.0.1:{app.port}/"
    print(f"Commitscape is running at {url}  (local only; press Ctrl+C to stop)", flush=True)
    print(f"SVGs are written to {app.out_root}", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
