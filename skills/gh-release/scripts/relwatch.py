#!/usr/bin/env python3
"""relwatch - check that a tag will trigger a release, then watch until it does.

  relwatch.py preflight <tag> [--dir .]     which workflows the tag would trigger
  relwatch.py watch <tag> [options]         poll until the release exists

preflight answers the question that costs the most time when it is skipped: a tag
that matches no `on.push.tags` pattern triggers nothing at all, and the failure
looks exactly like a slow CI queue.

watch verdicts (last line on stdout):
  verdict=released url=...
  verdict=ci_failed run=... conclusion=...
  verdict=no_run elapsed=...        nothing was triggered; check the tag pattern
  verdict=still_running run=...     not a failure, watch again
Exit: 0 released, 1 ci_failed, 2 no_run, 3 still_running.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import time


class ReleaseError(RuntimeError):
    pass


def run(argv):
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise ReleaseError(f"{argv[0]} failed: {(p.stderr + p.stdout).strip()[:400]}")
    return p.stdout


def gh_json(path, repo):
    return json.loads(run(["gh", "api", f"repos/{repo}/{path}"]) or "{}")


def repo_slug(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("GH_REPO"):
        return os.environ["GH_REPO"]
    url = run(["git", "remote", "get-url", "origin"]).strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise ReleaseError(f"cannot derive owner/repo from {url}")
    return m.group(1)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
def tag_patterns(text):
    """Extract `on.push.tags` globs. Hand-rolled because pyyaml is not a given
    on this machine and the shape we need is tiny and stable."""
    patterns, in_push, push_indent, in_tags, tags_indent = [], False, -1, False, -1
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        if in_tags:
            if line.startswith("- ") and indent > tags_indent:
                patterns.append(line[2:].strip().strip("'\""))
                continue
            in_tags = False
        if in_push and indent <= push_indent:
            in_push = False
        if re.match(r"^push:\s*$", line):
            in_push, push_indent = True, indent
            continue
        if in_push and line.startswith("tags:"):
            inline = line[len("tags:"):].strip()
            if inline.startswith("["):
                patterns += [x.strip().strip("'\"")
                             for x in inline[1:-1].split(",") if x.strip()]
            else:
                in_tags, tags_indent = True, indent
    return patterns


def creates_release(text):
    return bool(re.search(r"action-gh-release|gh release create|softprops|"
                          r"create-release", text))


def preflight(tag, workflow_dir):
    d = pathlib.Path(workflow_dir) / ".github" / "workflows"
    if not d.is_dir():
        raise ReleaseError(f"no {d}")
    matched, seen_any = [], []
    for path in sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml"))):
        text = path.read_text()
        pats = tag_patterns(text)
        if not pats:
            continue
        seen_any.append((path.name, pats))
        if any(fnmatch.fnmatch(tag, p) for p in pats):
            matched.append((path.name, pats, creates_release(text)))

    print(f"tag={tag}")
    for name, pats in seen_any:
        print(f"workflow={name} patterns={','.join(pats)} "
              f"matches={'yes' if any(fnmatch.fnmatch(tag, p) for p in pats) else 'no'}")
    if not seen_any:
        print("result=no_tag_triggered_workflows")
        return 2
    if not matched:
        print("result=NO_MATCH")
        print(f"hint=tag {tag!r} matches no pattern; pushing it triggers nothing")
        return 2
    if not any(rel for _, _, rel in matched):
        print("result=match_but_no_release_step")
        print("hint=a workflow triggers, but none of them looks like it creates a release")
        return 2
    print("result=ok")
    return 0


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------
def find_run(repo, tag):
    """A tag push shows up with head_branch set to the tag name."""
    data = gh_json("actions/runs?per_page=50", repo)
    for r in data.get("workflow_runs", []):
        if r.get("head_branch") == tag:
            return r
    return None


def released(repo, tag):
    try:
        rel = gh_json(f"releases/tags/{tag}", repo)
    except ReleaseError:
        return None
    return rel.get("html_url") if rel.get("tag_name") == tag else None


def watch(args):
    repo = repo_slug(args.repo)
    start = time.time()
    say = lambda m: print(f"relwatch: {m}", file=sys.stderr, flush=True)
    say(f"watching {repo}@{args.tag} (timeout={args.timeout}s interval={args.interval}s)")

    while True:
        elapsed = int(time.time() - start)

        url = released(repo, args.tag)
        if url:
            return f"verdict=released url={url} elapsed={elapsed}s", 0

        run_info = find_run(repo, args.tag)
        if run_info is None:
            # A tag matching no `on.push.tags` pattern looks exactly like a slow
            # queue, so call it rather than waiting out the whole timeout.
            if elapsed >= args.grace:
                return (f"verdict=no_run elapsed={elapsed}s "
                        f"hint=check_tag_pattern_with_preflight"), 2
            say(f"t+{elapsed}s no run yet")
        else:
            status = run_info.get("status")
            conclusion = run_info.get("conclusion")
            if status == "completed" and conclusion != "success":
                return (f"verdict=ci_failed run={run_info.get('html_url')} "
                        f"conclusion={conclusion}"), 1
            if status == "completed" and conclusion == "success":
                # Green but no release yet: the publish step may lag a moment.
                say(f"t+{elapsed}s ci green, waiting for the release to appear")
            else:
                say(f"t+{elapsed}s run {status}")

        if elapsed >= args.timeout:
            r = run_info or {}
            return (f"verdict=still_running elapsed={elapsed}s "
                    f"run={r.get('html_url', 'none')} status={r.get('status', 'none')}"), 3
        time.sleep(args.interval)


def build_parser():
    p = argparse.ArgumentParser(prog="relwatch.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight")
    pf.add_argument("tag")
    pf.add_argument("--dir", default=".")

    w = sub.add_parser("watch")
    w.add_argument("tag")
    w.add_argument("--repo")
    w.add_argument("--timeout", type=int, default=1800)
    w.add_argument("--interval", type=int, default=20)
    w.add_argument("--grace", type=int, default=90,
                   help="seconds to allow before calling a missing run 'no_run'")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "preflight":
        return preflight(args.tag, args.dir)
    line, code = watch(args)
    print(line)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as exc:
        print(f"relwatch: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
