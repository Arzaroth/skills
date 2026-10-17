"""Unit tests for prwatch.py. The forge and the clock are mocked, so a 30-minute
watch runs instantly and the trigger rules are actually exercised.

Each test scripts a sequence of poll results and asserts the verdict line, the
exit code, and how many times the watcher posted.
"""
import io
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

import prwatch
from classify import ReviewerStatus
from forge import ForgeError
from prwatch import Finished, main, parse_args

OPEN = {"number": 1, "url": "u", "head": "h1", "head_branch": "b", "base": "main",
        "state": "open", "draft": "false", "ci": "success", "mergeable": "true",
        "merge_state": "clean", "threads_total": 0, "unresolved": 0}


def review(author, at, body, cid="1"):
    return {"id": cid, "author": author, "created_at": at, "body": body}


CLEAN = [review("coderabbitai", "2026-01-01T01:00:00Z",
                "No actionable comments were generated")]
FINDINGS = [review("coderabbitai", "2026-01-01T01:00:00Z",
                   "Actionable comments posted: 3")]
SILENT = []
PUSH = "2026-01-01T00:00:00Z"


class FakeForge:
    """Returns the nth scripted value per poll, repeating the last one forever."""

    slug = "github.com/o/r"
    host = "github.com"

    def __init__(self, states, reviews=None, approvals=None, creds=(), limited=(),
                 statuses=None):
        self.statuses_seq = statuses or [ReviewerStatus()]
        self.status_reads = 0
        self.identity = "default"
        self.creds = list(creds)
        self.limited = set(limited)
        self.acted_as = []
        self.states = states
        self.reviews_seq = reviews if reviews is not None else [SILENT]
        self.approvals_seq = approvals or [[]]
        self.comments = []
        self.own_ids = []
        self.rotations = 0
        self._i = -1

    def _pick(self, seq):
        return seq[min(self._i, len(seq) - 1)]

    def state(self, pr):
        self._i += 1
        return self._pick(self.states)

    def reviews(self, pr):
        return self._pick(self.reviews_seq)

    def approvals(self, pr):
        return self._pick(self.approvals_seq)

    def reviewer_status(self, sha):
        self.status_reads += 1
        return self._pick(self.statuses_seq)

    def comment(self, pr, body):
        self.comments.append(body)
        self.acted_as.append(self.identity)
        # Real forges return the new comment's id; the watcher needs it to
        # recognise its own post. The fake must too, or the bug hides.
        cid = f"own-{len(self.comments)}"
        self.own_ids.append(cid)
        return cid

    def use_credential(self, name):
        self.identity = name
        return name

    def fresh_credential(self):
        return next((n for n in ["default", *self.creds]
                     if n != self.identity and n not in self.limited), None)

    def avoid_limited(self):
        if self.identity not in self.limited:
            return None
        fresh = self.fresh_credential()
        return self.use_credential(fresh) if fresh else None

    def rotate_credential(self):
        self.rotations += 1
        return self.use_credential(self.fresh_credential() or self.identity)


class FakeClock:
    """Virtual time, so a 30-minute watch runs in microseconds. Without this the
    suite takes ~22s of real waiting and stops being run."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1)


def run_watch(forge, argv, budget=None, marks=None, now=None):
    """Run the watcher with a mocked forge, a virtual clock, and an in-memory
    stand-in for the git-config trigger budget (shared across watches when the
    caller passes the same dict)."""
    clock = FakeClock()
    if now is not None:
        clock.now = now.timestamp()
    spent = budget if budget is not None else {}
    # mark_limited writes the store that fresh_credential reads; the fake has to
    # keep that coupling or a spent account looks available again.
    limits = marks if marks is not None else []
    with mock.patch.object(prwatch, "detect", return_value=forge), \
         mock.patch.object(prwatch, "mark_limited",
                           side_effect=lambda host, name, until:
                           (limits.append((host, name, until)),
                            forge.limited.add(name))), \
         mock.patch.object(prwatch, "time", clock), \
         mock.patch.object(prwatch, "triggers_used",
                           side_effect=lambda slug, pr, head: spent.get((pr, head), 0)), \
         mock.patch.object(prwatch, "record_trigger",
                           side_effect=lambda slug, pr, head, n:
                           spent.__setitem__((pr, head), n)), \
         mock.patch("sys.stdout"), mock.patch("sys.stderr"):
        args = parse_args(argv)
        try:
            prwatch.watch(args)
        except Finished as done:
            return done.line, done.code
    return None, None


class TestTerminalStates(unittest.TestCase):
    def test_merged_short_circuits(self):
        f = FakeForge([{**OPEN, "state": "merged"}])
        line, code = run_watch(f, ["1", "--no-trigger"])
        self.assertTrue(line.startswith("verdict=merged"))
        self.assertEqual(code, 0)

    def test_closed(self):
        f = FakeForge([{**OPEN, "state": "closed"}])
        self.assertTrue(run_watch(f, ["1", "--no-trigger"])[0]
                        .startswith("verdict=closed"))

    def test_conflict(self):
        f = FakeForge([{**OPEN, "mergeable": "conflict"}])
        line, _ = run_watch(f, ["1", "--no-trigger"])
        self.assertTrue(line.startswith("verdict=conflict"))

    def test_review_done_carries_counts(self):
        f = FakeForge([OPEN], reviews=[FINDINGS])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger"])
        self.assertIn("verdict=review_done", line)
        self.assertIn("actionable=3", line)
        self.assertEqual(code, 0)

    def test_clean_review_is_a_verdict_not_a_wait(self):
        f = FakeForge([OPEN], reviews=[CLEAN])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger"])
        self.assertIn("actionable=0", line)

    def test_skip_is_terminal(self):
        f = FakeForge([OPEN], reviews=[[review("coderabbitai", "2026-01-01T01:00:00Z",
                                               "Review skipped: Too many files")]])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger"])
        self.assertIn("verdict=review_skipped", line)
        self.assertIn("reason=too_many_files", line)


class TestWaiting(unittest.TestCase):
    def test_silence_times_out_as_still_waiting_exit_3(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                   "--timeout", "1", "--interval", "0"])
        self.assertTrue(line.startswith("verdict=still_waiting"))
        self.assertEqual(code, 3, "still_waiting is not a failure, it means watch again")

    def test_a_watch_that_cannot_read_the_pr_gives_up_instead_of_waiting(self):
        """49 identical 401s once read back as still_waiting while the review had
        finished and the MR was mergeable."""
        class Blind(FakeForge):
            def state(self, pr):
                raise ForgeError("glab failed (1): HTTP 401 invalid_token")
        f = Blind([OPEN])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                   "--timeout", "1800", "--interval", "30"])
        self.assertTrue(line.startswith("verdict=giveup reason=forge_error"), line)
        self.assertEqual(code, 2)

    def test_a_read_blip_does_not_end_the_watch(self):
        class Flaky(FakeForge):
            fails = 2

            def state(self, pr):
                if self.fails:
                    self.fails -= 1
                    raise ForgeError("glab failed (1): HTTP 502")
                return super().state(pr)
        f = Flaky([OPEN], reviews=[CLEAN])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                   "--timeout", "1800", "--interval", "30"])
        self.assertTrue(line.startswith("verdict=review_done actionable=0"), line)

    def test_a_failed_reviews_read_is_not_read_as_silence(self):
        class NoReviews(FakeForge):
            def reviews(self, pr):
                raise ForgeError("glab failed (1): HTTP 401")
        f = NoReviews([OPEN])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                   "--timeout", "1800", "--interval", "30"])
        self.assertTrue(line.startswith("verdict=giveup reason=forge_error"), line)

    def test_walkthrough_alone_does_not_end_the_watch(self):
        """The failure that merges unreviewed code, at the watcher level."""
        f = FakeForge([OPEN], reviews=[[review("coderabbitai", "2026-01-01T01:00:00Z",
                                               "## Walkthrough\nstuff")]])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                "--timeout", "1", "--interval", "0"])
        self.assertTrue(line.startswith("verdict=still_waiting"))


class TestUnparsedOutputIsReported(unittest.TestCase):
    """Every marker is prose the reviewer can reword. When that happens an
    unknown message currently reads as silence, which is indistinguishable from
    a slow review and hangs the loop - twice, on live MRs. Timing out with
    unparsed bot output must SAY so, with the text."""

    def test_timeout_reports_what_it_could_not_parse(self):
        future = [review("coderabbitai", "2026-01-01T01:00:00Z",
                         "Heads up: the review pipeline is thinking about it")]
        f = FakeForge([OPEN], reviews=[future])
        line, code = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                   "--timeout", "1", "--interval", "0"])
        self.assertTrue(line.startswith("verdict=still_waiting"))
        self.assertIn("unparsed_bot_comments=1", line)
        self.assertIn("pipeline is thinking", line)
        self.assertEqual(code, 3)

    def test_silence_is_not_reported_as_unparsed(self):
        """No bot output at all is an ordinary wait, not a parser problem."""
        f = FakeForge([OPEN], reviews=[SILENT])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                "--timeout", "1", "--interval", "0"])
        self.assertNotIn("unparsed_bot_comments", line)

    def test_a_recognised_verdict_never_reports_unparsed(self):
        """The walkthrough is legitimately unmatched and precedes every verdict;
        it must not look like a parser failure once the verdict lands."""
        both = [review("coderabbitai", "2026-01-01T01:00:00Z", "## Walkthrough\nstuff",
                       cid="1"),
                review("coderabbitai", "2026-01-01T01:05:00Z",
                       "Actionable comments posted: 2", cid="2")]
        f = FakeForge([OPEN], reviews=[both])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger", "--interval", "0"])
        self.assertIn("verdict=review_done", line)
        self.assertNotIn("unparsed", line)


class TestHumanSignals(unittest.TestCase):
    def test_new_human_comment_after_baseline(self):
        first = [review("bot", "2026-01-01T01:00:00Z", "hi", cid="1")]
        later = first + [review("allan", "2026-01-01T02:00:00Z", "please fix", cid="2")]
        f = FakeForge([OPEN], reviews=[first, later])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger", "--interval", "0"])
        self.assertIn("verdict=human_activity", line)
        self.assertIn("author=allan", line)

    def test_comment_present_at_baseline_does_not_fire(self):
        existing = [review("allan", "2026-01-01T01:00:00Z", "old note", cid="1")]
        f = FakeForge([OPEN], reviews=[existing])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger",
                                "--timeout", "1", "--interval", "0"])
        self.assertTrue(line.startswith("verdict=still_waiting"))

    def test_ignored_author_is_not_human_activity(self):
        first = [review("bot", "2026-01-01T01:00:00Z", "hi", cid="1")]
        later = first + [review("arza", "2026-01-01T02:00:00Z", "my own note", cid="2")]
        f = FakeForge([OPEN], reviews=[first, later])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger", "--ignore", "arza",
                                "--timeout", "1", "--interval", "0"])
        self.assertTrue(line.startswith("verdict=still_waiting"))

    def test_new_threads_between_watches(self):
        f = FakeForge([OPEN, {**OPEN, "unresolved": 2}])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger", "--interval", "0"])
        self.assertIn("verdict=new_threads", line)
        self.assertIn("unresolved=2", line)

    def test_approval_only_when_human_is_a_reviewer(self):
        approvals = [[{"author": "allan", "state": "approved"}]]
        f = FakeForge([OPEN], approvals=approvals)
        line, _ = run_watch(f, ["1", "--reviewers", "human", "--no-trigger",
                                "--interval", "0"])
        self.assertIn("verdict=approved", line)

        f2 = FakeForge([OPEN], approvals=approvals)
        line2, _ = run_watch(f2, ["1", "--reviewers", "coderabbit", "--no-trigger",
                                  "--after", PUSH, "--timeout", "1", "--interval", "0"])
        self.assertTrue(line2.startswith("verdict=still_waiting"))


class TestTriggering(unittest.TestCase):
    def test_first_poll_does_not_trigger_a_queued_review(self):
        """The caller just triggered on create or after a push; nudging now would
        spend a quota slot on a review that is already queued.

        The fixture must show the review IS queued - bot output since the push.
        It used to be SILENT, which is the opposite case and made this test pass
        for the wrong reason."""
        queued = [review("coderabbitai", "2026-01-01T01:00:00Z", "## Walkthrough\nstuff")]
        f = FakeForge([OPEN], reviews=[queued])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "1", "--interval", "0",
                      "--trigger-gap", "300"])
        self.assertEqual(f.comments, [])

    def test_a_pending_request_is_not_idle(self):
        """Regression: the skill triggered at 21:32:39, the watcher's first
        poll saw no BOT output yet, read that as idle, and posted a second trigger
        at 21:32:59 - 20 seconds apart, inside a 180s gap. A request is a human
        comment, so counting only the reviewer's output cannot see it."""
        pending = [review("arza", "2026-01-01T01:00:00Z", "@coderabbitai review")]
        f = FakeForge([OPEN], reviews=[pending])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "3000"])
        self.assertEqual(f.comments, [],
                         "a review was already requested; must not request again")

    def test_a_full_review_request_also_counts(self):
        pending = [review("arza", "2026-01-01T01:00:00Z", "@coderabbitai full review")]
        f = FakeForge([OPEN], reviews=[pending])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "3000"])
        self.assertEqual(f.comments, [])

    def test_ordinary_human_prose_is_not_a_request(self):
        """Only an actual invocation counts, not someone discussing one."""
        chatter = [review("arza", "2026-01-01T01:00:00Z",
                          "I will ask for a review once CI is green")]
        f = FakeForge([OPEN], reviews=[chatter])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "3000"])
        self.assertEqual(len(f.comments), 1, "genuinely idle, so nudge")

    def test_triggers_at_once_when_nothing_is_queued(self):
        """Measured on a live MR: the skill pushed and watched without
        triggering, so the 180s "a review is already queued" grace was 180s of
        nothing. With no bot output since the push, there is nothing to wait for."""
        f = FakeForge([OPEN], reviews=[SILENT])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "1", "--interval", "0",
                      "--trigger-gap", "3000"])
        self.assertEqual(len(f.comments), 1, "must nudge on the first poll")

    def test_the_grace_still_applies_once_the_bot_has_spoken(self):
        """A walkthrough since the push means a review IS in flight; nudging then
        spends a quota slot on work already happening."""
        walkthrough = [review("coderabbitai", "2026-01-01T01:00:00Z",
                              "## Walkthrough\nstuff")]
        f = FakeForge([OPEN], reviews=[walkthrough])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "3000"])
        self.assertEqual(f.comments, [], "a queued review must not be nudged")

    def test_never_triggers_while_a_review_is_running(self):
        running = [review("coderabbitai", "2026-01-01T01:00:00Z",
                          "Currently processing new changes")]
        f = FakeForge([OPEN], reviews=[running])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0"])
        self.assertEqual(f.comments, [], "a request during a review is refused and "
                                         "still burns a quota slot")

    def test_never_triggers_while_rate_limited(self):
        limited = [review("coderabbitai", "2026-01-01T01:00:00Z",
                          "Review limit reached for this hour")]
        f = FakeForge([OPEN], reviews=[limited])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0"])
        self.assertEqual(f.comments, [])

    def test_triggers_when_silent_and_past_the_gap(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0", "--max-triggers", "2"])
        self.assertTrue(all(c == "@coderabbitai review" for c in f.comments))
        self.assertGreaterEqual(len(f.comments), 1)

    def test_trigger_cap_gives_up_with_exit_2(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        line, code = run_watch(f, ["1", "--after", PUSH, "--timeout", "60",
                                   "--interval", "0", "--trigger-gap", "0",
                                   "--max-triggers", "3"])
        self.assertIn("verdict=giveup", line)
        self.assertIn("reason=trigger_cap", line)
        self.assertEqual(code, 2)
        self.assertEqual(len(f.comments), 3, "the cap is what stopped a waiter "
                                             "posting ~50 requests on one MR")

    def test_no_trigger_never_posts(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0", "--no-trigger"])
        self.assertEqual(f.comments, [])

    def test_rotate_rotates_before_posting(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0", "--max-triggers", "2", "--rotate"])
        self.assertEqual(f.rotations, len(f.comments))


class TestWatcherDoesNotWakeItself(unittest.TestCase):
    """Regression: the trigger is posted as the USER, so the next poll saw a new
    non-bot comment and reported human_activity. The watcher woke itself up over
    its own nudge, and --no-trigger was the only workaround."""

    def test_own_trigger_is_not_human_activity(self):
        class Echoing(FakeForge):
            """Posting a trigger makes it show up in the next reviews() page,
            exactly as a real forge would."""
            def reviews(self, pr):
                return [review("arza", "2026-01-01T01:00:00Z", body, cid=cid)
                        for cid, body in zip(self.own_ids, self.comments)]

        f = Echoing([OPEN])
        line, code = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                   "--interval", "0", "--trigger-gap", "0",
                                   "--max-triggers", "2"])
        self.assertGreaterEqual(len(f.comments), 1, "it did post a trigger")
        self.assertNotIn("human_activity", line,
                         f"woke itself over its own trigger: {line}")

    def test_a_nudge_from_ANOTHER_process_is_not_human_activity(self):
        """The id trick only recognises a comment this process posted. With
        --rotate the nudge goes out under a named account, and the skill also
        triggers from the main thread between watches, so the watcher reported
        `human_activity author=allan.menguy` on its own "@coderabbitai review"
        and burned the watch. Recognise it by what it says, not who said it."""
        late = [review("allan.menguy", "2026-01-01T02:00:00Z",
                       "@coderabbitai review", cid="rot1")]
        f = FakeForge([OPEN], reviews=[SILENT, late])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                "--interval", "0", "--no-trigger"])
        self.assertNotIn("human_activity", line)

    def test_a_person_actually_talking_still_wakes_it(self):
        late = [review("allan.menguy", "2026-01-01T02:00:00Z",
                       "hold off, I want to look at the migration", cid="h1")]
        f = FakeForge([OPEN], reviews=[SILENT, late])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                "--interval", "0", "--no-trigger"])
        self.assertIn("verdict=human_activity", line)
        self.assertIn("author=allan.menguy", line)

    def test_own_trigger_does_not_trip_new_threads(self):
        """On GitLab the trigger note is a resolvable discussion, so it raises
        `unresolved`. Six triggers each came back as new_threads, and each wake
        re-triggered - a loop built entirely out of its own comments."""
        class Counting(FakeForge):
            def state(self, pr):
                s = dict(super().state(pr))
                s["unresolved"] = len(self.comments)   # our own notes count
                return s

        f = Counting([OPEN], reviews=[SILENT])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                "--interval", "0", "--trigger-gap", "0",
                                "--max-triggers", "2"])
        self.assertGreaterEqual(len(f.comments), 1, "it did trigger")
        self.assertNotIn("new_threads", line, f"woke on its own note: {line}")

    def test_a_real_human_comment_still_wakes_it(self):
        first = [review("bot", "2026-01-01T01:00:00Z", "hi", cid="1")]
        later = first + [review("allan", "2026-01-01T02:00:00Z", "look at this", cid="2")]
        f = FakeForge([OPEN], reviews=[first, later])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--no-trigger", "--interval", "0"])
        self.assertIn("verdict=human_activity", line)


class TestTriggerBudgetSurvivesRespawns(unittest.TestCase):
    """`--max-triggers` used to reset with every process. The skill re-spawns a
    watch on each still_waiting, so the cap multiplied: six triggers went up on
    one live MR, and refused attempts still count against the hourly quota."""

    def test_budget_is_shared_across_watches_on_one_head(self):
        shared = {}
        total = 0
        for _ in range(3):
            f = FakeForge([OPEN], reviews=[SILENT])
            run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                          "--trigger-gap", "0", "--max-triggers", "2"], budget=shared)
            total += len(f.comments)
        self.assertEqual(total, 2, f"three watches posted {total} triggers, cap was 2")

    def test_a_new_head_earns_a_fresh_budget(self):
        """A new push deserves a new review, so the cap resets per head."""
        shared = {}
        f1 = FakeForge([OPEN], reviews=[SILENT])
        run_watch(f1, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                       "--trigger-gap", "0", "--max-triggers", "1"], budget=shared)
        f2 = FakeForge([{**OPEN, "head": "h2"}], reviews=[SILENT])
        run_watch(f2, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                       "--trigger-gap", "0", "--max-triggers", "1"], budget=shared)
        self.assertEqual((len(f1.comments), len(f2.comments)), (1, 1))


class TestTheVerdictSaysWhichPR(unittest.TestCase):
    """Two watches returned verdict=merged for PRs in unrelated repos: a bare
    number resolved against whatever directory the process stood in, and a
    subagent inherits no directory from the prose that spawned it. Under
    `merge: auto` that verdict ends the job."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BABYSIT_REPO": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_a_watch_without_a_repo_refuses_to_run(self):
        f = FakeForge([OPEN])
        with mock.patch.object(prwatch, "detect", return_value=f), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(main(["1", "--no-trigger", "--timeout", "0"]), 64)
        self.assertEqual(f._i, -1, "it must not poll a repo it cannot name")

    def test_the_refusal_is_one_copy_paste_from_correct(self):
        f = FakeForge([OPEN])
        err = io.StringIO()
        with mock.patch.object(prwatch, "detect", return_value=f), \
             mock.patch("sys.stdout"), redirect_stderr(err):
            main(["1", "--no-trigger", "--timeout", "0"])
        self.assertIn("--repo github.com/o/r", err.getvalue())

    def test_every_verdict_names_the_repo_and_the_pr(self):
        cases = [([{**OPEN, "state": "merged"}], []),
                 ([{**OPEN, "mergeable": "conflict"}], []),
                 ([OPEN], CLEAN)]
        for states, reviews in cases:
            line, _ = run_watch(FakeForge(states, reviews=[reviews]),
                                ["1", "--after", PUSH, "--no-trigger"])
            self.assertIn("repo=github.com/o/r", line)
            self.assertIn("url=u", line)

    def test_still_waiting_names_it_too(self):
        line, code = run_watch(FakeForge([OPEN], reviews=[SILENT]),
                               ["1", "--after", PUSH, "--timeout", "5",
                                "--interval", "0", "--no-trigger"])
        self.assertEqual(code, 3)
        self.assertIn("repo=github.com/o/r", line)

    def test_an_expected_branch_that_does_not_match_is_a_giveup(self):
        f = FakeForge([{**OPEN, "head_branch": "feature/other"}])
        line, code = run_watch(f, ["1", "--no-trigger", "--expect-branch",
                                   "feature/mine"])
        self.assertIn("verdict=giveup", line)
        self.assertIn("reason=wrong_pr", line)
        self.assertIn("expected=feature/mine", line)
        self.assertIn("actual=feature/other", line)
        self.assertEqual(code, 2)

    def test_a_matching_branch_is_silent(self):
        f = FakeForge([{**OPEN, "state": "merged"}])
        line, _ = run_watch(f, ["1", "--no-trigger", "--expect-branch", "b"])
        self.assertIn("verdict=merged", line)

    def test_the_branch_check_precedes_every_terminal_state(self):
        """Otherwise the wrong PR still reports merged, which is the failure."""
        f = FakeForge([{**OPEN, "state": "merged", "head_branch": "other"}])
        line, _ = run_watch(f, ["1", "--no-trigger", "--expect-branch", "mine"])
        self.assertIn("reason=wrong_pr", line)


class TestAlreadyTerminal(unittest.TestCase):
    """A PR that was already merged when the watch opened was not merged by this
    job. Saying so is what makes a 9-second `verdict=merged` visible."""

    def test_merged_on_the_first_poll_is_flagged(self):
        line, _ = run_watch(FakeForge([{**OPEN, "state": "merged"}]),
                            ["1", "--no-trigger"])
        self.assertIn("already=1", line)

    def test_merged_during_the_watch_is_not(self):
        f = FakeForge([OPEN, OPEN, {**OPEN, "state": "merged"}], reviews=[SILENT])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--interval", "0",
                                "--no-trigger"])
        self.assertIn("verdict=merged", line)
        self.assertNotIn("already=1", line)

    def test_closed_on_the_first_poll_is_flagged(self):
        line, _ = run_watch(FakeForge([{**OPEN, "state": "closed"}]),
                            ["1", "--no-trigger"])
        self.assertIn("already=1", line)


class TestTriggerBudgetIsPerRepo(unittest.TestCase):
    def test_two_repos_do_not_share_a_budget(self):
        a = prwatch.trigger_budget_path("github.com/o/r", 7, "abc123def456")
        b = prwatch.trigger_budget_path("gl.example.com/o/r", 7, "abc123def456")
        self.assertNotEqual(a, b)

    def test_the_budget_survives_outside_the_repo_checkout(self):
        """It used to live in the git config of whatever directory the watch ran
        in, so a watch addressed elsewhere lost it and the cap reset."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(prwatch, "STATE_DIR", pathlib.Path(tmp) / "s"):
                prwatch.record_trigger("github.com/o/r", 7, "abc123def456", 3)
                self.assertEqual(
                    prwatch.triggers_used("github.com/o/r", 7, "abc123def456"), 3)
                self.assertEqual(
                    prwatch.triggers_used("github.com/o/r", 7, "other-head"), 0)

    def test_an_unwritable_state_dir_does_not_kill_the_watch(self):
        with mock.patch.object(prwatch, "STATE_DIR", pathlib.Path("/proc/nope")), \
             mock.patch("sys.stderr"):
            prwatch.record_trigger("github.com/o/r", 7, "abc", 1)
            self.assertEqual(prwatch.triggers_used("github.com/o/r", 7, "abc"), 0)


RATELIMIT = [review("coderabbitai", "2026-01-01T01:00:00Z",
                    "Review limit reached for this hour. Try again in 40 minutes.",
                    cid="rl1")]
RATELIMIT2 = RATELIMIT + [review("coderabbitai", "2026-01-01T01:10:00Z",
                                 "Review limit reached for this hour.", cid="rl2")]


class TestRateLimitIsPerAccount(unittest.TestCase):
    """A rate limit belongs to the account that asked, not to the PR. Another
    account's quota is untouched, so moving to it is not a repeat nudge and has
    nothing to wait out."""

    def watch(self, forge, extra=(), marks=None):
        return run_watch(forge, ["1", "--after", PUSH, "--timeout", "5",
                                 "--interval", "0", "--trigger-gap", "9999",
                                 "--max-triggers", "3", *extra], marks=marks)

    def test_it_switches_account_and_asks_again_without_waiting_the_gap(self):
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"])
        self.watch(f, ["--rotate"], marks=marks)
        self.assertEqual(f.acted_as, ["alt1"], "the spent account must not ask again")
        self.assertEqual([(name) for _, name, _ in marks], ["default"],
                         "the limit is recorded against the account that hit it")

    def test_the_gap_alone_would_have_blocked_it(self):
        """Same run without the rate limit: the gap holds, so the trigger in the
        test above is the rotation's, not the gap expiring."""
        f = FakeForge([OPEN], reviews=[SILENT], creds=["alt1"])
        self.watch(f, ["--rotate"])
        self.assertEqual(len(f.comments), 1, "one idle nudge, then the gap holds")

    def test_one_notice_is_answered_once(self):
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1", "alt2"])
        self.watch(f, ["--rotate"])
        self.assertEqual(len(f.comments), 1,
                         "it stays the newest comment for the whole window")

    def test_a_second_notice_blames_the_account_that_asked_for_it(self):
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT, RATELIMIT2],
                      creds=["alt1", "alt2"])
        self.watch(f, ["--rotate"], marks=marks)
        self.assertEqual(f.acted_as, ["alt1", "alt2"])
        self.assertEqual([name for _, name, _ in marks], ["default", "alt1"])

    def test_the_stated_window_is_when_the_quota_comes_back(self):
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"])
        self.watch(f, ["--rotate"], marks=marks)
        stated = datetime(2026, 1, 1, 1, 40, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(marks[0][2], stated, delta=1,
                               msg="the reviewer said 40 minutes; use that")

    def test_nothing_is_posted_when_every_account_is_spent(self):
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"],
                      limited=["alt1"])
        self.watch(f, ["--rotate"])
        self.assertEqual(f.comments, [], "waiting beats posting into a wall")

    def test_a_watch_that_already_moved_off_the_spent_account_asks_at_once(self):
        """Regression from a live MR: an earlier watch recorded the limit, so this
        one started by moving to alt1. The notice then blamed `default`, which
        was no longer the current identity, so nothing counted as a rotation
        and the watch sat out the whole window with a fresh account in hand."""
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"],
                      limited=["default"])
        self.watch(f, ["--rotate"], marks=marks)
        self.assertEqual(f.acted_as, ["alt1"],
                         "the quota in hand is untouched; asking is not a repeat")

    def test_every_account_spent_hands_back_at_once(self):
        """Regression from a live MR: with no account left, the watch sat out
        its whole timeout behind a limit nobody saw, while the user had already
        authorised paid reviews. It must return on the first poll instead."""
        limited = [review("coderabbitai", "2026-01-01T01:00:00Z",
                          "Review limit reached for this hour. **Next included review "
                          "available in 40 minutes.** Run an on-demand review: "
                          "Comment `@coderabbitai review --use-credits`.", cid="rl1")]
        f = FakeForge([OPEN], reviews=[limited])
        line, code = run_watch(f, ["1", "--after", PUSH, "--timeout", "1800",
                                   "--interval", "30", "--rotate"])
        self.assertIn("verdict=rate_limited", line)
        self.assertEqual(code, 4)
        self.assertIn("credits_offered=1", line)
        self.assertIn("retry_at=", line)
        self.assertEqual(f.comments, [], "never posts into a wall, paid or free")
        self.assertEqual(f._i + 1, 1, "one look is enough to know nothing will come")

    def test_a_notice_whose_window_is_over_is_answered_with_a_request(self):
        """Regression from a live PR: the window had closed two minutes
        earlier, but the old notice was still the newest bot comment, so the
        watch blamed the account again and handed back rate_limited without
        asking for the review the quota now allowed."""
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT])
        line, _ = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                "--interval", "0", "--rotate"], marks=marks,
                            now=datetime(2026, 1, 1, 1, 42, tzinfo=timezone.utc))
        self.assertNotIn("verdict=rate_limited", line)
        self.assertEqual(f.acted_as, ["default"])
        self.assertEqual(marks, [], "a closed window spends nobody's quota")

    def test_a_lapsed_notice_is_answered_once(self):
        f = FakeForge([OPEN], reviews=[RATELIMIT])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "60", "--interval", "1",
                      "--trigger-gap", "9999", "--rotate"],
                  now=datetime(2026, 1, 1, 1, 42, tzinfo=timezone.utc))
        self.assertEqual(len(f.comments), 1,
                         "the notice stays newest until the review lands")

    def test_the_same_notice_inside_its_window_still_hands_back(self):
        f = FakeForge([OPEN], reviews=[RATELIMIT])
        line, code = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                   "--interval", "0", "--rotate"],
                               now=datetime(2026, 1, 1, 1, 38, tzinfo=timezone.utc))
        self.assertIn("verdict=rate_limited", line)
        self.assertEqual(code, 4)
        self.assertEqual(f.comments, [])

    def test_a_failed_review_hands_back_at_once(self):
        failed = [review("coderabbitai", "2026-01-01T01:00:00Z",
                         "❌ Action failed Review failed.", cid="rf1")]
        line, code = run_watch(FakeForge([OPEN], reviews=[failed]),
                               ["1", "--after", PUSH, "--timeout", "1800", "--interval", "30"])
        self.assertIn("verdict=review_failed", line)
        self.assertEqual(code, 5)

    def test_a_fresh_account_still_asks_rather_than_handing_back(self):
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"])
        line, _ = self.watch(f, ["--rotate"])
        self.assertNotIn("verdict=rate_limited", line)
        self.assertEqual(f.acted_as, ["alt1"])

    def test_without_rotate_the_limit_is_recorded_but_nothing_moves(self):
        marks = []
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"])
        self.watch(f, ["--no-rotate"], marks=marks)
        self.assertEqual(f.comments, [])
        self.assertEqual([name for _, name, _ in marks], ["default"])
        self.assertEqual(f.identity, "default")

    def test_the_trigger_cap_still_binds(self):
        """Rotation multiplies the quota, not the number of comments on the PR."""
        f = FakeForge([OPEN], reviews=[RATELIMIT, RATELIMIT2],
                      creds=["alt1", "alt2"])
        line, code = run_watch(f, ["1", "--after", PUSH, "--timeout", "5",
                                   "--interval", "0", "--trigger-gap", "9999",
                                   "--max-triggers", "1", "--rotate"])
        self.assertEqual(len(f.comments), 1)
        self.assertIn("reason=trigger_cap", line)


class TestAKnownSpentAccountIsNotUsed(unittest.TestCase):
    def test_a_watch_starts_on_an_account_with_quota_left(self):
        """Fix at the boundary, not in the loop: the first trigger of a run used
        to go out as whoever was default, including one limited minutes ago."""
        f = FakeForge([OPEN], reviews=[SILENT], creds=["alt1"], limited=["default"])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--no-trigger", "--rotate"])
        self.assertEqual(f.identity, "alt1")

    def test_a_free_default_is_left_alone(self):
        f = FakeForge([OPEN], reviews=[SILENT], creds=["alt1"])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--no-trigger", "--rotate"])
        self.assertEqual(f.identity, "default")

    def test_rotation_is_the_default(self):
        """Opt-in rotation left a run idle on a rate limit with two untouched
        seats in the credentials file, so it is on unless refused."""
        f = FakeForge([OPEN], reviews=[SILENT], creds=["alt1"], limited=["default"])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--no-trigger"])
        self.assertEqual(f.identity, "alt1")

    def test_without_rotate_nothing_switches(self):
        f = FakeForge([OPEN], reviews=[SILENT], creds=["alt1"], limited=["default"])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--no-trigger", "--no-rotate"])
        self.assertEqual(f.identity, "default")


BUSY = ReviewerStatus(state="running", description="Review in progress",
                      at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
FREE = ReviewerStatus(state="success", description="Review completed",
                      at=datetime(2026, 1, 1, 0, 9, tzinfo=timezone.utc))


class TestNoNudgeWhileTheReviewerSaysItIsReviewing(unittest.TestCase):
    """Regression from a live MR: the reviewer's commit status went `running`
    at 18:22:09 and the first bot comment arrived at 18:26:42. In between the PR
    looks idle to anything reading comments, and a trigger went up at 18:24:40."""

    def watch(self, forge, extra=()):
        return run_watch(forge, ["1", "--after", PUSH, "--timeout", "5",
                                 "--interval", "0", "--trigger-gap", "0",
                                 "--max-triggers", "3", *extra])

    def test_a_running_status_holds_every_nudge(self):
        f = FakeForge([OPEN], reviews=[SILENT], statuses=[BUSY])
        self.watch(f)
        self.assertEqual(f.comments, [], "the comments are silent, the status is not")

    def test_it_asks_once_the_status_clears(self):
        """The gate must not become a way to never ask."""
        f = FakeForge([OPEN], reviews=[SILENT], statuses=[BUSY, BUSY, FREE])
        self.watch(f)
        self.assertGreaterEqual(len(f.comments), 1)

    def test_a_held_nudge_does_not_spend_the_budget(self):
        shared = {}
        f = FakeForge([OPEN], reviews=[SILENT], statuses=[BUSY])
        run_watch(f, ["1", "--after", PUSH, "--timeout", "5", "--interval", "0",
                      "--trigger-gap", "0", "--max-triggers", "1"], budget=shared)
        self.assertEqual(shared, {})

    def test_an_unreadable_status_does_not_stall_the_watch(self):
        class Broken(FakeForge):
            def reviewer_status(self, sha):
                raise ForgeError("503")

        f = Broken([OPEN], reviews=[SILENT])
        self.watch(f)
        self.assertGreaterEqual(len(f.comments), 1)

    def test_rotating_past_a_rate_limit_still_respects_it(self):
        """A new account does not help while a review is actually running."""
        f = FakeForge([OPEN], reviews=[RATELIMIT], creds=["alt1"], statuses=[BUSY])
        self.watch(f, ["--rotate"])
        self.assertEqual(f.comments, [])

    def test_it_costs_nothing_when_there_is_no_nudge_to_make(self):
        f = FakeForge([OPEN], reviews=[SILENT], statuses=[BUSY])
        self.watch(f, ["--no-trigger"])
        self.assertEqual(f.status_reads, 0)


class TestOneSnapshot(unittest.TestCase):
    """`--timeout 0` is how /loop drives this: one look, act, return. It used to
    return still_waiting without polling at all, because the deadline was checked
    before the first poll."""

    def test_timeout_zero_still_takes_one_look(self):
        line, code = run_watch(FakeForge([{**OPEN, "state": "merged"}]),
                               ["1", "--no-trigger", "--timeout", "0"])
        self.assertIn("verdict=merged", line)
        self.assertEqual(code, 0)

    def test_timeout_zero_reports_what_it_saw(self):
        line, code = run_watch(FakeForge([OPEN], reviews=[FINDINGS]),
                               ["1", "--no-trigger", "--timeout", "0",
                                "--after", PUSH])
        self.assertIn("actionable=3", line)

    def test_nothing_there_yet_is_one_poll_then_still_waiting(self):
        f = FakeForge([OPEN], reviews=[SILENT])
        line, code = run_watch(f, ["1", "--no-trigger", "--timeout", "0",
                                   "--after", PUSH])
        self.assertIn("verdict=still_waiting", line)
        self.assertEqual(code, 3)
        self.assertEqual(f._i, 0, "exactly one poll")

    def test_a_forge_outage_does_not_spin_forever(self):
        class Broken(FakeForge):
            def state(self, pr):
                self._i += 1
                raise ForgeError("503")

        line, code = run_watch(Broken([OPEN]), ["1", "--no-trigger",
                                                "--timeout", "0", "--interval", "0"])
        self.assertIn("verdict=still_waiting", line)


class TestArgGuards(unittest.TestCase):
    def test_max_triggers_zero_is_rejected_not_an_instant_giveup(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(main(["1", "--max-triggers", "0"]), 64)

    def test_max_triggers_zero_allowed_with_no_trigger(self):
        f = FakeForge([{**OPEN, "state": "merged"}])
        with mock.patch.object(prwatch, "detect", return_value=f), \
             mock.patch.object(prwatch, "time", FakeClock()), mock.patch("sys.stdout"), \
             mock.patch("sys.stderr"):
            self.assertEqual(main(["1", "--repo", "o/r", "--max-triggers", "0",
                                   "--no-trigger"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
