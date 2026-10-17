"""Unit tests for relwatch.py. gh and the clock are mocked.

The preflight cases matter most: a tag that matches no pattern triggers nothing,
and that failure is indistinguishable from a slow CI queue until you go looking.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import relwatch
from relwatch import ReleaseError, main, tag_patterns

REAL_RELEASE_YML = """\
name: release
on:
  push:
    tags:
      - 'v*'
  workflow_dispatch:
    inputs:
      tag:
        description: 'Existing tag'
        required: true
permissions:
  contents: write
jobs:
  publish:
    steps:
      - uses: softprops/action-gh-release@v2
        with:
          tag_name: ${{ github.ref_name }}
"""

CI_YML = """\
name: ci
on:
  pull_request:
jobs:
  test:
    steps:
      - run: cargo test
"""


class TestTagPatterns(unittest.TestCase):
    def test_block_list(self):
        self.assertEqual(tag_patterns(REAL_RELEASE_YML), ["v*"])

    def test_inline_list(self):
        self.assertEqual(tag_patterns("on:\n  push:\n    tags: ['v*', \"rel-*\"]\n"),
                         ["v*", "rel-*"])

    def test_multiple_entries(self):
        y = "on:\n  push:\n    tags:\n      - 'v*'\n      - 'release-*'\n"
        self.assertEqual(tag_patterns(y), ["v*", "release-*"])

    def test_no_push_tags_means_none(self):
        self.assertEqual(tag_patterns(CI_YML), [])

    def test_branch_tags_under_pull_request_are_not_push_tags(self):
        y = "on:\n  pull_request:\n    tags:\n      - 'nope'\n"
        self.assertEqual(tag_patterns(y), [],
                         "only on.push.tags triggers on a tag push")

    def test_comments_and_blank_lines_ignored(self):
        y = "on:\n\n  push:\n    # only releases\n    tags:\n      - 'v*'\n"
        self.assertEqual(tag_patterns(y), ["v*"])

    def test_push_branches_without_tags(self):
        y = "on:\n  push:\n    branches:\n      - main\n"
        self.assertEqual(tag_patterns(y), [])


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wf = Path(self.tmp.name) / ".github" / "workflows"
        self.wf.mkdir(parents=True)
        (self.wf / "release.yml").write_text(REAL_RELEASE_YML)
        (self.wf / "ci.yml").write_text(CI_YML)

    def tearDown(self):
        self.tmp.cleanup()

    def preflight(self, tag):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["preflight", tag, "--dir", self.tmp.name])
        return buf.getvalue(), code

    def test_matching_tag_is_ok(self):
        out, code = self.preflight("v0.31.0")
        self.assertEqual(code, 0)
        self.assertIn("result=ok", out)
        self.assertIn("matches=yes", out)

    def test_missing_v_prefix_is_caught(self):
        """The classic silent failure: pushing this tag triggers nothing."""
        out, code = self.preflight("0.31.0")
        self.assertEqual(code, 2)
        self.assertIn("result=NO_MATCH", out)

    def test_ci_only_workflow_is_not_offered_as_a_match(self):
        out, _ = self.preflight("v1.0.0")
        self.assertNotIn("workflow=ci.yml", out)

    def test_triggering_workflow_without_a_release_step_is_flagged(self):
        (self.wf / "release.yml").write_text(
            "on:\n  push:\n    tags:\n      - 'v*'\njobs:\n  b:\n    steps:\n"
            "      - run: cargo build\n")
        out, code = self.preflight("v1.0.0")
        self.assertEqual(code, 2)
        self.assertIn("match_but_no_release_step", out)

    def test_no_workflows_at_all(self):
        for f in self.wf.iterdir():
            f.unlink()
        out, code = self.preflight("v1.0.0")
        self.assertEqual(code, 2)
        self.assertIn("no_tag_triggered_workflows", out)

    def test_missing_workflow_dir_raises(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ReleaseError):
                main(["preflight", "v1.0.0", "--dir", empty])


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1)


def run_watch(argv, *, runs, release=None):
    """runs/release are sequences consumed one per poll, last value repeating."""
    state = {"i": -1}

    def pick(seq):
        return seq[min(state["i"], len(seq) - 1)]

    def fake_find(repo, tag):
        state["i"] += 1
        return pick(runs)

    def fake_released(repo, tag):
        return pick(release or [None])

    buf = io.StringIO()
    with mock.patch.object(relwatch, "find_run", side_effect=fake_find), \
         mock.patch.object(relwatch, "released", side_effect=fake_released), \
         mock.patch.object(relwatch, "repo_slug", return_value="o/r"), \
         mock.patch.object(relwatch, "time", FakeClock()), \
         mock.patch("sys.stderr"), redirect_stdout(buf):
        code = main(["watch", *argv])
    return buf.getvalue().strip(), code


RUNNING = {"status": "in_progress", "conclusion": None, "html_url": "runurl"}
GREEN = {"status": "completed", "conclusion": "success", "html_url": "runurl"}
RED = {"status": "completed", "conclusion": "failure", "html_url": "runurl"}


class TestWatch(unittest.TestCase):
    def test_released_is_the_success_verdict(self):
        out, code = run_watch(["v1.0.0", "--interval", "0"],
                              runs=[GREEN], release=["https://gh/releases/v1.0.0"])
        self.assertIn("verdict=released", out)
        self.assertEqual(code, 0)

    def test_release_wins_even_before_the_run_is_found(self):
        out, code = run_watch(["v1.0.0", "--interval", "0"],
                              runs=[None], release=["https://gh/r"])
        self.assertIn("verdict=released", out)
        self.assertEqual(code, 0)

    def test_ci_failure_stops_immediately(self):
        out, code = run_watch(["v1.0.0", "--interval", "0"], runs=[RED])
        self.assertIn("verdict=ci_failed", out)
        self.assertIn("conclusion=failure", out)
        self.assertEqual(code, 1)

    def test_green_run_without_a_release_keeps_waiting(self):
        """The publish step can lag the build; green is not released."""
        out, code = run_watch(["v1.0.0", "--timeout", "5", "--interval", "0"],
                              runs=[GREEN], release=[None])
        self.assertIn("verdict=still_running", out)
        self.assertEqual(code, 3)

    def test_running_then_released(self):
        out, code = run_watch(["v1.0.0", "--interval", "0", "--timeout", "600"],
                              runs=[RUNNING, RUNNING, GREEN],
                              release=[None, None, "https://gh/r"])
        self.assertIn("verdict=released", out)
        self.assertEqual(code, 0)

    def test_no_run_after_grace_is_its_own_verdict(self):
        """A tag matching no pattern looks exactly like a slow queue, so it gets
        named rather than timing out as still_running."""
        out, code = run_watch(["v1.0.0", "--interval", "0", "--grace", "3",
                               "--timeout", "600"], runs=[None])
        self.assertIn("verdict=no_run", out)
        self.assertIn("check_tag_pattern", out)
        self.assertEqual(code, 2)

    def test_missing_run_inside_grace_does_not_give_up(self):
        out, code = run_watch(["v1.0.0", "--interval", "0", "--grace", "600",
                               "--timeout", "4"], runs=[None])
        self.assertIn("verdict=still_running", out)
        self.assertEqual(code, 3)

    def test_still_running_exit_code_means_watch_again(self):
        out, code = run_watch(["v1.0.0", "--timeout", "3", "--interval", "0",
                               "--grace", "600"], runs=[RUNNING])
        self.assertEqual(code, 3)
        self.assertIn("status=in_progress", out)


class TestRepoSlug(unittest.TestCase):
    def test_parses_ssh_and_https(self):
        for url in ("git@github.com:octo-org/octo-repo.git",
                    "https://github.com/octo-org/octo-repo.git",
                    "https://github.com/octo-org/octo-repo"):
            with mock.patch.object(relwatch, "run", return_value=url + "\n"):
                self.assertEqual(relwatch.repo_slug(), "octo-org/octo-repo")

    def test_explicit_wins(self):
        self.assertEqual(relwatch.repo_slug("a/b"), "a/b")

    def test_unparseable_raises(self):
        with mock.patch.object(relwatch, "run", return_value="weird\n"):
            with self.assertRaises(ReleaseError):
                relwatch.repo_slug()


if __name__ == "__main__":
    unittest.main(verbosity=2)
