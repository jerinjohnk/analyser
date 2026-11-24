"""
File groups: how files are bucketed for the "share of lines changed" breakdown, and which files
are left out of the analysis entirely (lockfiles, generated code, ...).

A file-groups profile is JSON:
    {
      "name": "React Native",
      "rules": [                                       # checked in order; first match wins
        {"name": "Tests", "patterns": ["**/__tests__/**", "*.test.*"]},
        {"name": "Android", "patterns": ["android/**"]}
      ],
      "languages": {"TSX": "JS / TS", "TypeScript": "JS / TS"},   # merges for everything else
      "exclude": ["Generated"]                        # group names to leave out
    }

Patterns are globs on the repository-relative path: `*` matches within a folder, `**` across
folders, `{a,b}` either. A pattern without a `/` matches the file name anywhere (`*.test.*`);
a pattern ending in `/` matches everything below that folder.
Files no rule matches are grouped by language (from their extension), then `languages` merges apply.
"""
import json
import os
import re

from .analyse import load_languages

# The generated language table drops Objective-C (its .m clashes with other languages); iOS code
# needs it, so a few extensions are pinned here.
EXT_OVERRIDES = {"m": "Objective-C", "mm": "Objective-C++", "mdx": "MDX"}
OTHER = "Other"

# ---------------------------------------------------------------------------- building blocks
# Path rules describe *areas of work*, so they carry over between projects. They're checked in the
# order the presets list them: exclusions first, then tests/stories (so a native unit test counts as a
# test), then tooling, then platform code, then assets.

LOCKFILES = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "bun.lockb",
             "Podfile.lock", "Gemfile.lock", "Cargo.lock", "poetry.lock", "composer.lock", "*.lock",
             "go.sum", "*.min.js", "*.min.css", "*.map", "*.snap", "*.generated.*", "**/generated/**",
             "**/__generated__/**", "**/dist/**", "**/build/**", "**/.gradle/**", "**/Pods/**",
             "*.pbxproj", "*.xcworkspacedata", "**/target/**", "*.class", "**/generated-sources/**",
             "**/generated-test-sources/**", "**/out/production/**"]
VENDORED = ["vendor/**", "**/vendor/**", "**/third_party/**", "**/third-party/**", "**/external/**"]
AGENT_CONFIG = [".cursor/**", ".claude/**", ".kiro/**", ".agents/**", ".windsurf/**", ".continue/**",
                ".aider*", "*.mdc", "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md",
                ".vscode/**", ".idea/**", ".fleet/**", ".zed/**", ".mcp.json", ".classpath", ".project",
                ".settings/**", ".springBeans", ".smarttomcat", "*.iml", ".factorypath"]
TESTS = ["**/__tests__/**", "**/__mocks__/**", "**/__fixtures__/**", "**/fixtures/**", "**/test/**",
         "**/tests/**", "**/e2e/**", "**/cypress/**", "**/playwright/**", "**/detox/**", "**/maestro/**",
         "**/androidTest/**", "*.test.*", "*.spec.*", "*_test.*", "test_*.py", "*Tests.swift", "*Test.kt",
         "*Test.java", "*Tests.m", "jest.config.*", "jest.setup.*", "vitest.config.*", "playwright.config.*",
         "cypress.config.*", ".detoxrc*"]
STORIES = ["*.stories.*", "*.story.*", "**/.storybook/**", "**/storybook/**", "**/.ondevice/**"]
CI_TOOLING = [".github/**", ".gitlab/**", ".gitlab-ci.yml", ".circleci/**", ".buildkite/**", "bitrise.yml",
              "azure-pipelines.yml", "ci/**", "Jenkinsfile*", "**/fastlane/**", "Fastfile", "Appfile",
              "Pluginfile", "Matchfile", "Gemfile", "Dangerfile*", ".husky/**", "scripts/**", "tools/**",
              "Makefile", "Dockerfile*", "docker-compose*.yml", "compose*.yml", "**/patches/**", "*.patch",
              "*.diff", "eas.json", "codemagic.yaml", "pre-commit", "pre-push", "post-commit", "post-merge",
              "post-checkout", "commit-msg"]
ANDROID = ["android/**", "**/android/**", "*.kt", "*.kts", "*.java", "*.gradle", "gradle.properties",
           "gradlew*", "proguard-rules.pro", "*.pro", "AndroidManifest.xml"]
IOS = ["ios/**", "**/ios/**", "**/*-ios/**", "**/*_ios/**", "*.swift", "*.m", "*.mm", "*.h", "*.podspec",
       "Podfile", "*.entitlements", "*.storyboard", "*.xib", "*.xcconfig", "*.xcscheme", "*.xcprivacy",
       "Info.plist"]
CONFIG_FILES = [".gitignore", ".*ignore", ".gitattributes", ".editorconfig", ".env", ".env.*", ".*rc",
                ".*rc.{js,cjs,mjs,json,yml,yaml}", ".nvmrc", ".node-version", ".ruby-version", ".python-version",
                ".tool-versions", ".bundle/config", ".watchmanconfig", "*.config.{js,cjs,mjs,ts}"]
ASSETS = ["*.{png,jpg,jpeg,gif,webp,avif,bmp,ico,icns,heic,svg,pdf}", "*.{ttf,otf,woff,woff2,eot}",
          "*.{mp3,wav,aac,m4a,ogg,mp4,mov,webm}", "*.{lottie,riv}", "*.{aar,jar,tgz,zip,keystore,jks,p12}"]

DOCS = {"Markdown": "Docs", "MDX": "Docs", "reStructuredText": "Docs", "Text": "Docs", "AsciiDoc": "Docs"}
CONFIG = {name: "Config" for name in (
    "JSON", "JSON with Comments", "JSON5", "YAML", "TOML", "XML", "XML Property List", "INI", "CSV", "TSV",
    "Ignore List", "Git Config", "Git Attributes", "EditorConfig", "Dotenv", "Java Properties")}
STYLES = {"CSS": "Styles", "SCSS": "Styles", "Sass": "Styles", "Less": "Styles", "Stylus": "Styles",
          "PostCSS": "Styles"}

EXCLUDED_AREAS = ["Generated", "Vendor", "AI & IDE"]

# Backends: build files, runtime config, data dumps, and the code split by layer. Layer rules use
# folder names and file suffixes, so they fit Spring (FooController.java), NestJS (foo.controller.ts),
# .NET (Controllers/), Go and similar layouts. Order matters: models are matched before services so
# a services/**/model/*Response.java counts as a model.
BUILD = ["pom.xml", "mvnw*", ".mvn/**", "build.gradle*", "settings.gradle*", "gradlew*", "gradle/**",
         "*.csproj", "*.sln", "Directory.Build.*", "go.mod", "setenv.sh", "*.sh", "*.bat", "*.cmd", "*.ps1"]
# Data dumps only ever match data files, so code in a "master" or "seed" package stays code.
DATA = ["*master*.{json,csv,tsv,xml,txt,sql,yml,yaml}", "**/*master*/**/*.{json,csv,tsv,xml,txt,sql,yml,yaml}",
        "**/{seed,seeds,seed-data,testdata,dump,dumps}/**/*.{json,csv,tsv,xml,txt,sql,yml,yaml}",
        "*.csv", "*.tsv", "*.ndjson", "*.geojson", "*.sql.gz"]
# Runtime config and key material (matched before Content, so a key JSON isn't "content") ...
BACKEND_CONFIG = ["*.properties", "application*.{yml,yaml,json}", "bootstrap*.{yml,yaml}", "appsettings*.json",
                  "logback*.xml", "log4j*", "web.xml", "server.xml", "context.xml", "persistence.xml",
                  "**/WEB-INF/*.xml", "**/META-INF/**", "**/Keys/**", "**/keys/**", "*.pem", "*.key", "*.crt",
                  "*.jks", "*.p12", "*.config.*"]
# ... and configuration *code*, matched after the API/model/DAO layers.
CONFIG_CODE = ["**/config/**", "**/configuration/**", "*Config.*", "*Configuration.*"]
CONTENT = ["**/resources/**/*.json", "**/static/**/*.json", "**/templates/**", "**/i18n/**", "**/locales/**",
           "messages*.properties"]
MODELS = ["**/model/**", "**/models/**", "**/dto/**", "**/dtos/**", "**/entity/**", "**/entities/**",
          "**/pojo/**", "**/pojos/**", "**/vo/**", "**/bean/**", "**/beans/**", "**/request/**",
          "**/requests/**", "**/response/**", "**/responses/**", "**/enum/**", "**/enums/**", "**/schema/**",
          "**/schemas/**", "*Request.*", "*Response.*", "*Dto.*", "*Entity.*", "*Enum.*", "*Pojo.*",
          "*.dto.*", "*.entity.*", "*.model.*", "*.schema.*", "**/*model/**", "**/*models/**"]
API = ["**/controller/**", "**/controllers/**", "*Controller.*", "*.controller.*", "**/filter/**",
       "**/filters/**", "*Filter.*", "**/interceptor/**", "**/interceptors/**", "*Interceptor.*",
       "**/security/**", "**/routes/**", "**/router/**", "**/handlers/**", "*Handler.*", "*.resolver.*",
       "**/middleware/**", "*.middleware.*", "*.guard.*",
       # JAX-RS / Jersey endpoints live in "resource(s)" packages and classes
       "**/resource/**/*.{java,kt}", "**/resources/**/*.{java,kt}", "*Resource.{java,kt}", "*Resources.{java,kt}"]
DAO = ["**/dao/**", "**/daos/**", "*Dao.*", "**/repository/**", "**/repositories/**", "*Repository.*",
       "*.repository.*", "**/mapper/**", "**/mappers/**", "*Mapper.*", "**/db/**", "**/migration/**",
       "**/migrations/**", "*.sql"]
UTILS = ["**/util/**", "**/utils/**", "*Util.*", "*Utils.*", "**/helper/**", "**/helpers/**", "*Helper.*",
         "**/constant/**", "**/constants/**", "*Constants.*", "**/common/**", "**/exception/**",
         "**/exceptions/**", "*Exception.*"]
SERVICES = ["**/service/**", "**/services/**", "*Service.*", "*Services.*", "*ServiceImpl.*", "*.service.*",
            "**/business/**", "**/usecase/**", "**/usecases/**", "**/client/**", "**/clients/**", "*Client.*",
            "**/scheduler/**", "**/jobs/**", "**/consumer/**", "**/consumers/**", "**/listener/**",
            "**/workers/**", "**/processor/**", "**/processors/**", "*Processor.*", "**/adapter/**",
            "**/adapters/**", "*Adapter.*", "**/cron/**", "**/ingest/**", "**/validator/**", "*Validator.*"]

PRESETS = {
    "backend": {
        "name": "Backend",
        "rules": [
            {"name": "Generated", "patterns": LOCKFILES},
            {"name": "Vendor", "patterns": VENDORED + ["lib/**", "libs/**", "*.jar"]},
            {"name": "AI & IDE", "patterns": AGENT_CONFIG},
            {"name": "Data", "patterns": DATA},
            {"name": "Tests", "patterns": TESTS},
            {"name": "Tooling", "patterns": CI_TOOLING + BUILD},
            {"name": "Assets", "patterns": ASSETS},
            {"name": "Config", "patterns": CONFIG_FILES + BACKEND_CONFIG},
            {"name": "Content", "patterns": CONTENT},
            {"name": "Models", "patterns": MODELS},
            {"name": "API", "patterns": API},
            {"name": "DAO", "patterns": DAO},
            {"name": "Config", "patterns": CONFIG_CODE},
            {"name": "Utils", "patterns": UTILS},
            {"name": "Services", "patterns": SERVICES},
        ],
        "languages": {"Shell": "Tooling", "Batchfile": "Tooling", "PowerShell": "Tooling", "Dockerfile": "Tooling",
                      "Java Server Pages": "Web", "HTML": "Web", "CSS": "Web", "JavaScript": "Web",
                      **DOCS, **CONFIG},
        "exclude": EXCLUDED_AREAS + ["Data"],
    },
    "default": {
        "name": "Default",
        "rules": [
            {"name": "Generated", "patterns": LOCKFILES},
            {"name": "Vendor", "patterns": VENDORED},
            {"name": "AI & IDE", "patterns": AGENT_CONFIG},
            {"name": "Tests", "patterns": TESTS},
            {"name": "Stories", "patterns": STORIES},
            {"name": "Tooling", "patterns": CI_TOOLING},
            {"name": "Config", "patterns": CONFIG_FILES},
            {"name": "Assets", "patterns": ASSETS},
        ],
        "languages": {"TSX": "TypeScript", "JSX": "JavaScript", "Objective-C++": "Objective-C",
                      **DOCS, **CONFIG, **STYLES},
        "exclude": EXCLUDED_AREAS,
    },
    "react-native": {
        "name": "React Native",
        "rules": [
            {"name": "Generated", "patterns": LOCKFILES},
            {"name": "Vendor", "patterns": VENDORED},
            {"name": "AI & IDE", "patterns": AGENT_CONFIG},
            {"name": "Tests", "patterns": TESTS},
            {"name": "Stories", "patterns": STORIES},
            {"name": "Tooling", "patterns": CI_TOOLING},
            {"name": "Config", "patterns": CONFIG_FILES},
            # Android and iOS share one "Native" group for now; split by giving each its own rule
            # ({"name": "Android", "patterns": ANDROID}, {"name": "iOS", "patterns": IOS}).
            {"name": "Native", "patterns": ANDROID + IOS},
            {"name": "Assets", "patterns": ASSETS},
        ],
        "languages": {"TypeScript": "TS", "TSX": "TS", "JavaScript": "JS", "JSX": "JS",
                      # scripting languages in an RN app are almost always build/release tooling
                      "Shell": "Tooling", "Python": "Tooling", "Ruby": "Tooling",
                      "Groovy": "Tooling", "PowerShell": "Tooling", "Diff": "Tooling",
                      **DOCS, **CONFIG, **STYLES},
        "exclude": EXCLUDED_AREAS,
    },
}


# ---------------------------------------------------------------------------- profiles

def clean_profile(data):
    """Validates a profile (from a file, a preset or the web page) and returns a normalised copy."""
    if not isinstance(data, dict):
        raise ValueError("file groups must be a JSON object with 'rules', 'languages' and/or 'exclude'")
    rules = data.get("rules") or []
    languages = data.get("languages") or {}
    exclude = data.get("exclude") or []
    if not isinstance(rules, list) or not isinstance(languages, dict) or not isinstance(exclude, list):
        raise ValueError("'rules' must be a list, 'languages' an object and 'exclude' a list")
    clean_rules = []
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("each rule must be an object with 'name' and 'patterns'")
        name = str(r.get("name") or "").strip()
        pats = r.get("patterns") or []
        if isinstance(pats, str):
            pats = re.split(r"[,\n]", pats)
        pats = [str(p).strip() for p in pats if str(p).strip()]
        if name and pats:
            clean_rules.append({"name": name, "patterns": pats})
    return {
        "name": str(data.get("name") or "Custom").strip() or "Custom",
        "rules": clean_rules,
        "languages": {str(k): str(v).strip() for k, v in languages.items() if str(v).strip() and str(v).strip() != str(k)},
        "exclude": sorted({str(x) for x in exclude}),
    }


def load_profile(value):
    """A profile from a JSON file path or a built-in preset id ("default", "react-native")."""
    if value in PRESETS:
        return clean_profile(PRESETS[value])
    with open(value, encoding="utf-8") as f:
        return clean_profile(json.load(f))


def save_profile(path, profile):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_profile(profile), f, indent=2, ensure_ascii=False)
        f.write("\n")


def detect_preset(files):
    """Picks a preset from the repository's file list: React Native when there's a JS package
    alongside android/ and ios/ folders; Backend for JVM/.NET/Go services; otherwise the default."""
    has = lambda prefix: any(f.startswith(prefix) for f in files)
    if "package.json" in files and has("android/") and has("ios/"):
        return "react-native"
    backend_markers = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "go.mod"}
    if backend_markers & set(files) or any(f.endswith((".csproj", ".sln")) and "/" not in f for f in files):
        return "backend"
    return "default"


# ---------------------------------------------------------------------------- matching

def glob_regex(pattern):
    """Compiles a glob (see module docstring) to a regex matched against a path or a file name."""
    p = pattern.strip()
    if p.startswith("./"):
        p = p[2:]
    if p.endswith("/"):
        p += "**"
    out, i = [], 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        elif p[i] == "{" and "}" in p[i:]:
            j = p.index("}", i)
            out.append("(?:" + "|".join(re.escape(x) for x in p[i + 1:j].split(",")) + ")")
            i = j + 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out), re.I), "/" in p


class Classifier:
    """Maps repository paths to group names for one profile (results are cached per path)."""

    def __init__(self, profile):
        self.profile = clean_profile(profile)
        self.rules = [(r["name"], [glob_regex(p) for p in r["patterns"]]) for r in self.profile["rules"]]
        self.languages = self.profile["languages"]
        self.exclude = set(self.profile["exclude"])
        self._ext = None
        self._cache = {}

    def language(self, path):
        if self._ext is None:
            try:
                self._ext = load_languages()
            except OSError:
                self._ext = {}
        ext = os.path.splitext(path)[1][1:]
        return EXT_OVERRIDES.get(ext.lower()) or self._ext.get(ext) or self._ext.get(ext.lower()) or OTHER

    def classify(self, path):
        """Returns (group, rule_name or None, language or None)."""
        if path in self._cache:
            return self._cache[path]
        name = path.rsplit("/", 1)[-1]
        result = None
        for rule, patterns in self.rules:
            if any(rx.fullmatch(path if full else name) for rx, full in patterns):
                result = (rule, rule, None)
                break
        if result is None:
            lang = self.language(path)
            result = (self.languages.get(lang, lang), None, lang)
        self._cache[path] = result
        return result

    def group(self, path):
        return self.classify(path)[0]

    def excluded(self, path):
        return self.classify(path)[0] in self.exclude


def breakdown(commits, files, classifier):
    """Lines changed and files touched per group, rule and language, for the preview and the designs.

    `commits` are analysis commits ({"files": [{"File", "Additions", "Deletions"}]}).
    """
    groups, rules, langs = {}, {}, {}
    seen = set()
    for c in commits:
        for f in c["files"]:
            path = files[f["File"]]
            group, rule, lang = classifier.classify(path)
            lines = f["Additions"] + f["Deletions"]
            first = f["File"] not in seen
            seen.add(f["File"])
            for table, key in ((groups, group), (rules, rule), (langs, lang)):
                if key is None:
                    continue
                entry = table.setdefault(key, {"lines": 0, "files": 0})
                entry["lines"] += lines
                entry["files"] += first
    return groups, rules, langs
