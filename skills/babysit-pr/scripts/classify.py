"""Decide what an AI reviewer has actually said about the current head.

The machine-readable half of references/ai-reviewers.md; change both together.

The rule that breaks everything if you get it wrong: a review is finished ONLY
on an explicit verdict. CodeRabbit posts its summary and walkthrough first and
the verdict minutes later, so "no findings yet" means "still working", never
"clean pass".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Order matters only within one comment; across comments, recency wins - but only
# among STATE markers. See below.
PATTERNS = [
    ("done",       re.compile(r"Actionable comments posted:\s*(\d+)")),
    ("done0",      re.compile(r"No actionable comments were generated")),
    ("skip",       re.compile(r"Too many files|files exceed the limit")),
    # "Already reviewed the last commit. Use @coderabbitai full review to rerun."
    # Match only that sentence: the boilerplate "does not re-review already
    # reviewed commits" note rides along on other refusals too, including the
    # rate-limited one, and would misclassify them.
    ("already",    re.compile(r"Already reviewed the last commit")),
    ("inprogress", re.compile(r"Currently processing new changes|review in progress"
                              r"|Currently reviewing")),
    # The reply to an accepted request, free or paid. It is never edited, so it
    # cannot feed the self-clearing `running` flag; it only wins the newest-first
    # scan until a verdict lands after it. Without it, the older rate-limit reply
    # stayed "in force" on a live MR while a paid review was running.
    ("accepted",   re.compile(r"Review triggered")),
    ("ratelimit",  re.compile(r"Review limit reached|rate limit", re.I)),
    # "Action failed  Review failed." - the reviewer accepted nothing and will not
    # report back. Unrecognised, it read as silence and a watch on a live MR
    # waited out its whole 25-minute timeout behind a paid request that had failed.
    ("failed",     re.compile(r"Action failed\W+Review failed|Review failed\.")),
    ("needsreq",   re.compile(r"Auto reviews are disabled")),
]

# A STATE marker reports what the reviewer did to a head: it supersedes whatever
# it said before. A NOTICE reports that the reviewer did NOT start a review, so
# it supersedes nothing and must never shadow an earlier verdict.
#
# Measured on a real PR: a clean "No actionable comments were generated" followed
# by three "rate limit" replies to redundant re-triggers classified as
# `ratelimit`, hiding a valid verdict that named the current head. The watcher
# then kept re-triggering, which produced more rate-limit notices, which hid the
# verdict harder. Notices are not events.
PATTERNS_BY_NAME = {name: rx for name, rx in PATTERNS}

STATE = {"done", "done0", "skip", "inprogress", "accepted"}
NOTICE = {"ratelimit", "needsreq", "already", "failed"}

# A rate-limited CodeRabbit offers a way out that SPENDS MONEY:
#   "Run an on-demand review: Comment @coderabbitai review --use-credits."
# Detected so the agent can report the offer, and refused at the write boundary
# (forge.post_guard) so no agent can accept it on the user's behalf.
CREDITS_OFFERED = re.compile(r"--use-credits|use\s+credits", re.I)
# Narrow on purpose: flag-shaped tokens that are known to bill. Prose about
# credits must stay postable, or every reply discussing quota gets blocked.
SPENDS_MONEY = re.compile(r"--use-credits\b|--credits\b|--paid\b", re.I)

# A request for a review is a comment by a HUMAN, so it is invisible to any count
# of the reviewer's own output. Right after a trigger the bot has said nothing
# yet, which looks identical to "nothing is queued" - and that is how a watcher
# posted a second trigger 20 seconds after the first.
TRIGGER_REQUEST = re.compile(r"@coderabbit(ai)?\s+(full\s+)?review", re.I)

# A comment that is ONLY a directive at the bot - "@coderabbitai review",
# "@coderabbitai pause". Bounded on purpose: a paragraph that merely mentions the
# bot is a real remark and must not be swallowed.
BOT_COMMAND = re.compile(r"^@coderabbit(ai)?\s+[\w\s-]{0,40}$", re.I)


def is_bot_command(body):
    """True when the whole comment is a command aimed at the reviewer.

    These are OUR side of the conversation, not review feedback. On GitLab such a
    note is a resolvable discussion, so counting it inflates `unresolved` - and a
    merge precondition of `unresolved=0` then blocks on the very comment that
    asked for the review, with every retry making it worse."""
    return bool(BOT_COMMAND.match((body or "").strip()))

# The reviewer states how long to wait. USE IT instead of assuming a quota
# period: a hardcoded "one hour" idled ~35 minutes past a stated 30-minute
# window in one session, and the only way to notice is to read the message.
#
# It is stated in the rate-limit BANNER, not in the terse reply to an explicit
# `@coderabbitai review` - that reply is 424 characters and carries no window at
# all. So scan every bot comment, not just the one that matched the notice.
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
RETRY_WINDOW = re.compile(
    r"(?:available|wait|try again|retry|resets?)\b[^.\n]{0,40}?"
    r"(\d+)\s*(second|minute|hour|day)s?"
    r"(?:\s*(?:and|,)\s*(\d+)\s*(second|minute|hour|day)s?)?", re.I)


def parse_retry(body):
    """Seconds to wait, as stated in the comment. None if it does not say."""
    m = RETRY_WINDOW.search(body or "")
    if not m:
        return None
    total = int(m.group(1)) * UNITS[m.group(2).lower()]
    if m.group(3):
        total += int(m.group(3)) * UNITS[m.group(4).lower()]
    return total

TERMINAL = {"skip"}

# The reviewer also posts a COMMIT STATUS on the head it reviews: context
# "CodeRabbit" on GitHub, name "CodeRabbit" on GitLab. Unlike every marker above
# it is not prose, and it exists before the bot has written a single comment.
# Measured on a live MR: the status went `running` at 18:22:09, a trigger went
# up at 18:24:40, and the first bot comment only arrived at 18:26:42 - so no
# reading of comments could have known a review was already under way.
#
# Only the busy states are trustworthy. `success` is overloaded: "Review
# completed", "Review rate limited" and "Review skipped: 313 files exceed the
# limit" all end as success, so the outcome still comes from the verdict.
REVIEWER_STATUS = re.compile(r"coderabbit", re.I)
BUSY = {"pending", "running"}
# Measured runs take 5 to 9 minutes. A status still busy after this is a review
# that died without reporting back, and honouring it would stop us asking for
# one forever.
STUCK_AFTER = 30 * 60


@dataclass
class ReviewerStatus:
    state: str = "none"        # the forge's own word: pending | running | success | ...
    description: str = ""
    at: object = None          # when the status was set
    stuck: bool = False

    @property
    def busy(self):
        """A review of this head is under way right now, so asking for one is a
        wasted request against the per-account quota."""
        return self.state in BUSY and not self.stuck


def reviewer_status(statuses, now=None):
    """statuses: [{name, state, description, at}] for ONE commit, any order."""
    ours = [s for s in statuses if REVIEWER_STATUS.search(s.get("name") or "")]
    if not ours:
        return ReviewerStatus()
    newest = max(ours, key=lambda s: _ts(s.get("at")) or datetime.min.replace(
        tzinfo=timezone.utc))
    at = _ts(newest.get("at"))
    state = (newest.get("state") or "").lower()
    stuck = bool(state in BUSY and at
                 and ((now or _utcnow()) - at).total_seconds() > STUCK_AFTER)
    return ReviewerStatus(state=state, description=newest.get("description") or "",
                          at=at, stuck=stuck)


def is_bot(login):
    log = (login or "").lower()
    return log.endswith("[bot]") or log.endswith("-bot") or "coderabbit" in log


def _utcnow():
    return datetime.now(timezone.utc)


def _effective_ts(comment):
    """When this comment's CONTENT became true.

    CodeRabbit edits one summary banner in place, so `created_at` can be an hour
    older than what the comment now says. Anchoring currency on it either hides a
    fresh notice or dresses an old verdict up as new."""
    stamps = [t for t in (_ts(comment.get("created_at")), _ts(comment.get("updated_at")))
              if t is not None]
    return max(stamps) if stamps else None


def _ts(value):
    try:
        return datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Verdict:
    bot: str = "none"          # review STATE: done | skip | inprogress | stale | none
    actionable: str = ""
    reason: str = ""
    blocked: str = ""          # NOTICE in force: ratelimit | needsreq | already | ""
    # Which comment said so. A watcher that reacts to a notice (by rotating to
    # another account, say) must react once, not on every poll that still sees it.
    blocked_id: str = ""
    credits_offered: bool = False   # the bot offered a paid way past the limit
    retry_at: object = None         # datetime the reviewer said to come back
    retry_in: int = 0               # seconds from `now`, 0 once it has passed
    stale_risk: int = 0
    # Bot comments in the window that matched NO pattern. Normal early on (the
    # walkthrough precedes the verdict); a standing signal that the wording
    # changed when it is all we ever see.
    unmatched: int = 0
    last_unmatched: str = ""
    # Bot comments since the push. Zero means the reviewer has not spoken yet.
    in_window: int = 0
    # Review requests since the push, from anyone. A review can be queued with
    # the bot still silent, so `in_window == 0` alone does NOT mean idle.
    triggers_seen: int = 0
    # Is a review running RIGHT NOW? Computed over every bot comment, ignoring
    # `after`, because this is a fact about the reviewer and not about a head.
    running: bool = False
    human_ids: list = field(default_factory=list)
    human_latest: str = ""

    @property
    def finished(self):
        return self.bot == "done" or self.bot in TERMINAL

    @property
    def idle(self):
        """Nothing requested and nothing said since the push, so a nudge is not
        duplicating work already in flight."""
        return self.in_window == 0 and self.triggers_seen == 0 and not self.running

    @property
    def limit_lapsed(self):
        """A rate-limit notice whose stated window is over. It stays the newest
        bot comment until the next review, so it says nothing about the quota
        any more."""
        return (self.blocked == "ratelimit" and self.retry_at is not None
                and self.retry_in == 0)

    @property
    def may_trigger(self):
        """Re-requesting while a review runs is refused and still burns a slot
        from the per-account quota; while rate limited it is pure waste.

        Once the reviewer's own stated window has elapsed, triggering is allowed
        again even though the old notice is still the newest comment - otherwise
        the watcher waits forever on a limit that expired."""
        if self.bot == "inprogress" or self.running:
            return False
        if self.blocked == "ratelimit":
            return self.limit_lapsed
        if self.blocked == "already":
            # An incremental review of a head it has already reviewed is refused
            # every time. Only `full review` would run, and that re-reads the
            # whole changeset - a cost decision, not the watcher's to take.
            return False
        return True


def classify(comments, head=None, after=None, ignore=(), now=None):
    """comments: [{id, author, created_at, body}] oldest first.

    `after` (when you last pushed) is the authoritative currency signal: whatever
    the bot said after your push is about your push.

    Do NOT filter by head SHA. A CodeRabbit verdict cites the range it reviewed,
    not the branch tip, so a current verdict often omits the current head. Worse,
    a SHA-matching subset can EXCLUDE newer comments: measured on a real MR, that
    dropped the two newest verdicts and surfaced an older "Review limit reached"
    instead. SHA match is only a confidence marker, applied at the end.
    """
    ignore = {x.strip().lower() for x in ignore if x and x.strip()}
    after_ts = _ts(after)

    bots = [c for c in comments if is_bot(c.get("author"))]
    v = Verdict()

    if after_ts:
        scan = [c for c in bots if (_effective_ts(c) or after_ts) > after_ts]
        if not scan and bots:
            v.bot = "stale"
    else:
        # Nothing to anchor on. Read the newest comment anyway and flag it, rather
        # than preferring an older one that happens to match the head.
        scan = bots
        v.stale_risk = 1 if bots else 0

    winner = None
    blocked_at = None
    unmatched = []
    mixed_comment = False
    for comment in reversed(scan):  # newest first: a fresh in-progress beats an older done
        body = comment.get("body") or ""
        hits = [n for n, rx in PATTERNS if rx.search(body)]
        if not hits:
            unmatched.append(comment)
            continue
        # One comment can carry markers for SEVERAL ranges, because the banner is
        # a running summary that CodeRabbit edits in place:
        #
        #   review in progress                    7eb3c5f08..8a1f0a523  (current)
        #   No actionable comments were generated 212deb4ad..7eb3c5f08  (old)
        #
        # PATTERNS order is not a precedence. It put done0 first, so a clean pass
        # for an OLD range beat a live review of the current head - twice, on live
        # MRs, and both times that routes straight to merge on unreviewed code.
        #
        # Precedence is by what the marker says about the NEWEST attempt:
        #   inprogress - a review is running right now; beats everything
        #   notice     - no review started for the newest range
        #   verdict    - a finished result, possibly for an older range
        name = ("inprogress" if "inprogress" in hits or "accepted" in hits
                else next((n for n in hits if n in NOTICE), None) or hits[0])
        # A verdict sharing a body with either of the other two describes an older
        # range than they do, so it cannot claim the current head.
        if name not in NOTICE and name != "inprogress" and len(hits) > 1:
            mixed_comment = True
        if name in NOTICE:
            # Newer than any verdict we go on to find, so it is the notice in
            # force - but it does not decide the review state.
            if not v.blocked:
                v.blocked, v.blocked_id = name, str(comment.get("id") or "")
                # Created, not edited: the banner is rewritten in place each time
                # an AUTO review is refused, which is no answer to a request.
                blocked_at = _ts(comment.get("created_at"))
            if CREDITS_OFFERED.search(body):
                v.credits_offered = True
            continue
        match = next(rx for n, rx in PATTERNS if n == name).search(body)
        if name == "done":
            v.bot, v.actionable = "done", match.group(1)
        elif name == "done0":
            v.bot, v.actionable = "done", "0"
        elif name == "skip":
            v.bot, v.reason = "skip", "too_many_files"
        else:
            v.bot = name
        winner = comment
        break

    # A request posted after the notice has not been answered yet, so the notice
    # no longer describes the newest attempt. Measured on a live MR: a watch
    # started two seconds after a paid request read the older rate limit and
    # handed back, while the reviewer accepted the request twelve seconds later.
    # A refusal of the request is a new reply, created after it, so it still
    # counts. On one MR the push's auto review was refused and the banner was
    # rewritten between the request and its acceptance; that must not count.
    # The request is dated by its creation too: the reviewer touches it when it
    # reacts, and on another that edit landed after the "Review failed" reply and
    # lifted the very refusal answering it.
    if v.blocked and blocked_at and any(
            not is_bot(c.get("author"))
            and TRIGGER_REQUEST.search(c.get("body") or "")
            and (_ts(c.get("created_at")) or blocked_at) > blocked_at
            for c in comments):
        v.blocked, v.blocked_id = "", ""

    # The head appearing in the winning comment proves it describes the head we
    # have now. Its absence proves nothing.
    if winner and head and head in (winner.get("body") or "") and not mixed_comment:
        v.stale_risk = 0
    if mixed_comment:
        # The winning verdict shared its comment with a marker about a newer
        # range, so it is not about the head we have.
        v.stale_risk = 1

    # "Already reviewed the last commit" is the reviewer stating outright that
    # the current head is covered - a direct answer to the question stale_risk
    # exists to guess at, and better evidence than any SHA heuristic.
    #
    # Without this the loop deadlocks: the verdict reads stale, the skill says
    # never act on a stale verdict and re-trigger, the bot refuses again because
    # it has already reviewed that commit, and nothing ever changes.
    if v.blocked == "already":
        v.stale_risk = 0

    # Every marker this file matches is prose the reviewer could reword without
    # warning, and an unknown message currently reads as silence - which is
    # indistinguishable from "still working" and hangs the loop. Counting what we
    # failed to parse lets the caller say "I do not understand this" instead of
    # waiting forever on it.
    # `after` answers "does this verdict cover my head". It is the wrong question
    # for "is the reviewer busy", and using it for both is why a watcher nudged a
    # review that had started three minutes BEFORE the push it was anchored on,
    # twice, on a live MR. A running review is a fact about now.
    #
    # Self-clearing: the marker lives in the banner, which the bot edits when the
    # review finishes, so this goes False on its own.
    v.running = any(
        PATTERNS_BY_NAME["inprogress"].search(comment.get("body") or "")
        for comment in bots)

    v.in_window = len(scan)
    if after_ts:
        v.triggers_seen = sum(
            1 for comment in comments
            if not is_bot(comment.get("author"))
            and (_effective_ts(comment) or after_ts) > after_ts
            and TRIGGER_REQUEST.search(comment.get("body") or ""))
    else:
        v.triggers_seen = sum(
            1 for comment in comments
            if not is_bot(comment.get("author"))
            and TRIGGER_REQUEST.search(comment.get("body") or ""))
    v.unmatched = len(unmatched)
    if unmatched:
        head_line = next((ln.strip() for ln in (unmatched[0].get("body") or "").splitlines()
                          if ln.strip() and not ln.strip().startswith("<!--")), "")
        v.last_unmatched = head_line[:160]

    # The stated window, from whichever bot comment carries it, anchored to when
    # that comment was posted ("in 51 minutes" is relative to its own timestamp).
    for comment in reversed(bots):
        secs = parse_retry(comment.get("body"))
        if secs is None:
            continue
        base = _effective_ts(comment)
        if base is None:
            continue
        v.retry_at = base + timedelta(seconds=secs)
        v.retry_in = max(0, int((v.retry_at - (now or _utcnow())).total_seconds()))
        break

    # A comment that is only a directive at the reviewer is OUR side of the
    # conversation, whoever posted it. Author is the wrong test: with --rotate a
    # nudge goes out under a named account, and one process cannot recognise the
    # id of a comment another process posted, so the watcher woke itself up and
    # reported its own "@coderabbitai review" back as human_activity.
    humans = [c for c in comments
              if not is_bot(c.get("author"))
              and (c.get("author") or "").lower() not in ignore
              and not is_bot_command(c.get("body"))]
    v.human_ids = [str(c["id"]) for c in humans]
    v.human_latest = humans[-1]["author"] if humans else ""
    return v
