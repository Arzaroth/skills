#!/usr/bin/env python3
"""docscheck - work out which docs the recent changes should have touched.

  docscheck.py scope [--since REF|--full]   resolve the change range and its files
  docscheck.py markers                      docs that record their own last-updated point
  docscheck.py impacted [--since REF]       docs that cite the code that changed
  docscheck.py links                        relative links and cited paths that no longer resolve

`impacted` is the point of this tool: instead of rereading every doc, find the
docs that name the files you changed. A doc that cites a path you touched is the
doc most likely to be describing something that is no longer true.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

DOC_GLOBS = ("*.md", "docs/**/*.md", "brain/**/*.md", "doc/**/*.md",
             "documentation/**/*.md", "*.mdx", "docs/**/*.mdx")
EXCLUDE = re.compile(r"(^|/)(node_modules|target|dist|build|vendor|\.git|"
                     r"\.venv|__pycache__)(/|$)")

# A doc that records where it was last brought up to date lets us scope to that
# point instead of guessing. These are the spellings seen in the wild.
MARKER = re.compile(
    r"(?:last[-_ ]?updated|last[-_ ]?reviewed|as[-_ ]of|up[-_ ]to[-_ ]date[-_ ]as[-_ ]of)"
    r"\s*[:=]?\s*[\"'<]?\s*([0-9a-f]{7,40}|\d{4}-\d{2}-\d{2}|v?\d+\.\d+\.\d+)",
    re.I)

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Docs that cite paths which are meant not to exist: history (the file was
# deleted after that entry) and plans (the file is not written yet).
NARRATIVE = re.compile(r"(^|/)(CHANGELOG|CHANGES|NEWS|HISTORY|TODO|ROADMAP)"
                       r"|(^|/)changelogs?/", re.I)
# Brace expansion, placeholders and globs are shorthand for several files, not a
# path: `use{Cabinet,Showcase}.ts`, `use<Feature>.ts`, `_nuxt/*.js`.
NOT_A_PATH = re.compile(r"[{}<>*?\[\]$]")
# `drizzle/NNNN_name.sql`, `XXXX-migration.sql`: a run of repeated capitals is a
# fill-in-the-blank, not a filename.
PLACEHOLDER = re.compile(r"([A-Z])\1{2,}")
CODEISH = re.compile(r"\.(py|js|ts|tsx|jsx|rs|go|rb|java|kt|c|h|cc|cpp|cs|php|"
                     r"sh|bash|vue|svelte|sql|toml|ya?ml|json|mjs|cjs)$")
INLINE_PATH = re.compile(r"`([^`\n]*/[^`\n]*)`")


def sh(argv, cwd=None):
    p = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout.strip()


def emit(**kv):
    print(" ".join(f'{k}="{v}"' if " " in str(v) else f"{k}={v}"
                   for k, v in kv.items()))


def tracked_files(root):
    rc, out = sh(["git", "ls-files"], cwd=root)
    return set(out.splitlines()) if rc == 0 else set()


def resolves(cand, root, base, tracked):
    """A doc may cite a path relative to the repo, to itself, or to a package
    root in a monorepo (`server/api/x.ts` under `apps/web-nuxt/`). Accept any."""
    if (pathlib.Path(root) / cand).exists() or (base / cand).exists():
        return True
    return cand in tracked or any(f.endswith("/" + cand) for f in tracked)


def tracked_docs(root):
    rc, out = sh(["git", "ls-files", *DOC_GLOBS], cwd=root)
    if rc != 0:
        return []
    return [p for p in out.splitlines() if p and not EXCLUDE.search(p)]


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------
def resolve_range(root, since=None, full=False):
    """Return (range, label). Precedence: explicit ref, then the last merge, then
    the last commit. A merge is preferred because 'what changed' is usually a
    branch's worth of work, not one commit."""
    if full:
        return None, "full"
    if since:
        return f"{since}..HEAD", f"since {since}"

    rc, out = sh(["git", "log", "--merges", "-1", "--format=%H"], cwd=root)
    if rc == 0 and out:
        merge = out
        rc2, parents = sh(["git", "log", "-1", "--format=%P", merge], cwd=root)
        if rc2 == 0 and len(parents.split()) >= 2:
            first_parent = parents.split()[0]
            return f"{first_parent}..{merge}", f"last merged branch ({merge[:9]})"

    rc, out = sh(["git", "log", "-1", "--format=%H"], cwd=root)
    if rc == 0 and out:
        return f"{out}~1..{out}", f"last commit ({out[:9]})"
    return None, "empty repo"


def changed_files(root, rng):
    if rng is None:
        rc, out = sh(["git", "ls-files"], cwd=root)
    else:
        rc, out = sh(["git", "diff", "--name-only", rng], cwd=root)
    if rc != 0:
        return []
    return [p for p in out.splitlines() if p and not EXCLUDE.search(p)]


def is_doc(path):
    return path.endswith((".md", ".mdx"))


def cmd_scope(args):
    rng, label = resolve_range(args.dir, args.since, args.full)
    files = changed_files(args.dir, rng)
    code = [f for f in files if not is_doc(f)]
    docs = [f for f in files if is_doc(f)]
    emit(scope=label, range=rng or "(all tracked)",
         changed=len(files), code=len(code), docs=len(docs))
    for f in code:
        emit(kind=("code" if not is_doc(f) else "doc"), path=f)
    for f in docs:
        emit(kind="doc-already-touched", path=f)
    return 0 if files else 1


# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------
def cmd_markers(args):
    root = pathlib.Path(args.dir)
    found = 0
    for rel in tracked_docs(args.dir):
        try:
            head = (root / rel).read_text(errors="replace")[:4000]
        except OSError:
            continue
        m = MARKER.search(head)
        if m:
            found += 1
            emit(doc=rel, marker=m.group(1))
    emit(result="found" if found else "none", docs=found,
         hint="" if found else "no doc records its own last-updated point; scope from git")
    return 0 if found else 1


# ---------------------------------------------------------------------------
# impacted
# ---------------------------------------------------------------------------
def cmd_impacted(args):
    root = pathlib.Path(args.dir)
    rng, label = resolve_range(args.dir, args.since, args.full)
    changed = [f for f in changed_files(args.dir, rng) if not is_doc(f)]
    if not changed:
        emit(result="no_code_changes", scope=label)
        return 1

    # Full-path matches only. Matching the parent directory as well sounds
    # generous and is useless: on a monorepo `apps/web-nuxt/` appears in 55 of 62
    # docs, so every change impacts everything and the signal is gone.
    hits = {}
    for rel in tracked_docs(args.dir):
        try:
            text = (root / rel).read_text(errors="replace")
        except OSError:
            continue
        for f in changed:
            if f in text:
                hits.setdefault(rel, []).append(f)

    emit(scope=label, range=rng or "(all)", changed_code=len(changed),
         impacted_docs=len(hits))
    for doc in sorted(hits):
        emit(doc=doc, cites=len(hits[doc]), files=",".join(sorted(hits[doc])[:6]))
    if not hits:
        emit(result="none",
             hint="no doc names the changed files; judge by subject, not by path")
        return 1
    emit(result="found")
    return 0


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------
def check_doc_links(root, rel, tracked=None):
    """Relative markdown links, plus inline-code strings that look like repo
    paths. The second kind is skipped in narrative docs, where a path that does
    not resolve is usually correct rather than rotten."""
    tracked = tracked if tracked is not None else tracked_files(root)
    path = pathlib.Path(root) / rel
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    base = path.parent
    broken = []
    for n, line in enumerate(lines, 1):
        for target in LINK.findall(line):
            if re.match(r"^(https?:|mailto:|#|<)", target):
                continue
            clean = target.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            clean = clean[2:] if clean.startswith("./") else clean
            if not (base / clean).exists():
                broken.append((n, clean, "link"))
        if NARRATIVE.search(rel):
            continue
        for cand in INLINE_PATH.findall(line):
            cand = cand.strip()
            if not CODEISH.search(cand) or cand.startswith(("http", "/", "~", "$")):
                continue
            cand = cand[2:] if cand.startswith("./") else cand
            if " " in cand or cand.count("/") > 8 or NOT_A_PATH.search(cand):
                continue
            if PLACEHOLDER.search(cand):
                continue
            if resolves(cand, root, base, tracked):
                continue
            broken.append((n, cand, "source"))
    return broken


def drop_ignored(root, findings):
    """Build output (`.output/`, `.nuxt/`) is cited legitimately and never exists
    in a clean checkout. Ask git what is ignored rather than guessing at dotdirs,
    which would also throw away `.github/workflows/*.yml`."""
    cands = sorted({f[1] for f in findings})
    if not cands:
        return findings
    p = subprocess.run(["git", "check-ignore", "--stdin"], cwd=root,
                       input="\n".join(cands), capture_output=True, text=True)
    ignored = set(p.stdout.split())
    return [f for f in findings if f[1] not in ignored]


def cmd_links(args):
    docs = [args.docs] if args.docs else tracked_docs(args.dir)
    tracked = tracked_files(args.dir)
    findings = []
    for rel in docs:
        for line, target, kind in check_doc_links(args.dir, rel, tracked):
            findings.append((rel, target, line, kind))
    findings = drop_ignored(args.dir, findings)
    for rel, target, line, kind in findings:
        emit(kind=kind, doc=rel, line=line, target=target)
    emit(result="broken" if findings else "clean", count=len(findings))
    return 1 if findings else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="docscheck.py")
    p.add_argument("--dir", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("scope", "impacted"):
        s = sub.add_parser(name)
        s.add_argument("--since")
        s.add_argument("--full", action="store_true")
    sub.add_parser("markers")
    s = sub.add_parser("links")
    s.add_argument("--docs", help="check one doc instead of all tracked docs")
    args = p.parse_args(argv)

    return {"scope": cmd_scope, "markers": cmd_markers,
            "impacted": cmd_impacted, "links": cmd_links}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
