#!/usr/bin/env python3
"""Read, flip and restore a project's squash-on-merge setting, crash-safely.

The point of the state file is that the restore does not depend on this process
living long enough to run it. Anything that changes the setting writes the
previous value down first; `restore` replays that file and is safe to run when
nothing is pending.

usage:
  squashctl.py show    --repo <slug> [--host <host>]
  squashctl.py set     --repo <slug> --value <never|always|default_on|default_off>
  squashctl.py restore --repo <slug>
  squashctl.py pending
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

STATE = Path(os.environ.get("MERGE_STACK_STATE", Path.home() / ".claude" / "merge-stack-state.json"))
GITLAB_VALUES = ("never", "always", "default_on", "default_off")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _write_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    tmp.replace(STATE)


def _glab(args: list[str], host: str | None) -> str:
    env = dict(os.environ)
    if host:
        env["GITLAB_HOST"] = host
    r = subprocess.run(["glab", "api", *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise SystemExit(f"glab api failed: {r.stderr.strip()[:400]}")
    return r.stdout


def _project(repo: str, host: str | None) -> dict:
    return json.loads(_glab([f"projects/{urllib.parse.quote(repo, safe='')}"], host))


def show(repo: str, host: str | None) -> dict:
    p = _project(repo, host)
    out = {
        "repo": repo,
        "squash_option": p.get("squash_option"),
        "merge_method": p.get("merge_method"),
        "default_branch": p.get("default_branch"),
    }
    print(json.dumps(out, indent=2))
    return out


def set_value(repo: str, host: str | None, value: str) -> None:
    if value not in GITLAB_VALUES:
        raise SystemExit(f"value must be one of {GITLAB_VALUES}")
    cur = _project(repo, host)
    previous = cur.get("squash_option")
    if previous == value:
        print(json.dumps({"repo": repo, "squash_option": value, "changed": False}, indent=2))
        return

    # Write the restore point BEFORE the change, so a crash between the two
    # leaves a recoverable state rather than a silently flipped project.
    st = _state()
    st.setdefault("projects", {})
    st["projects"][repo] = {
        "host": host,
        "original_squash_option": previous,
        "set_to": value,
        "merge_method_at_change": cur.get("merge_method"),
    }
    _write_state(st)

    q = urllib.parse.quote(repo, safe="")
    _glab(["--method", "PUT", f"projects/{q}?squash_option={value}"], host)
    now = _project(repo, host).get("squash_option")
    if now != value:
        raise SystemExit(f"set failed: squash_option is {now!r}, wanted {value!r}")
    print(json.dumps({"repo": repo, "squash_option": now, "was": previous, "changed": True}, indent=2))


def restore(repo: str | None) -> int:
    st = _state()
    projects = st.get("projects", {})
    targets = [repo] if repo else list(projects)
    if not targets:
        print(json.dumps({"restored": [], "note": "nothing pending"}, indent=2))
        return 0
    restored, failed = [], []
    for name in targets:
        rec = projects.get(name)
        if not rec:
            continue
        want = rec.get("original_squash_option")
        if want is None:
            # Nothing recorded to go back to; leave it and say so rather than guess.
            failed.append({"repo": name, "reason": "no original value recorded"})
            continue
        q = urllib.parse.quote(name, safe="")
        try:
            _glab(["--method", "PUT", f"projects/{q}?squash_option={want}"], rec.get("host"))
            now = _project(name, rec.get("host")).get("squash_option")
        except SystemExit as e:
            failed.append({"repo": name, "reason": str(e)[:200]})
            continue
        if now == want:
            restored.append({"repo": name, "squash_option": now})
            projects.pop(name, None)
        else:
            failed.append({"repo": name, "reason": f"is {now!r}, wanted {want!r}"})
    st["projects"] = projects
    _write_state(st)
    print(json.dumps({"restored": restored, "failed": failed, "state_file": str(STATE)}, indent=2))
    return 1 if failed else 0


def pending() -> int:
    st = _state()
    projects = st.get("projects", {})
    print(json.dumps({"pending": projects, "state_file": str(STATE)}, indent=2))
    return 1 if projects else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "set", "restore"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=(name != "restore"))
        s.add_argument("--host")
        if name == "set":
            s.add_argument("--value", required=True)
    sub.add_parser("pending")
    a = ap.parse_args()

    if a.cmd == "show":
        show(a.repo, a.host); return 0
    if a.cmd == "set":
        set_value(a.repo, a.host, a.value); return 0
    if a.cmd == "restore":
        return restore(getattr(a, "repo", None))
    return pending()


if __name__ == "__main__":
    sys.exit(main())
