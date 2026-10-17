#!/usr/bin/env python3
"""prctl - forge-agnostic pull/merge request primitives. See references/forges.md.

usage: prctl.py [--repo SLUG] [--as NAME|--rotate] <command> [args]

Every command that names a PR requires --repo (or BABYSIT_REPO): a number alone
is not an address, and the working directory is not a statement of intent.

Env: BABYSIT_REPO    target repo, same value as --repo
     BABYSIT_REMOTE  read the forge from this remote (default origin)
     BABYSIT_FORGE   github|gitlab, skips detection
     BABYSIT_CREDENTIALS  credentials file (default ~/.claude/babysit-pr/credentials)

Every request is addressed explicitly, so GH_REPO does not need to be exported.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

from forge import AddressError, ForgeError, PaidActionRefused, detect

# An allowlist, not a template. Every phrase here is free; nothing that bills is
# reachable through `trigger`, whatever a rate-limit message suggests.
TRIGGERS = {
    "coderabbit": "@coderabbitai review",
    "coderabbit-full": "@coderabbitai full review",
}


def emit_kv(mapping):
    for key, value in mapping.items():
        print(f"{key}={value}")


def emit_ndjson(rows):
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


# A reply that is nothing but a filesystem path is always a mistake: the author
# wrote the text to a file and passed the path where the body was wanted. It
# posted `/tmp/.../scratchpad/reply1.md` to a real PR twice before this existed,
# which the reviewer could not read and which leaked local paths into the thread.
BARE_PATH = re.compile(r"^\s*[~.]{0,2}/[^\s]+\s*$")


def read_body(value, body_file=None):
    if body_file:
        return pathlib.Path(body_file).read_text()
    if value == "-":
        return sys.stdin.read()
    if value is None:
        raise ForgeError("a body is required: pass text, '-' for stdin, or --body-file")
    if BARE_PATH.match(value):
        exists = " (it exists on disk)" if pathlib.Path(value.strip()).exists() else ""
        raise ForgeError(
            f"refusing to post {value.strip()!r} as a comment body{exists}: that is a "
            "path, not prose. Use --body-file to post its contents, or '-' for stdin.")
    return value


def reviewer_busy(forge, pr):
    """Why not to ask, or None to go ahead.

    A failed check does not block the request: a status we cannot read is not
    evidence of a review running, and refusing on it would stall the loop."""
    try:
        head = forge.state(pr)["head"]
        st = forge.reviewer_status(head)
    except ForgeError as exc:
        print(f"prctl: could not read the reviewer's status ({exc}); asking anyway",
              file=sys.stderr)
        return None
    if st.stuck:
        print(f"prctl: reviewer status on {head[:12]} has said '{st.state}' since "
              f"{st.at:%H:%M} - treating it as dead, not as busy", file=sys.stderr)
    if not st.busy:
        return None
    return (f"skipped: the reviewer is already reviewing {head[:12]} "
            f"({st.state} since {st.at:%H:%M:%S}, '{st.description}'). "
            "Watch for its verdict instead.")


def build_parser():
    p = argparse.ArgumentParser(prog="prctl.py", add_help=True)
    p.add_argument("--repo", metavar="SLUG",
                   help="host/owner/name, owner/name, or a remote url. Required "
                        "for every command that names a PR.")
    p.add_argument("--as", dest="as_cred", metavar="NAME")
    p.add_argument("--rotate", action="store_true")
    p.add_argument("--allow-paid", action="store_true",
                   help="permit posting text that spends credits. ONLY with the "
                        "user's explicit authorisation for this specific spend; "
                        "never because a bot suggested it.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, *, pr=True):
        s = sub.add_parser(name)
        if pr:
            s.add_argument("pr", type=int)
        return s

    sub.add_parser("detect")
    sub.add_parser("creds")
    s = sub.add_parser("find"); s.add_argument("branch")

    s = sub.add_parser("create")
    s.add_argument("--head", required=True)
    s.add_argument("--base", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file")
    s.add_argument("--draft", action="store_true")

    cmd("state")
    s = cmd("threads"); s.add_argument("--unresolved", action="store_true")
    cmd("reviews")
    cmd("approvals")
    cmd("body")
    s = cmd("set-body"); s.add_argument("body", nargs="?"); s.add_argument("--body-file")
    s.add_argument("--drop-bot-sections", action="store_true",
                   help="do NOT carry the reviewer's own sections across (destructive)")
    cmd("rebase")
    cmd("refresh-diff")

    s = cmd("reply"); s.add_argument("thread_id")
    s.add_argument("body", nargs="?"); s.add_argument("--body-file")
    s = cmd("resolve"); s.add_argument("thread_id")
    s = cmd("comment"); s.add_argument("body", nargs="?"); s.add_argument("--body-file")
    s = cmd("trigger"); s.add_argument("reviewer", nargs="?", default="coderabbit")
    s.add_argument("--force", action="store_true",
                   help="ask even though the reviewer's status says it is already "
                        "reviewing this head")
    cmd("review-status")

    s = cmd("review-comment")
    s.add_argument("--file", required=True)
    s.add_argument("--line", required=True, type=int)
    s.add_argument("--body")
    s.add_argument("--body-file")

    s = cmd("merge")
    s.add_argument("--squash", action="store_true")
    s.add_argument("--keep-branch", action="store_true")
    return p


# A number resolves against whatever repo the process happens to stand in, and a
# subagent does not inherit a working directory from the prose that spawned it.
# Two watchers reported a merge for PRs in unrelated repos that way, and under
# `merge: auto` that verdict ends the job.
def require_address(cmd, args, forge):
    if getattr(args, "pr", None) is None and cmd != "create":
        return
    if args.repo or os.environ.get("BABYSIT_REPO"):
        return
    raise AddressError(
        f"{cmd} names a pull request, so it needs an explicit repo: a number is "
        f"not an address. Pass --repo {forge.slug} if that is the one you mean.")


def main(argv=None):
    args = build_parser().parse_args(argv)
    f = detect(args.repo)
    require_address(args.cmd, args, f)

    if args.as_cred:
        print(f"prctl: acting as '{f.use_credential(args.as_cred)}'", file=sys.stderr)
    elif args.rotate:
        print(f"prctl: acting as '{f.rotate_credential()}'", file=sys.stderr)

    cmd = args.cmd

    if cmd == "detect":
        emit_kv(f.describe())
        return 0

    if cmd == "creds":
        from forge import load_creds
        for name, _ in load_creds(f.host):
            print(name)
        return 0

    if cmd == "find":
        got = f.find(args.branch)
        print(got if got else "")
        return 0

    if cmd == "create":
        body = read_body(args.body, args.body_file)
        print(f.create(args.head, args.base, args.title, body, args.draft))
        return 0

    if cmd == "state":
        emit_kv(f.state(args.pr))
        return 0

    if cmd == "threads":
        emit_ndjson(f.threads(args.pr, unresolved_only=args.unresolved))
        return 0

    if cmd == "reviews":
        emit_ndjson(f.reviews(args.pr))
        return 0

    if cmd == "approvals":
        emit_ndjson(f.approvals(args.pr))
        return 0

    if cmd == "reply":
        f.reply(args.pr, args.thread_id, read_body(args.body, args.body_file),
                args.allow_paid)
        print("replied")
        return 0

    if cmd == "resolve":
        f.resolve(args.pr, args.thread_id)
        print("resolved")
        return 0

    if cmd == "comment":
        f.comment(args.pr, read_body(args.body, args.body_file), args.allow_paid)
        print("posted")
        return 0

    if cmd == "trigger":
        phrase = TRIGGERS.get(args.reviewer)
        if not phrase:
            raise ForgeError(f"no trigger phrase known for '{args.reviewer}'")
        if not args.force:
            busy = reviewer_busy(f, args.pr)
            if busy:
                print(busy)
                return 0
        f.comment(args.pr, phrase)
        print(f"triggered {args.reviewer}")
        return 0

    if cmd == "review-status":
        head = f.state(args.pr)["head"]
        st = f.reviewer_status(head)
        emit_kv({"sha": head, "state": st.state, "busy": str(st.busy).lower(),
                 "stuck": str(st.stuck).lower(), "description": st.description,
                 "at": st.at.isoformat() if st.at else ""})
        return 0

    if cmd == "review-comment":
        if not (args.body or args.body_file):
            raise ForgeError("review-comment: --body or --body-file required")
        body = read_body(args.body, args.body_file)
        f.review_comment(args.pr, args.file, args.line, body, args.allow_paid)
        print("posted")
        return 0

    if cmd == "body":
        print(f.body(args.pr), end="")
        return 0

    if cmd == "set-body":
        kept = f.set_body(args.pr, read_body(args.body, args.body_file),
                          keep_bot_sections=not args.drop_bot_sections)
        print(f"updated" + (f" (kept {kept} reviewer section(s))" if kept else ""))
        return 0

    if cmd == "rebase":
        print(f.rebase(args.pr))
        return 0

    if cmd == "refresh-diff":
        print(f.refresh_diff(args.pr))
        return 0

    if cmd == "merge":
        print("MERGED", f.merge(args.pr, args.squash, args.keep_branch))
        return 0

    raise ForgeError(f"unknown command: {cmd}")


def cli(argv=None):
    """Exit codes are the contract for a caller that only sees a status:
    3 refused a spend, 64 bad usage, 1 the forge said no."""
    try:
        sys.exit(main(argv))
    except PaidActionRefused as exc:
        print(f"prctl: {exc}", file=sys.stderr)
        sys.exit(3)
    except AddressError as exc:
        print(f"prctl: {exc}", file=sys.stderr)
        sys.exit(64)
    except ForgeError as exc:
        print(f"prctl: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
