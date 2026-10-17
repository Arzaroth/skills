"""Unit tests for prctl.py. The forge is mocked; these pin the CLI contract that
the skills and the workflow depend on: argument shapes, output formats, and which
forge method each command reaches.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import prctl
from prctl import TRIGGERS
from datetime import datetime, timedelta, timezone

from classify import ReviewerStatus
from forge import AddressError, ForgeError


class FakeForge:
    host = "github.com"
    path = "o/r"
    slug = "github.com/o/r"
    term = "pull request"

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return FakeForge.RETURNS.get(name)
        return record

    RETURNS = {
        "describe": {"forge": "github", "host": "github.com", "path": "o/r",
                     "cli": "gh", "term": "pull request", "auth": "ok"},
        "state": {"number": 7, "url": "u", "head": "abc", "unresolved": 2},
        "threads": [{"id": "T1", "author": "bot", "resolved": False},
                    {"id": "T2", "author": "x", "resolved": True}],
        "reviews": [{"id": "1", "author": "coderabbitai", "body": "hi"}],
        "approvals": [{"author": "allan", "state": "approved"}],
        "find": 42,
        "create": 99,
        "merge": "deadbeef1",
        "rebase": "rebase requested",
        "refresh_diff": "diff recomputed",
        "use_credential": "alt1",
        "reviewer_status": ReviewerStatus(),
        "rotate_credential": "alt2",
    }


def posted(forge):
    return [c for c in forge.calls if c[0] == "comment"]


def run(argv, forge=None, repo="o/r"):
    """Commands that name a PR need an explicit repo; the tests pass one the way
    a caller must."""
    if repo and "--repo" not in argv:
        argv = ["--repo", repo] + list(argv)
    f = forge or FakeForge()
    buf = io.StringIO()
    with mock.patch.object(prctl, "detect", return_value=f), \
         mock.patch("sys.stderr"), redirect_stdout(buf):
        code = prctl.main(argv)
    return f, buf.getvalue(), code


class TestOutputFormats(unittest.TestCase):
    def test_detect_emits_kv(self):
        _, out, code = run(["detect"])
        self.assertEqual(code, 0)
        self.assertIn("forge=github", out)
        self.assertIn("auth=ok", out)
        for line in out.strip().splitlines():
            self.assertIn("=", line, "detect output must be parseable as KV")

    def test_state_emits_kv(self):
        _, out, _ = run(["state", "7"])
        self.assertIn("number=7", out)
        self.assertIn("unresolved=2", out)

    def test_threads_emit_one_json_object_per_line(self):
        _, out, _ = run(["threads", "7"])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(x)["id"] for x in lines], ["T1", "T2"])

    def test_threads_unresolved_flag_reaches_the_forge(self):
        f, _, _ = run(["threads", "7", "--unresolved"])
        name, args, kwargs = f.calls[0]
        self.assertEqual(name, "threads")
        self.assertTrue(kwargs["unresolved_only"])

    def test_reviews_and_approvals_are_ndjson(self):
        for cmd in ("reviews", "approvals"):
            _, out, _ = run([cmd, "7"])
            for line in out.strip().splitlines():
                json.loads(line)

    def test_find_prints_empty_string_when_absent(self):
        f = FakeForge()
        f.RETURNS = {**FakeForge.RETURNS, "find": None}
        with mock.patch.dict(FakeForge.RETURNS, {"find": None}):
            _, out, _ = run(["find", "some/branch"])
        self.assertEqual(out.strip(), "")

    def test_find_prints_the_number(self):
        _, out, _ = run(["find", "b"])
        self.assertEqual(out.strip(), "42")

    def test_create_prints_only_the_number(self):
        _, out, _ = run(["create", "--head", "h", "--base", "b", "--title", "t"])
        self.assertEqual(out.strip(), "99")


class TestRouting(unittest.TestCase):
    def test_pr_number_is_parsed_as_int(self):
        f, _, _ = run(["state", "7"])
        self.assertEqual(f.calls[0][1][0], 7)
        self.assertIsInstance(f.calls[0][1][0], int)

    def test_reply_passes_thread_and_body(self):
        f, _, _ = run(["reply", "7", "T1", "done in abc123"])
        self.assertEqual(f.calls[0],
                         ("reply", (7, "T1", "done in abc123", False), {}),
                         "allow_paid must default to False on every write")

    def test_resolve_passes_thread(self):
        f, _, _ = run(["resolve", "7", "T1"])
        self.assertEqual(f.calls[0], ("resolve", (7, "T1"), {}))

    def test_trigger_uses_the_known_phrase(self):
        f, _, _ = run(["trigger", "7"])
        self.assertEqual(posted(f), [("comment", (7, "@coderabbitai review"), {})])

    def test_trigger_full_review_variant(self):
        f, _, _ = run(["trigger", "7", "coderabbit-full"])
        self.assertEqual(posted(f)[0][1][1], "@coderabbitai full review")

    def test_unknown_reviewer_raises_rather_than_posting(self):
        f = FakeForge()
        with mock.patch.object(prctl, "detect", return_value=f), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(ForgeError):
                prctl.main(["trigger", "7", "mystery-bot"])
        self.assertEqual(f.calls, [], "must not post an unknown phrase")

    def test_merge_flags_reach_the_forge(self):
        f, out, _ = run(["merge", "7", "--squash", "--keep-branch"])
        self.assertEqual(f.calls[0], ("merge", (7, True, True), {}))
        self.assertIn("MERGED", out)

    def test_merge_defaults_are_merge_commit_and_delete_branch(self):
        f, _, _ = run(["merge", "7"])
        self.assertEqual(f.calls[0], ("merge", (7, False, False), {}))

    def test_review_comment_requires_a_body(self):
        f = FakeForge()
        with mock.patch.object(prctl, "detect", return_value=f), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(ForgeError):
                prctl.main(["review-comment", "7", "--file", "a.py", "--line", "3"])

    def test_review_comment_line_is_an_int(self):
        f, _, _ = run(["review-comment", "7", "--file", "a.py", "--line", "3",
                       "--body", "x"])
        self.assertEqual(f.calls[0], ("review_comment", (7, "a.py", 3, "x", False), {}))


class TestPaidActionsAreRefused(unittest.TestCase):
    """A rate-limited CodeRabbit suggests `@coderabbitai review --use-credits`,
    which bills. No agent may accept that offer on the user's behalf."""

    def test_comment_defaults_to_refusing_paid_text(self):
        f, _, _ = run(["comment", "7", "hi"])
        self.assertIs(f.calls[0][1][-1], False)

    def test_allow_paid_flag_is_threaded_through(self):
        f, _, _ = run(["--allow-paid", "comment", "7", "@coderabbitai review --use-credits"])
        self.assertIs(f.calls[0][1][-1], True)

    def test_trigger_cannot_reach_a_paid_phrase(self):
        """`trigger` is an allowlist, so no argument produces a billing comment."""
        for phrase in TRIGGERS.values():
            self.assertNotRegex(phrase, r"--use-credits|--credits|--paid")

    def test_trigger_never_passes_allow_paid(self):
        f, _, _ = run(["--allow-paid", "trigger", "7"])
        self.assertEqual(posted(f)[0][1][1], "@coderabbitai review",
                         "the trigger phrase is fixed regardless of --allow-paid")


class TestTriggerPhrasesAreRecognisableAsOurs(unittest.TestCase):
    def test_every_trigger_phrase_reads_as_a_bot_command(self):
        """What makes `human_activity` safe: the watcher filters our nudges by
        what they say. A phrase added here that does not match would start waking
        watchers up again."""
        from classify import is_bot_command
        for name, phrase in TRIGGERS.items():
            self.assertTrue(is_bot_command(phrase), f"{name}: {phrase!r}")


class TestTriggerChecksTheReviewerFirst(unittest.TestCase):
    """Regression from a live MR: the reviewer's status went `running` at
    18:22:09, a trigger went up at 18:24:40, and the first bot comment arrived at
    18:26:42. Only the status could have known, so trigger reads it first."""

    def forge_with(self, status):
        f = FakeForge()
        f.reviewer_status = lambda sha: (f.calls.append(("reviewer_status", (sha,), {}))
                                         or status)
        return f

    def running(self, minutes_ago=2, state="running"):
        return ReviewerStatus(state=state, description="Review in progress",
                              at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))

    def test_a_review_already_running_is_not_asked_for_again(self):
        f, out, code = run(["trigger", "7"], forge=self.forge_with(self.running()))
        self.assertEqual(posted(f), [])
        self.assertEqual(code, 0, "the review we wanted is happening; that is success")
        self.assertIn("already reviewing", out)

    def test_github_pending_is_busy_too(self):
        f, _, _ = run(["trigger", "7"], forge=self.forge_with(self.running(state="pending")))
        self.assertEqual(posted(f), [])

    def test_it_reads_the_status_of_the_current_head(self):
        f, _, _ = run(["trigger", "7"], forge=self.forge_with(self.running()))
        self.assertIn(("reviewer_status", ("abc",), {}), f.calls)

    def test_a_finished_review_does_not_block_asking(self):
        done = ReviewerStatus(state="success", description="Review completed",
                              at=datetime.now(timezone.utc))
        f, out, _ = run(["trigger", "7"], forge=self.forge_with(done))
        self.assertEqual(len(posted(f)), 1)
        self.assertIn("triggered", out)

    def test_a_status_stuck_for_half_an_hour_is_treated_as_dead(self):
        """Honouring it would stop us asking for a review forever."""
        stuck = self.running(minutes_ago=45)
        stuck.stuck = True
        f, _, _ = run(["trigger", "7"], forge=self.forge_with(stuck))
        self.assertEqual(len(posted(f)), 1)

    def test_an_unreadable_status_does_not_block_asking(self):
        f = FakeForge()
        def boom(sha):
            raise ForgeError("503")
        f.reviewer_status = boom
        f, _, _ = run(["trigger", "7"], forge=f)
        self.assertEqual(len(posted(f)), 1)

    def test_force_asks_anyway(self):
        f, _, _ = run(["trigger", "7", "--force"], forge=self.forge_with(self.running()))
        self.assertEqual(len(posted(f)), 1)

    def test_review_status_prints_kv(self):
        _, out, code = run(["review-status", "7"], forge=self.forge_with(self.running()))
        self.assertEqual(code, 0)
        for key in ("sha=abc", "state=running", "busy=true", "stuck=false"):
            self.assertIn(key, out)


class TestEveryPRIsAddressed(unittest.TestCase):
    """A bare number resolves against whatever repo the process stands in. Two
    watches reported a merge for PRs in unrelated repos that way, and `merge`
    would have merged one."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BABYSIT_REPO": ""})
        env.start()
        self.addCleanup(env.stop)

    def refuse(self, argv):
        f = FakeForge()
        with mock.patch.object(prctl, "detect", return_value=f), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(AddressError, msg=str(argv)) as ctx:
                prctl.main(argv)
        self.assertEqual(f.calls, [], f"{argv} must not reach the forge")
        return str(ctx.exception)

    def test_every_command_naming_a_pr_refuses_without_a_repo(self):
        for argv in (["state", "7"], ["merge", "7"], ["comment", "7", "hi"],
                     ["trigger", "7"], ["threads", "7"], ["reply", "7", "T1", "x"],
                     ["resolve", "7", "T1"], ["body", "7"], ["rebase", "7"],
                     ["approvals", "7"], ["reviews", "7"],
                     ["create", "--head", "b", "--base", "main", "--title", "t"]):
            self.refuse(argv)

    def test_the_refusal_is_one_copy_paste_from_correct(self):
        msg = self.refuse(["merge", "7"])
        self.assertIn("--repo github.com/o/r", msg)

    def test_repo_free_commands_still_work(self):
        for argv in (["detect"], ["find", "some-branch"]):
            _, _, code = run(argv, repo=None)
            self.assertEqual(code, 0, argv)

    def test_the_env_var_is_an_address_too(self):
        with mock.patch.dict(os.environ, {"BABYSIT_REPO": "o/r"}):
            f, _, code = run(["state", "7"], repo=None)
        self.assertEqual(code, 0)
        self.assertEqual(f.calls[0][0], "state")

    def test_the_repo_flag_reaches_detection(self):
        with mock.patch.object(prctl, "detect", return_value=FakeForge()) as det, \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            prctl.main(["--repo", "gl.example.com/g/r", "state", "7"])
        det.assert_called_once_with("gl.example.com/g/r")

    def test_the_cli_exits_64_not_1(self):
        """A usage error, distinguishable from a forge outage by a caller that
        only sees an exit code."""
        with mock.patch.object(prctl, "detect", return_value=FakeForge()), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                prctl.cli(["merge", "7"])
        self.assertEqual(ctx.exception.code, 64)


class TestBodyIsNeverAPath(unittest.TestCase):
    """Regression from a live MR: the author wrote the reply to a scratchpad file
    and passed the path where the body was wanted. prctl posted the path. The
    reviewer answered "je ne peux pas lire ce chemin local", twice, and the
    thread now carries local directory layout and a session UUID."""

    PATHS = ["/tmp/claude-1000/-home-user/abc/scratchpad/reply1.md",
             "./scratchpad/reply-63c9810c.txt",
             "~/notes/reply.md",
             "  /var/tmp/x.md  "]

    def test_bare_path_is_refused_for_every_body_command(self):
        for argv in (["reply", "7", "T1"], ["comment", "7"]):
            for path in self.PATHS:
                f = FakeForge()
                with mock.patch.object(prctl, "detect", return_value=f), \
                     mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
                    with self.assertRaises(ForgeError, msg=f"{argv} {path}"):
                        prctl.main(["--repo", "o/r", *argv, path])
                self.assertEqual(f.calls, [], "nothing may be posted")

    def test_the_refusal_names_the_fix(self):
        f = FakeForge()
        with mock.patch.object(prctl, "detect", return_value=f), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(ForgeError) as ctx:
                prctl.main(["--repo", "o/r", "comment", "7", "/tmp/x/reply.md"])
        self.assertIn("--body-file", str(ctx.exception))

    def test_body_file_posts_the_contents(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("Fixed in abc123.\n\n- widened the guard\n")
            name = fh.name
        try:
            f, _, _ = run(["reply", "7", "T1", "--body-file", name])
        finally:
            os.unlink(name)
        self.assertIn("widened the guard", f.calls[0][1][2])

    def test_comment_accepts_body_file_too(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("multi\nline\n")
            name = fh.name
        try:
            f, _, _ = run(["comment", "7", "--body-file", name])
        finally:
            os.unlink(name)
        self.assertEqual(f.calls[0][1][1], "multi\nline\n")

    def test_prose_mentioning_a_path_still_posts(self):
        """Only a body that is ONLY a path is refused; real replies name files."""
        for body in ("Fixed in /src/foo.rs, see line 12.",
                     "Moved to ./src/bar.rs",
                     "/tmp is not involved here, the bug was in the guard."):
            f, _, _ = run(["comment", "7", body])
            self.assertEqual(f.calls[0][1][1], body)

    def test_multiline_starting_with_a_path_still_posts(self):
        body = "/src/a.rs was wrong.\n\nFixed by widening the match."
        f, _, _ = run(["comment", "7", body])
        self.assertEqual(f.calls[0][1][1], body)

    def test_stdin_still_works(self):
        with mock.patch("sys.stdin", io.StringIO("from stdin\n")):
            f, _, _ = run(["comment", "7", "-"])
        self.assertEqual(f.calls[0][1][1], "from stdin\n")

    def test_missing_body_is_an_error_not_an_empty_post(self):
        f = FakeForge()
        with mock.patch.object(prctl, "detect", return_value=f), \
             mock.patch("sys.stderr"), redirect_stdout(io.StringIO()):
            with self.assertRaises(ForgeError):
                prctl.main(["comment", "7"])
        self.assertEqual(f.calls, [])


class TestCredentialFlags(unittest.TestCase):
    def test_as_applies_a_named_credential_before_acting(self):
        f, _, _ = run(["--as", "alt1", "comment", "7", "hi"])
        self.assertEqual([c[0] for c in f.calls], ["use_credential", "comment"])

    def test_rotate_rotates_before_acting(self):
        f, _, _ = run(["--rotate", "trigger", "7"])
        names = [c[0] for c in f.calls]
        self.assertEqual(names[0], "rotate_credential")
        self.assertEqual(names.count("comment"), 1)

    def test_no_credential_flag_means_default_auth(self):
        f, _, _ = run(["state", "7"])
        self.assertEqual([c[0] for c in f.calls], ["state"])


class TestBodyFromStdin(unittest.TestCase):
    def test_dash_reads_stdin(self):
        with mock.patch("sys.stdin", io.StringIO("multi\nline body\n")):
            f, _, _ = run(["comment", "7", "-"])
        self.assertEqual(f.calls[0][1][1], "multi\nline body\n")

    def test_literal_body_is_not_treated_as_a_path(self):
        f, _, _ = run(["comment", "7", "@coderabbitai review"])
        self.assertEqual(f.calls[0][1][1], "@coderabbitai review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
