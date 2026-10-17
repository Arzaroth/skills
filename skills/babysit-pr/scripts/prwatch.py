#!/usr/bin/env python3
"""prwatch - block until a PR/MR reaches a state worth waking an agent for.

Runs in the FOREGROUND. Progress goes to stderr; the last line on stdout is a
single KV verdict, which is the only thing a caller needs to report back.

usage: prwatch.py <pr> --repo SLUG [options]
  --repo SLUG         host/owner/name, owner/name, or a remote url. REQUIRED: a
                      bare number resolves against whatever repo the process
                      stands in, and a subagent has no working directory.
  --expect-branch B   refuse to report on a PR whose head branch is not B
  --reviewers a,b     reviewers to wait on (default coderabbit). "human" also
                      waits for an approval.
  --timeout SEC       give up waiting and report still_waiting (default 1800).
                      0 is one snapshot: it always polls once before giving up.
  --interval SEC      poll interval (default 30)
  --after ISO8601     only trust reviewer output newer than this. Set it to when
                      you last pushed; without it a verdict cannot be anchored to
                      your head and the result carries stale_risk=1.
  --max-triggers N    hard cap on review requests (default 4)
  --trigger-gap SEC   min seconds between triggers (default 180)
  --no-trigger        observe only, never post
  --no-rotate         trigger as the default account only. By default the
                      watch rotates credentials across triggers (per-user
                      quotas): a reviewer rate limit is recorded against the
                      account that hit it, and the watch steps to one with quota
                      left. Without a credentials file there is nothing to
                      rotate to, so the default costs nothing.
  --ignore A,B        logins whose comments are not "human activity"

Verdicts: merged | review_done actionable=N | review_skipped reason=R
          human_activity author=X | new_threads | approved | closed | conflict
          still_waiting | giveup reason=R
          rate_limited retry_in= retry_at= credits_offered=1 (every account spent)
          review_failed (the reviewer refused the request outright)
Every verdict carries repo= and url=, so the line says which PR it is about.
Exit: 0 actionable verdict, 3 still_waiting, 2 giveup, 4 rate_limited, 5 review_failed,
      64 no repo.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

from classify import classify
from forge import (ASSUMED_QUOTA_WINDOW, STATE_DIR, AddressError, ForgeError,
                   detect, mark_limited)

BUDGET_TTL = 14 * 86400

# Consecutive polls that could not read the PR before the watch says so. A blip
# recovers well inside this; a dead token or a revoked project never does.
MAX_READ_FAILURES = 5


def trigger_budget_path(slug, pr, head):
    """Per repo, PR AND head: a new push earns a fresh budget, a re-spawned watch
    on the same head does not. Kept outside git config, which would follow the
    working directory rather than the repo being watched."""
    key = re.sub(r"[^A-Za-z0-9._-]", "-", f"{slug}#{pr}@{(head or 'nohead')[:12]}")
    return STATE_DIR / (key + ".triggers")


def triggers_used(slug, pr, head):
    try:
        return int(trigger_budget_path(slug, pr, head).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def record_trigger(slug, pr, head, n):
    path = trigger_budget_path(slug, pr, head)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(str(n))
        cutoff = time.time() - BUDGET_TTL
        for old in STATE_DIR.glob("*.triggers"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError as exc:
        say(f"cannot persist the trigger budget ({exc}); it lasts this watch only")


def reviewer_busy(f, head):
    """The reviewer's own commit status says it is reviewing this head.

    Comments cannot tell: on a live MR the status went `running` two and a half
    minutes before the bot wrote anything, and a trigger went up in between. A
    status we cannot read is not evidence of anything, so it never blocks."""
    try:
        st = f.reviewer_status(head)
    except ForgeError as exc:
        say(f"cannot read the reviewer's status ({exc}); deciding without it")
        return False
    if st.stuck:
        say(f"reviewer status '{st.state}' since {st.at:%H:%M} looks dead; ignoring it")
    if st.busy:
        say(f"reviewer status is '{st.state}' on {head[:12]} since "
            f"{st.at:%H:%M:%S}: a review is under way, not asking for another")
    return st.busy


def say(msg):
    print(f"prwatch: {msg}", file=sys.stderr, flush=True)


class Finished(Exception):
    def __init__(self, verdict, **fields):
        self.line = " ".join([f"verdict={verdict}"]
                             + [f"{k}={v}" for k, v in fields.items()])
        self.code = {"still_waiting": 3, "giveup": 2, "rate_limited": 4,
                     "review_failed": 5}.get(verdict, 0)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="prwatch.py")
    p.add_argument("pr", type=int)
    p.add_argument("--repo", metavar="SLUG")
    p.add_argument("--expect-branch", default=None)
    p.add_argument("--reviewers", default="coderabbit")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--after", default=None)
    p.add_argument("--max-triggers", type=int, default=4)
    p.add_argument("--trigger-gap", type=int, default=180)
    p.add_argument("--no-trigger", action="store_true")
    p.add_argument("--rotate", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--ignore", default="")
    return p.parse_args(argv)


def watch(args, f=None):
    f = f or detect(args.repo)
    addr = {"repo": f.slug}

    def finish(verdict, **fields):
        raise Finished(verdict, **fields, **addr)

    reviewers = {r.strip() for r in args.reviewers.split(",") if r.strip()}
    want_bot, want_human = "coderabbit" in reviewers, "human" in reviewers
    ignore = args.ignore.split(",")
    start_identity = f.identity

    if args.rotate:
        moved = f.avoid_limited()
        if moved:
            say(f"'{start_identity}' is still rate limited; starting as '{moved}'")
    # A rate-limit notice is answered once. It stays the newest bot comment for
    # as long as the window lasts, and reacting on every poll would walk through
    # every credential in a few minutes.
    handled_limits = set()
    # Once its window is over the same notice gets one request, at once: the
    # gap assumes the caller just asked, and nobody asks into a closed window.
    answered_lapses = set()
    triggered_as = None

    start = time.time()
    # The cap is per PR+head, not per process. It used to reset on every watch,
    # so a chain of re-spawns multiplied it: six triggers went up on a live MR
    # between two watches, and refused attempts still count against the quota.
    triggers = 0
    # Starts at `start`, not 0: the caller has just triggered a review (on PR
    # creation, or after the push that ended the last round), so nudging on the
    # first poll would spend a quota slot on a review already queued.
    last_trigger = start
    baseline_ids = baseline_unresolved = None
    rebaseline_unresolved = False
    verdict = None

    say(f"watching {f.slug}#{args.pr} (reviewers={args.reviewers} "
        f"timeout={args.timeout}s interval={args.interval}s)")
    if args.after:
        say(f"ignoring reviewer output older than {args.after}")
    else:
        say("no --after given: a verdict cannot be anchored to the current head")

    polled = False
    read_failures = 0
    while True:
        elapsed = int(time.time() - start)
        # Always take one look first: `--timeout 0` means one snapshot, which is
        # how /loop drives this, not "give up before asking".
        if polled and elapsed >= args.timeout:
            fields = {"elapsed": f"{elapsed}s",
                      "bot": (verdict.bot if verdict else "unknown"),
                      "blocked": (verdict.blocked if verdict else "")}
            if verdict and verdict.credits_offered:
                fields["credits_offered"] = 1
            # Hand the caller the reviewer's OWN stated wait, so it can schedule
            # the next watch instead of guessing at a quota period.
            if verdict and verdict.retry_in:
                fields["retry_in"] = f"{verdict.retry_in}s"
                fields["retry_at"] = verdict.retry_at.isoformat()
            # Timing out with bot comments we could not parse means the wording
            # probably changed. Say so, with the text, instead of reporting a
            # silence that looks the same as a slow review.
            if verdict and verdict.bot in ("none", "stale") and verdict.unmatched:
                fields["unparsed_bot_comments"] = verdict.unmatched
                fields["unparsed_sample"] = repr(verdict.last_unmatched)
            finish("still_waiting", **fields)

        try:
            polled = True
            state = f.state(args.pr)
            comments = f.reviews(args.pr)
        except ForgeError as exc:
            read_failures += 1
            say(f"read failed ({read_failures}/{MAX_READ_FAILURES}): {exc}")
            # A watch that cannot read the PR knows nothing, and reporting that as
            # still_waiting looks exactly like a slow review. It hid a finished
            # review and a mergeable MR behind 49 identical 401s for 25 minutes.
            if read_failures >= MAX_READ_FAILURES:
                finish("giveup", reason="forge_error", identity=f.identity,
                       error=repr(str(exc)[:200]))
            time.sleep(args.interval)
            continue
        read_failures = 0

        addr["url"] = state["url"]
        first_poll = baseline_ids is None
        # An assertion the caller can make about the PR it means, for when the
        # number itself is stale: a restack renumbers nothing, but a branch that
        # has moved on to another PR reads as a perfectly healthy watch.
        if args.expect_branch and state["head_branch"] != args.expect_branch:
            finish("giveup", reason="wrong_pr", expected=args.expect_branch,
                   actual=state["head_branch"])
        # Terminal before the watch did anything: nobody here merged it. Say so,
        # so a 9-second run cannot be read back as this job's success.
        if state["state"] == "merged":
            finish("merged", head=state["head"], **({"already": 1} if first_poll else {}))
        if state["state"] == "closed":
            finish("closed", **({"already": 1} if first_poll else {}))
        if state["mergeable"] == "conflict":
            finish("conflict", head=state["head"], base=state["base"])

        verdict = classify(comments, head=state["head"], after=args.after, ignore=ignore,
                           now=datetime.fromtimestamp(time.time(), timezone.utc))
        unresolved = state["unresolved"]
        if baseline_ids is None:
            triggers = triggers_used(f.slug, args.pr, state["head"])
            if triggers:
                say(f"{triggers} trigger(s) already spent on this head by earlier watches")

        if rebaseline_unresolved:
            baseline_unresolved = max(baseline_unresolved or 0, unresolved)
            rebaseline_unresolved = False

        if baseline_ids is None:
            baseline_ids, baseline_unresolved = set(verdict.human_ids), unresolved
            say(f"baseline: bot={verdict.bot} ci={state['ci']} "
                f"unresolved={unresolved} human_notes={len(baseline_ids)}")
        else:
            new_human = [i for i in verdict.human_ids if i not in baseline_ids]
            if new_human:
                finish("human_activity", author=verdict.human_latest,
                               unresolved=unresolved)
            # A thread opened between two watches has no new top-level comment to
            # notice, so watch the unresolved count too.
            if unresolved > baseline_unresolved:
                finish("new_threads", unresolved=unresolved,
                               was=baseline_unresolved)

        if want_human and f.approvals(args.pr):
            finish("approved", unresolved=unresolved)

        if want_bot:
            if verdict.bot == "done":
                finish("review_done", actionable=verdict.actionable,
                               unresolved=unresolved, ci=state["ci"],
                               head=state["head"], stale_risk=verdict.stale_risk)
            if verdict.bot == "skip":
                finish("review_skipped", reason=verdict.reason,
                               head=state["head"])

        # A rate limit belongs to an ACCOUNT, not to the PR. Another account's
        # quota is untouched, so switching to it is not a repeat nudge and does
        # not wait out the gap - the gap exists to stop us asking the same
        # account twice, and this is a different one.
        rotated_now = False
        no_account_left = False
        # A notice whose window is over is history, not a limit: blaming the
        # account for it again handed back rate_limited on a live PR two minutes
        # after the quota had come back, with nobody asking for the review.
        if (verdict.blocked == "ratelimit" and not verdict.limit_lapsed
                and verdict.blocked_id not in handled_limits):
            handled_limits.add(verdict.blocked_id)
            blamed = triggered_as or start_identity
            until = (verdict.retry_at.timestamp() if verdict.retry_at
                     else time.time() + ASSUMED_QUOTA_WINDOW)
            mark_limited(f.host, blamed, until)
            say(f"'{blamed}' is out of review quota"
                + (f" for {verdict.retry_in}s" if verdict.retry_in else ""))
            if args.rotate and blamed == f.identity:
                fresh = f.fresh_credential()
                if fresh:
                    f.use_credential(fresh)
                    say(f"switching to '{fresh}', whose quota is untouched")
                    rotated_now = True
                else:
                    say("every credential is rate limited")
                    no_account_left = True
            elif args.rotate:
                # avoid_limited() already moved us off the blamed account at
                # startup, so the identity in hand has quota even though no
                # switch happens here.
                say(f"already on '{f.identity}', whose quota is untouched")
                rotated_now = True
            else:
                no_account_left = True

        # No account left to ask with: nothing this watch can do will produce a
        # review before the window ends, and sitting it out looks exactly like a
        # slow review from the caller's side. Measured on a live MR: a watch
        # idled 30 minutes behind a limit nobody was told about, while the user
        # had already authorised paid reviews. Hand the decision back at once.
        # A refusal that is not about quota: nothing more will come for this
        # request, so waiting is pointless. Hand it back like a rate limit.
        if verdict.blocked == "failed":
            finish("review_failed", head=state["head"])

        if no_account_left:
            fields = {"head": state["head"], "identity": f.identity}
            if verdict.retry_at:
                fields["retry_in"] = f"{verdict.retry_in}s"
                fields["retry_at"] = verdict.retry_at.isoformat()
            if verdict.credits_offered:
                fields["credits_offered"] = 1
            finish("rate_limited", **fields)

        # Nudge, unless the reviewer has told us not to bother (see Verdict.may_trigger).
        if want_bot and not args.no_trigger and (verdict.may_trigger or rotated_now):
            if triggers >= args.max_triggers:
                finish("giveup", reason="trigger_cap", triggers=triggers,
                               bot=verdict.bot, head=state["head"])
            # The gap exists because the caller normally triggers before the
            # watch starts, so an immediate nudge would spend a slot on a review
            # already queued. Skip it only when the PR is genuinely idle - see
            # Verdict.idle. Testing bot output alone was not enough: a request is
            # a HUMAN comment, so right after a trigger the bot is still silent
            # and that read as idle, posting a second trigger 20 seconds later.
            nothing_queued = triggers == 0 and verdict.idle
            lapse_now = (verdict.limit_lapsed
                         and verdict.blocked_id not in answered_lapses)
            if ((nothing_queued or rotated_now or lapse_now
                    or time.time() - last_trigger >= args.trigger_gap)
                    and not reviewer_busy(f, state["head"])):
                if lapse_now:
                    answered_lapses.add(verdict.blocked_id)
                triggers += 1
                record_trigger(f.slug, args.pr, state["head"], triggers)
                last_trigger = time.time()
                try:
                    if args.rotate and not rotated_now:
                        f.rotate_credential()
                    triggered_as = f.identity
                    say(f"requesting review ({triggers}/{args.max_triggers}, "
                        f"state={verdict.bot}, as={f.identity})")
                    # Fixed, free phrase. The watcher never posts a paid option,
                    # even when a rate-limit message offers one.
                    own = f.comment(args.pr, "@coderabbitai review")
                    # The trigger is posted as the USER, so without this the next
                    # poll sees a new non-bot comment and reports human_activity -
                    # the watcher waking itself up over its own nudge.
                    if own and baseline_ids is not None:
                        baseline_ids.add(own)
                    # On GitLab that note is also a resolvable discussion, so it
                    # raises `unresolved` and trips the new_threads check too.
                    # Measured: six triggers each returned new_threads, and each
                    # wake re-triggered. Re-baseline instead of counting our own
                    # comment as someone else's review thread.
                    rebaseline_unresolved = True
                except ForgeError as exc:
                    say(f"trigger failed: {exc}")

        blocked = f" blocked={verdict.blocked}" if verdict.blocked else ""
        if verdict.retry_in:
            blocked += f" retry_in={verdict.retry_in}s"
        if verdict.credits_offered:
            blocked += " credits_offered=1"
        say(f"t+{elapsed}s bot={verdict.bot}{blocked} "
            f"ci={state['ci']} unresolved={unresolved}")
        time.sleep(args.interval)


# A bare number resolves against whatever repo the process stands in, and a
# subagent does not inherit a working directory from the prose that spawned it.
# Two watches reported a merge for PRs in unrelated repos that way, and under
# `merge: auto` that verdict ends the job.
def require_address(args, forge):
    if args.repo or os.environ.get("BABYSIT_REPO"):
        return
    raise AddressError(
        f"#{args.pr} is a number, not an address, and this watch decides whether "
        f"a PR is done. Pass --repo {forge.slug} if that is the one you mean.")


def take_over(slug, pr):
    """Stop any older watch on the same PR and record this one.

    A watch outlives the session that started it: when a session dies mid-watch
    the process keeps polling, and the next session starts a second one on the
    same PR. Measured on a live MR: two watches ran at once, neither one
    reported, and the user saw nothing happen for half an hour.
    """
    path = pathlib.Path(STATE_DIR) / "watch-{}-{}.pid".format(
        re.sub(r"[^A-Za-z0-9]+", "_", slug), pr)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = int(path.read_text().strip())
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid():
        try:
            cmdline = pathlib.Path(f"/proc/{old}/cmdline").read_bytes()
        except OSError:
            cmdline = b""
        if b"prwatch" in cmdline:
            os.kill(old, 15)
            say(f"stopped an older watch on this PR (pid {old})")
    path.write_text(str(os.getpid()))


def main(argv=None):
    args = parse_args(argv)
    if args.max_triggers == 0 and not args.no_trigger:
        print("prwatch: --max-triggers 0 means 'give up immediately'; "
              "use --no-trigger to observe without posting", file=sys.stderr)
        return 64
    try:
        f = detect(args.repo)
        require_address(args, f)
        take_over(f.slug, args.pr)
        watch(args, f)
    except Finished as done:
        print(done.line)
        return done.code
    except AddressError as exc:
        print(f"prwatch: {exc}", file=sys.stderr)
        return 64
    except ForgeError as exc:
        print(f"prwatch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
