#!/usr/bin/env python3
"""versiontool - find the repo's own release machinery before inventing any.

  versiontool.py discover              what this repo already provides
  versiontool.py locations <version>   files holding a version string (fallback)
  versiontool.py verify <old-version>  fail if any stale version survives

`discover` exists because the right answer is almost always "the repo already has
a bumper". A hand-rolled search edits files the repo's own task would have
handled, misses the ones it knows about, and skips its gate.

`verify` runs even when a repo task did the bump: a bumper written six months ago
does not know about the file someone added last week.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

# Lockfiles hold the version but are generated; hand-editing one passes review
# and then fails `--locked` in CI.
REGENERATE = {
    "Cargo.lock": "cargo update -w",
    "package-lock.json": "npm install --package-lock-only",
    "pnpm-lock.yaml": "pnpm install --lockfile-only",
    "yarn.lock": "yarn install --mode update-lockfile",
    "poetry.lock": "poetry lock --no-update",
    "uv.lock": "uv lock",
    "Gemfile.lock": "bundle install",
    "composer.lock": "composer update --lock",
}
SKIP_PATHS = ("CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md", "NEWS.md", "HISTORY.md")
RELEASEY = re.compile(r"release|bump|version|publish|tag", re.I)


def sh(argv, cwd=None):
    p = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout


def emit(**kv):
    """KV line. Values with spaces are quoted so the output stays parseable."""
    parts = []
    for k, v in kv.items():
        v = str(v)
        parts.append(f'{k}="{v}"' if " " in v else f"{k}={v}")
    print(" ".join(parts))


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------
def discover(root):
    root = pathlib.Path(root)
    found = []

    def add(kind, name, path, hint=""):
        found.append({"kind": kind, "name": name, "path": path, "hint": hint})

    # A project skill outranks everything: it is this repo's own procedure.
    for p in sorted(root.glob(".claude/skills/*/SKILL.md")):
        if RELEASEY.search(p.parent.name):
            add("project-skill", p.parent.name, str(p.relative_to(root)),
                "follow this instead of the generic procedure")

    for p in sorted(root.glob("mise-tasks/*")):
        if p.is_file() and RELEASEY.search(p.name):
            add("mise-task", p.name, str(p.relative_to(root)), f"mise run {p.name}")
    for cfg in ("mise.toml", ".mise.toml", ".config/mise.toml"):
        f = root / cfg
        if f.is_file():
            for m in re.finditer(r"^\[tasks\.([\w:-]+)\]", f.read_text(), re.M):
                if RELEASEY.search(m.group(1)):
                    add("mise-task", m.group(1), cfg, f"mise run {m.group(1)}")

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except (json.JSONDecodeError, OSError):
            scripts = {}
        for name in scripts:
            if RELEASEY.search(name):
                add("npm-script", name, "package.json", f"npm run {name}")

    mk = root / "Makefile"
    if mk.is_file():
        for m in re.finditer(r"^([\w.-]+):(?!=)", mk.read_text(), re.M):
            if RELEASEY.search(m.group(1)):
                add("make-target", m.group(1), "Makefile", f"make {m.group(1)}")

    for jf in ("justfile", "Justfile", ".justfile"):
        f = root / jf
        if f.is_file():
            for m in re.finditer(r"^([\w-]+)(?:\s[^:=]*)?:(?!=)", f.read_text(), re.M):
                if RELEASEY.search(m.group(1)):
                    add("just-recipe", m.group(1), jf, f"just {m.group(1)}")

    for d in ("scripts", "bin", "tools"):
        for p in sorted((root / d).glob("*")) if (root / d).is_dir() else []:
            if p.is_file() and RELEASEY.search(p.name):
                add("script", p.name, str(p.relative_to(root)), f"./{p.relative_to(root)}")

    for cfg, tool in (("release.toml", "cargo-release"),
                      (".bumpversion.cfg", "bumpversion"),
                      (".bumpversion.toml", "bump-my-version"),
                      (".versionrc", "standard-version"),
                      (".release-it.json", "release-it"),
                      ("release-please-config.json", "release-please"),
                      ("cliff.toml", "git-cliff (changelog)")):
        if (root / cfg).is_file():
            add("tool-config", tool, cfg, f"repo is configured for {tool}")

    for f in found:
        emit(**f)
    if not found:
        emit(result="none", hint="no repo release machinery found; fall back to 'locations'")
        return 1
    emit(result="found", count=len(found))
    return 0


# ---------------------------------------------------------------------------
# locations / verify
# ---------------------------------------------------------------------------
def grep_version(version, root):
    """Literal search, because keys are not always spelled 'version'.

    Measured on a real repo: a `version`-key search found 5 files, the literal
    version string found 8. The misses spelled it "version-name" and "Version"."""
    rc, out = sh(["git", "grep", "-n", "--fixed-strings", version], cwd=root)
    if rc not in (0, 1):
        raise SystemExit(f"versiontool: git grep failed in {root}")
    hits = []
    for line in out.splitlines():
        path, _, rest = line.partition(":")
        hits.append((path, rest))
    return hits


def classify(path):
    name = pathlib.Path(path).name
    if name in REGENERATE:
        return "regenerate", REGENERATE[name]
    if name in SKIP_PATHS:
        return "changelog", "handled as the changelog, not a bump"
    return "edit", ""


def locations(version, root):
    seen = {}
    for path, _ in grep_version(version, root):
        seen.setdefault(path, 0)
        seen[path] += 1
    if not seen:
        emit(result="none", version=version)
        return 1
    for path in sorted(seen):
        kind, hint = classify(path)
        emit(kind=kind, path=path, hits=seen[path], **({"hint": hint} if hint else {}))
    emit(result="found", files=len(seen))
    return 0


def verify(version, root):
    stale = [(p, t) for p, t in grep_version(version, root)
             if classify(p)[0] == "edit"]
    if stale:
        for path, text in stale:
            emit(stale=path, line=text.strip()[:80])
        emit(result="STALE", files=len({p for p, _ in stale}))
        return 1
    emit(result="clean", version=version)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="versiontool.py")
    p.add_argument("--dir", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    for name in ("locations", "verify"):
        s = sub.add_parser(name)
        s.add_argument("version")
    args = p.parse_args(argv)

    if args.cmd == "discover":
        return discover(args.dir)
    if args.cmd == "locations":
        return locations(args.version, args.dir)
    return verify(args.version, args.dir)


if __name__ == "__main__":
    sys.exit(main())
