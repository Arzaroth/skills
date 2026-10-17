"""Contract tests for the reviewer classifier.

Every case here is a bug that shipped, or would silently merge unreviewed code
if it regressed. Cases A and C are regressions against real data from a live MR.

Run: python3 -m unittest discover -s scripts   (or: python3 scripts/test_classify.py)
"""
import unittest

from datetime import datetime, timezone

from classify import classify, parse_retry

HEAD = "0fabd3c0ccaf8adaf0909ec429102d7375c3439a"
PUSH = "2026-09-13T18:40:00Z"


def c(cid, author, at, body):
    return {"id": cid, "author": author, "created_at": at, "body": body}


# Shape taken from a real MR: a rate-limit notice that HAPPENS to carry the
# current head SHA, followed by two genuine verdicts that do not.
REAL = [
    c("1", "coderabbitai", "2026-09-13T18:29:28Z",
      f"Review limit reached. Reviewed up to {HEAD}"),
    c("2", "coderabbitai", "2026-09-13T18:35:43Z", "## Walkthrough\nIt changes things."),
    c("3", "coderabbitai", "2026-09-13T18:35:46Z",
      "**Actionable comments posted: 2**\nReviewed up to " + "a" * 40),
    c("4", "allan.menguy", "2026-09-13T18:36:42Z", "lgtm once CI is green"),
    c("5", "coderabbitai", "2026-09-13T18:49:13Z",
      "**Actionable comments posted: 1**\nReviewed up to " + "b" * 40),
]


class TestCurrency(unittest.TestCase):
    def test_a_no_push_time_reads_newest_and_flags_it(self):
        """Regression: SHA filtering used to drop the two newest verdicts and
        return the older 'Review limit reached' instead."""
        v = classify(REAL, head=HEAD)
        self.assertEqual(v.bot, "done")
        self.assertEqual(v.actionable, "1")
        self.assertEqual(v.stale_risk, 1, "unanchored verdict must be flagged")

    def test_b_push_time_anchors_the_newest_verdict(self):
        v = classify(REAL, head=HEAD, after=PUSH)
        self.assertEqual((v.bot, v.actionable, v.stale_risk), ("done", "1", 0))

    def test_c_nothing_since_the_push_is_stale_not_done(self):
        v = classify(REAL, head=HEAD, after="2026-09-14T00:00:00Z")
        self.assertEqual(v.bot, "stale")
        self.assertEqual(v.actionable, "", "a stale read must not carry findings")

    def test_head_in_winning_comment_clears_stale_risk(self):
        verdict_naming_head = [c("9", "coderabbitai", "2026-09-13T19:00:00Z",
                                 f"Actionable comments posted: 1\nReviewed up to {HEAD}")]
        self.assertEqual(classify(verdict_naming_head, head=HEAD).stale_risk, 0)

    def test_a_notice_can_never_be_the_winning_comment(self):
        """Only a STATE marker anchors currency; a notice naming the head does
        not make an absent verdict current."""
        self.assertEqual(classify([REAL[0]], head=HEAD).stale_risk, 1)


class TestOurOwnNudgesAreNotHumanActivity(unittest.TestCase):
    """A comment that is only a directive at the reviewer is our side of the
    conversation. Author cannot be the test: --rotate posts it under a named
    account, and a watcher cannot recognise the id of a comment that another
    process posted."""

    def trigger(self, author="allan.menguy", body="@coderabbitai review"):
        return [c("t1", author, "2026-09-13T19:00:00Z", body)]

    def test_a_rotated_trigger_is_not_human_activity(self):
        self.assertEqual(classify(self.trigger()).human_ids, [])

    def test_it_still_counts_as_a_review_already_requested(self):
        """Dropping it from human activity must not make the PR look idle, or
        the watcher nudges again on top of the nudge it just filtered out."""
        v = classify(self.trigger(), after="2026-09-13T18:00:00Z")
        self.assertEqual(v.triggers_seen, 1)
        self.assertFalse(v.idle, "a queued review is not an idle PR")

    def test_a_real_comment_from_the_same_person_still_counts(self):
        v = classify(self.trigger(body="this needs a second look at the index"))
        self.assertEqual(v.human_ids, ["t1"])
        self.assertEqual(v.human_latest, "allan.menguy")

    def test_prose_that_merely_names_the_bot_is_still_a_person_talking(self):
        v = classify(self.trigger(
            body="@coderabbitai is wrong here, the lock is held by the caller"))
        self.assertEqual(v.human_ids, ["t1"])

    def test_a_mix_of_both_leaves_only_the_real_comment(self):
        comments = [c("t1", "allan.menguy", "2026-09-13T19:00:00Z", "@coderabbitai review"),
                    c("t2", "maxime", "2026-09-13T19:05:00Z", "lgtm")]
        self.assertEqual(classify(comments).human_ids, ["t2"])


class TestReviewerStatus(unittest.TestCase):
    """The commit status the reviewer posts on the head: the one signal that
    exists before the bot has said anything, and the one that is not prose."""

    NOW = datetime(2026, 9, 23, 18, 24, 40, tzinfo=timezone.utc)

    def st(self, state, at="2026-09-23T18:22:09.387Z", name="CodeRabbit",
           description="Review in progress"):
        return {"name": name, "state": state, "description": description, "at": at}

    def test_live_mr_39_was_busy_when_the_trigger_went_up(self):
        from classify import reviewer_status
        self.assertTrue(reviewer_status([self.st("running")], now=self.NOW).busy)

    def test_github_calls_it_pending(self):
        from classify import reviewer_status
        self.assertTrue(reviewer_status([self.st("pending")], now=self.NOW).busy)

    def test_the_newest_status_wins(self):
        """GitHub keeps the history: pending, then success, on one commit."""
        from classify import reviewer_status
        got = reviewer_status([self.st("success", at="2026-09-23T18:30:41Z",
                                       description="Review completed"),
                               self.st("pending", at="2026-09-23T18:22:09Z")],
                              now=self.NOW)
        self.assertFalse(got.busy)
        self.assertEqual(got.description, "Review completed")

    def test_success_is_not_a_verdict(self):
        """Rate limited and skipped also end as success, so the status can only
        say whether a review is running, never what it found."""
        from classify import reviewer_status
        for desc in ("Review rate limited",
                     "Review skipped: 313 files exceed the limit of 150"):
            got = reviewer_status([self.st("success", description=desc)], now=self.NOW)
            self.assertEqual((got.state, got.busy), ("success", False))

    def test_other_ci_statuses_are_not_the_reviewer(self):
        from classify import reviewer_status
        got = reviewer_status([self.st("running", name="build")], now=self.NOW)
        self.assertEqual(got.state, "none")
        self.assertFalse(got.busy)

    def test_no_status_at_all_is_not_busy(self):
        from classify import reviewer_status
        self.assertFalse(reviewer_status([], now=self.NOW).busy)

    def test_a_status_busy_for_over_half_an_hour_is_stuck_not_busy(self):
        from classify import reviewer_status
        got = reviewer_status([self.st("running", at="2026-09-23T17:40:00Z")],
                              now=self.NOW)
        self.assertTrue(got.stuck)
        self.assertFalse(got.busy)


class TestVerdicts(unittest.TestCase):
    def test_d_newer_in_progress_beats_older_done(self):
        comments = REAL + [c("9", "coderabbitai", "2026-09-13T19:00:00Z",
                             "Currently processing new changes")]
        self.assertEqual(classify(comments, head=HEAD, after=PUSH).bot, "inprogress")

    def test_e_walkthrough_without_verdict_is_never_clean(self):
        """The failure that merges unreviewed code. 'none' must not read as done."""
        comments = [c("1", "coderabbitai", "2026-09-13T19:00:00Z",
                      "## Walkthrough\nThis PR adds a thing.\n## Changes\n| File |")]
        v = classify(comments, after="2026-09-13T18:00:00Z")
        self.assertEqual(v.bot, "none")
        self.assertFalse(v.finished)

    def test_f_too_many_files_is_terminal(self):
        for body in ("Review skipped: Too many files",
                     "Review skipped. 812 files exceed the limit"):
            v = classify([c("1", "coderabbitai", "2026-09-13T19:00:00Z", body)],
                         after="2026-09-13T18:00:00Z")
            self.assertEqual(v.bot, "skip")
            self.assertEqual(v.reason, "too_many_files")
            self.assertTrue(v.finished, "terminal states must stop the loop")

    def test_clean_pass_is_done_with_zero(self):
        v = classify([c("1", "coderabbitai", "2026-09-13T19:00:00Z",
                        "No actionable comments were generated in the recent review")],
                     after="2026-09-13T18:00:00Z")
        self.assertEqual((v.bot, v.actionable), ("done", "0"))
        self.assertTrue(v.finished)

    def test_rate_limit_is_not_finished(self):
        v = classify([c("1", "coderabbitai", "2026-09-13T19:00:00Z",
                        "Review limit reached for this hour")],
                     after="2026-09-13T18:00:00Z")
        self.assertEqual(v.bot, "none", "a notice is not a review state")
        self.assertEqual(v.blocked, "ratelimit")
        self.assertFalse(v.finished, "rate limit must not stop the loop")


class TestNoticesDoNotShadowVerdicts(unittest.TestCase):
    """Regression from a live PR. A clean verdict was followed by
    three rate-limit replies to redundant re-triggers. Treating those as the
    current state hid a valid verdict that named the current head, and the
    watcher kept re-triggering, producing more notices that hid it harder."""

    LIVE = [
        c("1", "coderabbitai", "2026-09-14T02:57:21Z",
          "No actionable comments were generated\n\nReviewed up to " + HEAD),
        c("2", "coderabbitai", "2026-09-14T03:02:44Z", "Review limit reached"),
        c("3", "coderabbitai", "2026-09-14T03:05:12Z", "Review limit reached"),
        c("4", "coderabbitai", "2026-09-14T03:05:54Z", "Review limit reached"),
    ]

    def test_rate_limit_does_not_hide_an_earlier_clean_verdict(self):
        v = classify(self.LIVE, head=HEAD)
        self.assertEqual(v.bot, "done")
        self.assertEqual(v.actionable, "0")
        self.assertEqual(v.blocked, "ratelimit", "still reported, just not as the state")
        self.assertEqual(v.stale_risk, 0, "the verdict names the current head")
        self.assertTrue(v.finished)

    def test_rate_limit_suppresses_further_triggering(self):
        self.assertFalse(classify(self.LIVE, head=HEAD).may_trigger,
                         "re-requesting while rate limited is pure waste")

    def test_in_progress_still_supersedes_an_earlier_verdict(self):
        """inprogress IS a state change: a new review started, so the old verdict
        no longer describes the head being reviewed."""
        comments = self.LIVE + [c("5", "coderabbitai", "2026-09-14T04:00:00Z",
                                  "Currently processing new changes")]
        v = classify(comments, head=HEAD)
        self.assertEqual(v.bot, "inprogress")
        self.assertFalse(v.may_trigger)

    def test_rate_limit_alone_is_not_a_verdict(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T03:00:00Z",
                        "Review limit reached")], head=HEAD)
        self.assertEqual(v.bot, "none")
        self.assertEqual(v.blocked, "ratelimit")
        self.assertFalse(v.finished, "no review happened, so nothing is finished")

    def test_needs_request_is_also_a_notice(self):
        comments = [c("1", "coderabbitai", "2026-09-14T02:00:00Z",
                      "Actionable comments posted: 2"),
                    c("2", "coderabbitai", "2026-09-14T03:00:00Z",
                      "Auto reviews are disabled on this repository")]
        v = classify(comments, head=HEAD)
        self.assertEqual((v.bot, v.actionable, v.blocked), ("done", "2", "needsreq"))
        self.assertTrue(v.may_trigger, "needsreq means ask, unlike ratelimit")


class TestAlreadyReviewedBreaksTheDeadlock(unittest.TestCase):
    """Regression from a live MR. The newest bot comment was
    "Already reviewed the last commit", which had no pattern, so the verdict read
    stale_risk=1. The skill says never act on a stale verdict and re-trigger; the
    bot refuses again for the same reason; nothing ever changes. The agent sat
    there."""

    ALREADY = ("⚠️ Action not completed\n\nAlready reviewed the last commit. Use "
               "`@coderabbitai full review` to rerun a review of the entire "
               "changeset.\n\nNote: CodeRabbit is an incremental review system and "
               "does not re-review already reviewed commits.")

    LIVE = [
        c("1", "coderabbitai", "2026-09-14T13:24:25Z", "Actionable comments posted: 1"),
        c("2", "coderabbitai", "2026-09-14T13:25:54Z", "Review rate limited."),
        c("3", "coderabbitai", "2026-09-14T13:40:30Z", ALREADY),
    ]

    def test_already_reviewed_clears_stale_risk(self):
        """The reviewer states the current head is covered - better evidence than
        any SHA heuristic, and the whole question stale_risk exists to guess at."""
        v = classify(self.LIVE, head=HEAD)
        self.assertEqual(v.bot, "done")
        self.assertEqual(v.actionable, "1")
        self.assertEqual(v.blocked, "already")
        self.assertEqual(v.stale_risk, 0)
        self.assertTrue(v.finished, "the loop must be able to move on")

    def test_it_does_not_shadow_the_verdict(self):
        self.assertEqual(classify(self.LIVE, head=HEAD).bot, "done",
                         "a refusal is a notice, not a review state")

    def test_retriggering_is_pointless_and_suppressed(self):
        self.assertFalse(classify(self.LIVE, head=HEAD).may_trigger,
                         "an incremental review of a reviewed head is always refused")

    def test_the_boilerplate_note_alone_is_not_the_marker(self):
        """Every refusal carries 'does not re-review already reviewed commits',
        including the rate-limited one. Matching that would misclassify them."""
        boilerplate = ("Review rate limited.\n\n> Note: CodeRabbit is an incremental "
                       "review system and does not re-review already reviewed commits.")
        v = classify([c("1", "coderabbitai", "2026-09-14T13:00:00Z", boilerplate)])
        self.assertEqual(v.blocked, "ratelimit", "must not read as 'already'")

    def test_already_alone_is_not_a_verdict(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T13:40:00Z", self.ALREADY)])
        self.assertEqual(v.bot, "none")
        self.assertEqual(v.blocked, "already")
        self.assertFalse(v.finished, "no verdict was read, so nothing is finished")


class TestOneCommentCarryingTwoRanges(unittest.TestCase):
    """The most dangerous misread found so far.

    CodeRabbit keeps ONE summary banner and edits it in place. It can hold a
    finished verdict for an older range AND a rate limit for the current one:

        No actionable comments were generated ... between 212deb4a and 7eb3c5f0
        Review limit reached ...              ... between 7eb3c5f0 and 8a1f0a52

    PATTERNS order put done0 before ratelimit, so this classified as a CLEAN PASS
    with stale_risk=0 - which routes straight to merge, on a head no reviewer had
    looked at. Only a human reading the banner caught it."""

    BANNER = ("<!-- summarize by coderabbit.ai -->\n"
              "> No actionable comments were generated in the recent review.\n"
              "> Reviewing files that changed between 212deb4ad and 7eb3c5f08\n\n"
              "> Review limit reached. Or wait 47 minutes for your next included review.\n"
              "> Reviewing files that changed between 7eb3c5f08 and 8a1f0a523\n")

    def banner(self, created="2026-09-14T16:01:27Z", updated="2026-09-14T16:16:01Z"):
        return [{"id": "1", "author": "coderabbitai", "created_at": created,
                 "updated_at": updated, "body": self.BANNER}]

    def test_the_notice_wins_over_the_verdict_in_one_comment(self):
        v = classify(self.banner(), head="8a1f0a523")
        self.assertEqual(v.blocked, "ratelimit")
        self.assertNotEqual(v.bot, "done", "a rate-limited head is NOT a clean pass")
        self.assertFalse(v.finished, "must not route to merge")

    def test_it_does_not_claim_the_head_is_covered(self):
        v = classify(self.banner(), head="8a1f0a523")
        self.assertEqual(v.stale_risk, 1)

    def test_the_stated_window_is_still_read(self):
        # Anchored to the banner's EDIT stamp (16:16:01), not its creation.
        v = classify(self.banner(),
                     now=datetime(2026, 9, 14, 16, 16, 1, tzinfo=timezone.utc))
        self.assertEqual(v.retry_in, 47 * 60)

    def test_an_edited_banner_is_current_from_its_edit_not_its_creation(self):
        """created_at is 16:01 but the content became true at 16:16. Anchoring on
        created_at drops a fresh notice out of the window entirely."""
        v = classify(self.banner(), after="2026-09-14T16:10:00Z")
        self.assertEqual(v.blocked, "ratelimit",
                         "the edit is newer than the push, so it must be in scope")
        self.assertNotEqual(v.bot, "stale")

    def test_a_live_review_in_the_same_body_beats_an_old_verdict(self):
        """Second instance of the same trap, caught by the user mid-merge.

        The banner held `review in progress` for 7eb3c5f08..8a1f0a523 (current)
        and a clean pass for 212deb4ad..7eb3c5f08 (old). My first fix made
        NOTICES beat verdicts - but inprogress is a STATE, so PATTERNS order
        handed the win back to done0 and it read as a clean pass again."""
        banner = ("<!-- walkthrough_start -->\n"
                  "> review in progress ... between 7eb3c5f08 and 8a1f0a523\n"
                  "<!-- recent_review -->\n"
                  "> No actionable comments were generated ... between 212deb4ad and 7eb3c5f08\n")
        cs = [{"id": "1", "author": "coderabbitai",
               "created_at": "2026-09-14T16:01:00Z",
               "updated_at": "2026-09-14T16:20:15Z", "body": banner}]
        v = classify(cs, head="8a1f0a523", after="2026-09-14T16:18:00Z")
        self.assertEqual(v.bot, "inprogress", "a running review outranks any verdict")
        self.assertFalse(v.finished, "must not route to merge")
        self.assertFalse(v.may_trigger, "never re-trigger a running review")

    def test_precedence_is_inprogress_then_notice_then_verdict(self):
        body = ("review in progress\nReview limit reached\n"
                "No actionable comments were generated")
        self.assertEqual(classify([c("1", "coderabbitai", "2026-09-14T16:00:00Z",
                                     body)]).bot, "inprogress")
        body2 = "Review limit reached\nNo actionable comments were generated"
        v = classify([c("1", "coderabbitai", "2026-09-14T16:00:00Z", body2)])
        self.assertEqual(v.blocked, "ratelimit")
        self.assertNotEqual(v.bot, "done")

    def test_a_mixed_body_never_claims_the_current_head(self):
        body = ("Review limit reached\nNo actionable comments were generated\n"
                "Reviewed up to 8a1f0a523")
        v = classify([c("1", "coderabbitai", "2026-09-14T16:00:00Z", body)],
                     head="8a1f0a523")
        self.assertEqual(v.stale_risk, 1,
                         "the head appearing in a mixed body proves nothing")

    def test_a_pure_verdict_is_still_a_verdict(self):
        """The notice-wins rule must not swallow an ordinary clean pass."""
        v = classify([c("1", "coderabbitai", "2026-09-14T16:00:00Z",
                        "No actionable comments were generated in the recent review")],
                     after="2026-09-14T15:00:00Z")
        self.assertEqual((v.bot, v.actionable), ("done", "0"))
        self.assertTrue(v.finished)


class TestARunningReviewIgnoresTheWindow(unittest.TestCase):
    """Regression. CodeRabbit started reviewing at 12:44. A rebase pushed
    at 12:47:53, so the watch anchored --after there - which filtered the running
    review OUT of the window. The watcher saw nothing, called it idle, and nudged
    at 12:50 and again at 12:53. The review had been running the whole time and
    delivered its verdict at 12:55.

    `after` answers "does this verdict cover my head". That is the wrong question
    for "is the reviewer busy", and using one window for both caused this."""

    RUNNING = [c("1", "coderabbitai", "2026-09-15T12:44:16Z",
                 "<!-- walkthrough_start -->\n> review in progress")]
    PUSH_AFTER = "2026-09-15T12:47:53Z"

    def test_a_review_started_before_the_push_still_counts_as_running(self):
        v = classify(self.RUNNING, after=self.PUSH_AFTER)
        self.assertTrue(v.running)
        self.assertEqual(v.in_window, 0, "it is genuinely outside the verdict window")

    def test_it_is_not_idle_and_must_not_be_nudged(self):
        v = classify(self.RUNNING, after=self.PUSH_AFTER)
        self.assertFalse(v.idle)
        self.assertFalse(v.may_trigger,
                         "nudging a running review is refused and burns quota")

    def test_the_verdict_is_still_window_scoped(self):
        """Only `running` ignores the window. A pre-push VERDICT stays stale, or
        we are back to merging on a review of code we replaced."""
        old_verdict = [c("1", "coderabbitai", "2026-09-15T12:44:16Z",
                         "Actionable comments posted: 5")]
        v = classify(old_verdict, after=self.PUSH_AFTER)
        self.assertEqual(v.bot, "stale")
        self.assertFalse(v.finished)

    def test_it_clears_when_the_banner_stops_saying_so(self):
        finished = [c("1", "coderabbitai", "2026-09-15T12:55:26Z",
                      "Actionable comments posted: 5")]
        v = classify(finished, after=self.PUSH_AFTER)
        self.assertFalse(v.running)
        self.assertTrue(v.may_trigger)


class TestPaidOfferIsSurfaced(unittest.TestCase):
    """The agent must be able to tell the user that a paid option exists, without
    being able to take it."""

    OFFER = ("Review limit reached for this hour.\n\n"
             "Run an on-demand review: Comment `@coderabbitai review --use-credits`.")

    def test_credit_offer_is_flagged(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T03:00:00Z", self.OFFER)])
        self.assertEqual(v.blocked, "ratelimit")
        self.assertTrue(v.credits_offered)

    def test_plain_rate_limit_does_not_flag_credits(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T03:00:00Z",
                        "Review limit reached for this hour.")])
        self.assertEqual(v.blocked, "ratelimit")
        self.assertFalse(v.credits_offered)

    def test_a_paid_offer_never_makes_the_review_finished(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T03:00:00Z", self.OFFER)])
        self.assertFalse(v.finished)
        self.assertFalse(v.may_trigger, "still rate limited; a free retry is waste")


class TestStatedRetryWindow(unittest.TestCase):
    """Use the window the reviewer states. A hardcoded 'one hour' idled ~35
    minutes past a stated 30-minute window in a real session."""

    # Verbatim shapes seen on a live PR.
    BANNER = ("> [!WARNING]\n> ## Review limit reached\n> \n"
              "> **Next included review available in 51 minutes.**\n")
    TERSE = ("<details>\n<summary>Action not completed</summary>\n\n"
             "Review rate limited.\n\n> Note: CodeRabbit is an incremental "
             "review system and does not re-review already reviewed commits.\n")

    def test_parses_minutes(self):
        self.assertEqual(parse_retry(self.BANNER), 51 * 60)

    def test_parses_compound_wait(self):
        self.assertEqual(
            parse_retry("Please wait 11 minutes and 4 seconds before requesting"),
            11 * 60 + 4)

    def test_parses_hours(self):
        self.assertEqual(parse_retry("try again in 2 hours"), 7200)

    def test_terse_refusal_states_no_window(self):
        """The reply to an explicit trigger carries no window; only the banner
        does. Reading just the refusal finds nothing."""
        self.assertIsNone(parse_retry(self.TERSE))

    def test_window_is_taken_from_whichever_comment_has_it(self):
        now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
        comments = [
            c("1", "coderabbitai", "2026-09-14T02:57:00Z", self.BANNER),
            c("2", "coderabbitai", "2026-09-14T02:58:00Z", self.TERSE),
        ]
        v = classify(comments, now=now)
        self.assertEqual(v.retry_at,
                         datetime(2026, 9, 14, 3, 48, tzinfo=timezone.utc))
        self.assertEqual(v.retry_in, 48 * 60)

    def test_trigger_is_suppressed_until_the_window_passes(self):
        comments = [c("1", "coderabbitai", "2026-09-14T02:57:00Z", self.BANNER)]
        before = classify(comments, now=datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc))
        self.assertFalse(before.may_trigger)
        self.assertEqual(before.retry_in, 48 * 60)

    def test_trigger_resumes_once_the_window_has_elapsed(self):
        """Without this the watcher waits forever: the old notice stays the
        newest comment long after the limit expired."""
        comments = [c("1", "coderabbitai", "2026-09-14T02:57:00Z", self.BANNER)]
        after = classify(comments, now=datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(after.retry_in, 0)
        self.assertTrue(after.may_trigger)

    def test_rate_limit_with_no_stated_window_stays_suppressed(self):
        v = classify([c("1", "coderabbitai", "2026-09-14T03:00:00Z",
                        "Review limit reached.")])
        self.assertIsNone(v.retry_at)
        self.assertFalse(v.may_trigger, "no window stated: do not guess one")


class TestHumans(unittest.TestCase):
    def test_humans_separated_from_bots(self):
        v = classify(REAL, head=HEAD)
        self.assertEqual(v.human_ids, ["4"])
        self.assertEqual(v.human_latest, "allan.menguy")

    def test_ignore_list_excludes_the_author(self):
        v = classify(REAL, head=HEAD, ignore=["Allan.Menguy"])
        self.assertEqual(v.human_ids, [])

    def test_bot_logins_recognised(self):
        for login in ("coderabbitai", "coderabbitai[bot]", "CodeRabbitAI", "some-bot"):
            v = classify([c("1", login, "2026-09-13T19:00:00Z",
                            "Actionable comments posted: 3")],
                         after="2026-09-13T18:00:00Z")
            self.assertEqual(v.bot, "done", f"{login} should classify as a bot")

    def test_no_comments_at_all(self):
        v = classify([], after=PUSH)
        self.assertEqual((v.bot, v.stale_risk, v.human_ids), ("none", 0, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAnAcceptedRequestSupersedesTheLimit(unittest.TestCase):
    """Regression from a live MR: a free request was refused for quota, a
    paid one was accepted ("Review triggered"), and the older refusal was still
    read as the notice in force, so the watch reported rate_limited while the
    paid review was running."""

    REFUSED = ("⚠️ Action not completed Review rate limited. > Note: CodeRabbit is an "
               "incremental review system and does not re-review already reviewed commits.")
    ACCEPTED = ("✅ Action performed Review triggered. > Note: CodeRabbit is an "
                "incremental review system and does not re-review already reviewed commits.")

    def thread(self):
        return [c("1", "me", "2026-09-30T23:57:38Z", "@coderabbitai review"),
                c("2", "coderabbitai", "2026-09-30T23:57:52Z", self.REFUSED),
                c("3", "me", "2026-09-30T23:58:00Z", "@coderabbitai review --use-credits"),
                c("4", "coderabbitai", "2026-09-30T23:58:26Z", self.ACCEPTED)]

    def test_the_accepted_request_means_a_review_is_under_way(self):
        v = classify(self.thread(), after="2026-09-30T23:36:43Z")
        self.assertEqual(v.blocked, "")
        self.assertEqual(v.bot, "inprogress")

    def test_the_verdict_that_follows_still_wins(self):
        comments = self.thread() + [c("5", "coderabbitai", "2026-09-30T00:05:00Z",
                                      "No actionable comments were generated")]
        comments[-1]["created_at"] = "2026-10-01T00:05:00Z"
        v = classify(comments, after="2026-09-30T23:36:43Z")
        self.assertEqual(v.bot, "done")
        self.assertEqual(v.actionable, "0")

    def test_the_acknowledgement_never_reads_as_a_running_review(self):
        v = classify(self.thread(), after="2026-09-30T23:36:43Z")
        self.assertFalse(v.running, "the reply is never edited, so it would stay busy forever")


class TestARequestAfterTheNoticeIsStillPending(unittest.TestCase):
    """Regression from a live MR: the watch read a rate limit that predated a
    paid request it had just posted, and handed back before the reviewer could
    answer it."""

    BANNER = c("1", "coderabbitai", "2026-10-01T00:19:30Z",
               "Review limit reached for this hour. Next included review available in 10 minutes.")

    def test_an_unanswered_request_lifts_the_notice(self):
        v = classify([self.BANNER,
                      c("2", "me", "2026-10-01T00:20:28Z", "@coderabbitai review --use-credits")],
                     after="2026-10-01T00:19:10Z")
        self.assertEqual(v.blocked, "")

    def test_a_refusal_of_that_request_still_counts(self):
        v = classify([self.BANNER,
                      c("2", "me", "2026-10-01T00:20:28Z", "@coderabbitai review"),
                      c("3", "coderabbitai", "2026-10-01T00:20:40Z",
                        "Action not completed Review rate limited.")],
                     after="2026-10-01T00:19:10Z")
        self.assertEqual(v.blocked, "ratelimit")


class TestABannerEditIsNoAnswerToARequest(unittest.TestCase):
    """Regression from a live MR: the push's auto review was refused and the
    banner rewritten after a paid request, which read as the request refused."""

    def test_an_edited_banner_does_not_reblock(self):
        banner = c("1", "coderabbitai", "2026-09-26T05:22:00Z",
                   "## Review limit reached\nRun an on-demand review: `@coderabbitai review --use-credits`")
        banner["updated_at"] = "2026-10-01T00:50:17Z"
        v = classify([banner,
                      c("2", "me", "2026-10-01T00:49:32Z", "@coderabbitai review --use-credits")],
                     after="2026-10-01T00:49:10Z")
        self.assertEqual(v.blocked, "")


class TestAFailedReviewIsNotSilence(unittest.TestCase):
    """Regression from a live MR: a paid request answered with "Review failed"
    matched nothing and read as silence for the whole watch."""

    def test_review_failed_is_a_notice(self):
        v = classify([c("1", "me", "2026-10-01T12:48:23Z", "@coderabbitai review --use-credits"),
                      c("2", "coderabbitai", "2026-10-01T12:48:41Z",
                        "❌ Action failed Review failed. > Note: CodeRabbit is an incremental review system.")],
                     after="2026-10-01T12:40:00Z")
        self.assertEqual(v.blocked, "failed")


class TestARequestIsDatedByItsCreation(unittest.TestCase):
    """Regression from a live MR: the reviewer edited the request when it
    reacted, after posting its refusal, which lifted the refusal."""

    def test_an_edited_request_does_not_lift_its_own_refusal(self):
        request = c("1", "me", "2026-10-01T12:48:23Z", "@coderabbitai review --use-credits")
        request["updated_at"] = "2026-10-01T12:48:42Z"
        v = classify([request,
                      c("2", "coderabbitai", "2026-10-01T12:48:41Z", "❌ Action failed\n\nReview failed.")],
                     after="2026-10-01T12:40:00Z")
        self.assertEqual(v.blocked, "failed")
