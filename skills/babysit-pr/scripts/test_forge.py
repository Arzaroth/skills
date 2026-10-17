"""Unit tests for forge.py. The gh/glab boundary is mocked: these pin the shaping
and normalisation, which is where the forge differences actually bite.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import forge
from forge import (AddressError, ForgeError, GitHubForge, GitLabForge,
                   PaidActionRefused, json_stream, parse_remote, parse_target,
                   post_guard)


class TestParseRemote(unittest.TestCase):
    def test_forms(self):
        cases = [
            ("git@github.com:octo-org/octo-repo.git", ("github.com", "octo-org/octo-repo")),
            ("git@git.example.com:Group/Sub/repo.git", ("git.example.com", "Group/Sub/repo")),
            ("https://github.com/o/r.git", ("github.com", "o/r")),
            ("https://user@git.example.com/g/r", ("git.example.com", "g/r")),
            ("ssh://git@git.example.com:2222/g/r.git", ("git.example.com", "g/r")),
            ("http://host/o/r", ("host", "o/r")),
        ]
        for url, want in cases:
            self.assertEqual(parse_remote(url), want, url)

    def test_rejects_garbage(self):
        for bad in ("", "not-a-url", "file:///tmp/x"):
            with self.assertRaises(ForgeError):
                parse_remote(bad)

    def test_nested_group_path_survives(self):
        """GitLab subgroups: the path must keep every segment."""
        _, path = parse_remote("git@gl.example.com:a/b/c/d.git")
        self.assertEqual(path, "a/b/c/d")


class TestAddressing(unittest.TestCase):
    """A PR number means nothing without a repo. Resolving one against the
    working directory reported a merge for PRs in unrelated repos."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BABYSIT_REPO": "", "BABYSIT_FORGE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_full_slug_needs_no_working_directory(self):
        with mock.patch.object(forge, "ambient_target",
                               side_effect=AssertionError("must not be consulted")):
            self.assertEqual(parse_target("git.example.com/Group/Sub/repo"),
                             ("git.example.com", "Group/Sub/repo"))

    def test_url_form(self):
        self.assertEqual(parse_target("https://github.com/o/r.git"), ("github.com", "o/r"))
        self.assertEqual(parse_target("git@gl.example.com:g/r.git"), ("gl.example.com", "g/r"))

    def test_bare_path_borrows_the_ambient_host_only(self):
        with mock.patch.object(forge, "ambient_target",
                               return_value=("github.com", "somewhere/else")):
            self.assertEqual(parse_target("o/r"), ("github.com", "o/r"))

    def test_bare_path_outside_a_repo_says_what_to_pass(self):
        with mock.patch.object(forge, "ambient_target", return_value=(None, None)):
            with self.assertRaises(AddressError) as ctx:
                parse_target("o/r")
        self.assertIn("host/owner/name", str(ctx.exception))

    def test_no_repo_and_no_remote_is_an_address_error_not_a_forge_error(self):
        """The caller distinguishes them by exit code: 64 is usage, 1 is the
        forge saying no."""
        with mock.patch.object(forge, "ambient_target", return_value=(None, None)):
            with self.assertRaises(AddressError):
                forge.detect()

    def test_a_single_segment_is_not_an_address(self):
        with self.assertRaises(AddressError):
            parse_target("repo")

    def test_explicit_repo_beats_the_working_directory(self):
        with mock.patch.object(forge, "ambient_target", return_value=("github.com", "a/b")), \
             mock.patch.object(forge, "detect_kind", return_value="github"):
            self.assertEqual(forge.detect("c/d").path, "c/d")
            self.assertEqual(forge.detect().path, "a/b")

    def test_slug_round_trips_as_an_address(self):
        """`prctl.py detect` prints slug=; it must be passable back as --repo."""
        with mock.patch.object(forge, "detect_kind", return_value="gitlab"):
            f = GitLabForge("git.example.com", "Group/Sub/repo")
            self.assertEqual(parse_target(f.slug), (f.host, f.path))

    def test_forge_kind_cache_is_per_host(self):
        """Standing in a GitHub checkout while addressing a GitLab repo must not
        reuse the local cache, or every call hits the wrong API."""
        seen = []

        def fake_git(*args, default=""):
            seen.append(args)
            return ""

        with mock.patch.object(forge, "git", fake_git), \
             mock.patch.object(forge, "_probe", return_value=401):
            forge.detect_kind("gl.example.com")
        self.assertTrue(any("babysit.forge.gl-example-com" in a for args in seen for a in args),
                        f"cache key must name the host: {seen}")


class TestJsonStream(unittest.TestCase):
    def test_concatenated_pages(self):
        self.assertEqual(json_stream('[{"a":1}] [{"a":2}]'), [{"a": 1}, {"a": 2}])

    def test_single_object(self):
        self.assertEqual(json_stream('{"a":1}'), [{"a": 1}])

    def test_empty(self):
        self.assertEqual(json_stream("   "), [])


GH_PR = {
    "number": 39, "url": "https://github.com/o/r/pull/39", "state": "OPEN",
    "isDraft": False, "merged": False, "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN", "headRefName": "feat/x", "baseRefName": "main",
    "headRefOid": "abc123",
    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    "reviewThreads": {"nodes": [
        {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py",
         "line": 10, "originalLine": 9, "comments": {"nodes": [
             {"author": {"login": "coderabbitai[bot]"}, "body": "bad",
              "url": "u", "createdAt": "t"},
             {"author": {"login": "arza"}, "body": "no", "url": "u2", "createdAt": "t2"}]}},
        {"id": "T2", "isResolved": True, "isOutdated": True, "path": "b.py",
         "line": None, "originalLine": 4, "comments": {"nodes": [
             {"author": {"login": "human"}, "body": "ok", "url": "u", "createdAt": "t"}]}},
    ]},
}


class TestGitHubShaping(unittest.TestCase):
    def setUp(self):
        self.f = GitHubForge("github.com", "o/r")

    def _with_pr(self, pr=None):
        return mock.patch.object(self.f, "_pr", return_value=pr or GH_PR)

    def test_state_normalises_mergeable(self):
        """Regression: GitHub says CONFLICTING where GitLab says conflict, so an
        un-normalised value made the watcher's conflict check unreachable."""
        with self._with_pr():
            self.assertEqual(self.f.state(39)["mergeable"], "true")
        conflicted = {**GH_PR, "mergeable": "CONFLICTING"}
        with self._with_pr(conflicted):
            self.assertEqual(self.f.state(39)["mergeable"], "conflict")
        unknown = {**GH_PR, "mergeable": "UNKNOWN"}
        with self._with_pr(unknown):
            self.assertEqual(self.f.state(39)["mergeable"], "unknown")

    def test_state_fields(self):
        with self._with_pr():
            s = self.f.state(39)
        self.assertEqual(s["state"], "open")
        self.assertEqual(s["ci"], "success")
        self.assertEqual(s["head"], "abc123")
        self.assertEqual((s["threads_total"], s["unresolved"]), (2, 1))

    def test_merged_state_wins_over_open(self):
        with self._with_pr({**GH_PR, "merged": True}):
            self.assertEqual(self.f.state(39)["state"], "merged")

    def test_no_ci_is_none_not_crash(self):
        with self._with_pr({**GH_PR, "commits": {"nodes": []}}):
            self.assertEqual(self.f.state(39)["ci"], "none")

    def test_threads_shape_and_filter(self):
        with self._with_pr():
            allt = self.f.threads(39)
            unres = self.f.threads(39, unresolved_only=True)
        self.assertEqual(len(allt), 2)
        self.assertEqual([t["id"] for t in unres], ["T1"])
        t = allt[0]
        self.assertEqual(t["source"], "bot")
        self.assertEqual(t["replies"], 1)
        self.assertEqual(t["line"], 10)
        self.assertEqual(allt[1]["source"], "human")
        self.assertEqual(allt[1]["line"], 4, "falls back to originalLine when line is null")

    def test_reviews_sorted_oldest_first(self):
        pages = {
            "reviews": [{"id": 2, "user": {"login": "b"}, "body": "second",
                         "submitted_at": "2026-01-02T00:00:00Z"}],
            "comments": [{"id": 1, "user": {"login": "a"}, "body": "first",
                          "created_at": "2026-01-01T00:00:00Z"},
                         {"id": 3, "user": {"login": "c"}, "body": "   "}],
        }
        def fake(ep, **kw):
            return pages["reviews"] if "/reviews" in ep else pages["comments"]
        with mock.patch.object(self.f, "_api", side_effect=fake):
            got = self.f.reviews(39)
        self.assertEqual([c["body"] for c in got], ["first", "second"])
        self.assertEqual(len(got), 2, "blank bodies dropped")

    def test_merge_raises_when_not_merged(self):
        with mock.patch.object(self.f, "state", return_value={"head_branch": "b"}), \
             mock.patch.object(self.f, "_api", return_value={"merged": False,
                                                            "message": "blocked"}):
            with self.assertRaises(ForgeError) as ctx:
                self.f.merge(39)
        self.assertIn("blocked", str(ctx.exception))

    def test_merge_deletes_branch_and_survives_delete_failure(self):
        calls = []
        def fake(ep, **kw):
            calls.append((ep, kw.get("method")))
            if ep.endswith("/merge"):
                return {"merged": True, "sha": "d" * 40}
            raise ForgeError("branch already gone")
        with mock.patch.object(self.f, "state", return_value={"head_branch": "b"}), \
             mock.patch.object(self.f, "_api", side_effect=fake):
            self.assertEqual(self.f.merge(39), "d" * 9)
        self.assertIn(("repos/o/r/git/refs/heads/b", "DELETE"), calls)

    def test_create_returns_number_in_one_call(self):
        with mock.patch.object(self.f, "_api", return_value={"number": 7}) as api:
            self.assertEqual(self.f.create("h", "b", "t", "body"), 7)
        self.assertEqual(api.call_count, 1, "create must not need a follow-up lookup")


GL_MR = {
    "iid": 691, "web_url": "https://gl/x/-/merge_requests/691", "sha": "def456",
    "source_branch": "feat/y", "target_branch": "master", "state": "opened",
    "draft": False, "has_conflicts": False, "detailed_merge_status": "mergeable",
    "head_pipeline": {"status": "success"}, "blocking_discussions_resolved": True,
    "changes_count": "3", "merge_error": None, "rebase_in_progress": False,
}
GL_DISCUSSIONS = [
    {"id": "D1", "notes": [
        {"system": False, "resolvable": True, "resolved": False,
         "author": {"username": "coderabbitai"}, "body": "bad",
         "position": {"new_path": "a.py", "new_line": 10}}]},
    {"id": "D2", "notes": [
        {"system": False, "resolvable": True, "resolved": True,
         "author": {"username": "allan"}, "body": "ok",
         "position": {"new_path": "b.py", "new_line": 2}}]},
    {"id": "D3", "notes": [{"system": True, "body": "changed the milestone"}]},
]


class TestGitLabShaping(unittest.TestCase):
    def setUp(self):
        self.f = GitLabForge("gl.example.com", "Group/repo")

    def test_project_path_is_url_encoded(self):
        self.assertEqual(self.f.proj, "projects/Group%2Frepo")

    def test_state_normalises_opened_to_open(self):
        with mock.patch.object(self.f, "_api", return_value=GL_MR), \
             mock.patch.object(self.f, "_discussions", return_value=GL_DISCUSSIONS):
            s = self.f.state(691)
        self.assertEqual(s["state"], "open", "must match github's vocabulary")
        self.assertEqual(s["mergeable"], "true")
        self.assertEqual((s["threads_total"], s["unresolved"]), (2, 1))

    def test_conflicts_reported_as_conflict(self):
        with mock.patch.object(self.f, "_api", return_value={**GL_MR, "has_conflicts": True}), \
             mock.patch.object(self.f, "_discussions", return_value=[]):
            self.assertEqual(self.f.state(691)["mergeable"], "conflict")

    def test_threads_skip_system_notes(self):
        with mock.patch.object(self.f, "_discussions", return_value=GL_DISCUSSIONS):
            got = self.f.threads(691)
        self.assertEqual([t["id"] for t in got], ["D1", "D2"])
        self.assertEqual(got[0]["source"], "bot")
        self.assertEqual(got[1]["source"], "human")

    def test_reviews_drop_system_notes(self):
        notes = [{"id": 1, "system": True, "author": {"username": "x"}, "body": "assigned"},
                 {"id": 2, "system": False, "author": {"username": "y"}, "body": "real",
                  "created_at": "t"}]
        with mock.patch.object(self.f, "_api", return_value=notes):
            got = self.f.reviews(691)
        self.assertEqual([c["id"] for c in got], ["2"])

    def test_merge_raises_when_state_is_not_merged(self):
        with mock.patch.object(self.f, "_api", return_value={"state": "opened",
                                                             "merge_error": "CI failed"}):
            with self.assertRaises(ForgeError) as ctx:
                self.f.merge(691)
        self.assertIn("CI failed", str(ctx.exception))

    def test_review_comment_requires_diff_refs(self):
        with mock.patch.object(self.f, "_api", return_value={"diff_refs": {}}):
            with self.assertRaises(ForgeError) as ctx:
                self.f.review_comment(691, "a.py", 3, "x")
        self.assertIn("refresh-diff", str(ctx.exception))


class TestPostGuard(unittest.TestCase):
    """A rate-limited CodeRabbit says: "Run an on-demand review: Comment
    @coderabbitai review --use-credits." That bills. The refusal lives at the
    write boundary so no caller can post it by reading the suggestion."""

    PAID = ["@coderabbitai review --use-credits",
            "@coderabbitai review --credits",
            "please run it with --paid"]

    def test_paid_text_is_refused(self):
        for body in self.PAID:
            with self.assertRaises(PaidActionRefused, msg=body):
                post_guard(body)

    def test_refusal_says_what_to_do(self):
        with self.assertRaises(PaidActionRefused) as ctx:
            post_guard("@coderabbitai review --use-credits")
        msg = str(ctx.exception)
        self.assertIn("costs money", msg)
        self.assertIn("Ask the user", msg)

    def test_explicit_authorisation_lets_it_through(self):
        body = "@coderabbitai review --use-credits"
        self.assertEqual(post_guard(body, allow_paid=True), body)

    def test_free_text_passes(self):
        for body in ("@coderabbitai review", "@coderabbitai full review",
                     "Fixed in abc123.", ""):
            self.assertEqual(post_guard(body), body)

    def test_prose_about_credits_is_not_blocked(self):
        """Only flag-shaped tokens bill. A reply discussing quota must still post,
        or the guard silently eats ordinary review conversation."""
        for body in ("We are out of credits this hour, so I will wait.",
                     "CodeRabbit offers a paid on-demand review; your call.",
                     "This uses credits from the team plan."):
            self.assertEqual(post_guard(body), body)

    def test_every_write_path_guards(self):
        """Both forges, every method that posts text. A guard on one path is a
        guard that gets routed around."""
        paid = "@coderabbitai review --use-credits"
        for f in (GitHubForge("github.com", "o/r"), GitLabForge("gl.example.com", "g/r")):
            # _graphql exists only on GitHub; create=True keeps one loop for both.
            with mock.patch.object(f, "_api"), \
                 mock.patch.object(f, "_graphql", create=True), \
                 mock.patch.object(f, "state", return_value={"head": "abc"}):
                for name, call in (("comment", lambda: f.comment(1, paid)),
                                   ("reply", lambda: f.reply(1, "T1", paid)),
                                   ("review_comment",
                                    lambda: f.review_comment(1, "a.py", 2, paid))):
                    with self.assertRaises(PaidActionRefused,
                                           msg=f"{f.kind}.{name} posted paid text"):
                        call()


class TestDescriptionPreservesReviewerSections(unittest.TestCase):
    """Regression: an agent rewrote an MR's description and wiped CodeRabbit's
    walkthrough. The bot only rewrites that block on its next review, so a poll
    loop waiting for it to reappear hung for hours on something that could not
    happen."""

    SUMMARY = ("## Summary by CodeRabbit\n\n- **New Features**\n  - adds a gauge\n")

    def _forge(self, current):
        f = GitHubForge("github.com", "o/r")
        written = {}
        def api(ep, **kw):
            if kw.get("method") == "PATCH":
                written["body"] = kw["body"]["body"]
                return {}
            return {"body": current}
        return f, written, mock.patch.object(f, "_api", side_effect=api)

    def test_reviewer_section_is_carried_across(self):
        f, written, patched = self._forge("My description\n\n" + self.SUMMARY)
        with patched:
            kept = f.set_body(1, "A rewritten description")
        self.assertEqual(kept, 1)
        self.assertIn("Summary by CodeRabbit", written["body"])
        self.assertIn("A rewritten description", written["body"])

    def test_not_duplicated_when_the_new_body_already_has_it(self):
        f, written, patched = self._forge("old\n\n" + self.SUMMARY)
        with patched:
            kept = f.set_body(1, "new\n\n" + self.SUMMARY)
        self.assertEqual(kept, 0)
        self.assertEqual(written["body"].count("Summary by CodeRabbit"), 1)

    def test_html_comment_form_is_recognised(self):
        body = ("intro\n<!-- This is an auto-generated comment: summarize by "
                "coderabbit.ai -->\nwalkthrough\n<!-- end of auto-generated comment: "
                "summarize by coderabbit.ai -->")
        f, written, patched = self._forge(body)
        with patched:
            kept = f.set_body(1, "rewritten")
        self.assertEqual(kept, 1)
        self.assertIn("auto-generated comment", written["body"])

    def test_nothing_to_keep_is_a_plain_write(self):
        f, written, patched = self._forge("just my words")
        with patched:
            kept = f.set_body(1, "new words")
        self.assertEqual(kept, 0)
        self.assertEqual(written["body"], "new words")

    def test_explicit_drop_is_possible_but_must_be_asked_for(self):
        f, written, patched = self._forge("old\n\n" + self.SUMMARY)
        with patched:
            kept = f.set_body(1, "new", keep_bot_sections=False)
        self.assertEqual(kept, 0)
        self.assertNotIn("CodeRabbit", written["body"])


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".creds", delete=False)
        self.tmp.write("# comment\n\nalt1 gl.example.com tok1\n"
                       "alt2 gl.example.com tok2\nany * tok3\nother github.com tok4\n")
        self.tmp.close()
        os.chmod(self.tmp.name, 0o600)
        self.patch = mock.patch.object(forge, "CREDS_PATH", self.tmp.name)
        self.patch.start()
        self.state = tempfile.TemporaryDirectory()
        self.statepatch = mock.patch.object(forge, "STATE_DIR",
                                            pathlib.Path(self.state.name))
        self.statepatch.start()

    def tearDown(self):
        self.patch.stop()
        self.statepatch.stop()
        self.state.cleanup()
        os.unlink(self.tmp.name)

    def test_host_match_and_wildcard(self):
        self.assertEqual([n for n, _ in forge.load_creds("gl.example.com")],
                         ["alt1", "alt2", "any"])

    def test_unknown_credential_raises(self):
        f = GitLabForge("gl.example.com", "g/r")
        with self.assertRaises(ForgeError):
            f.use_credential("nope")

    def test_use_credential_sets_env(self):
        f = GitLabForge("gl.example.com", "g/r")
        f.use_credential("alt2")
        self.assertEqual(f.env["GITLAB_TOKEN"], "tok2")
        self.assertEqual(f.env["GITLAB_HOST"], "gl.example.com")

    def test_rotation_visits_default_slot_then_each_credential(self):
        f = GitLabForge("gl.example.com", "g/r")
        seen = [f.rotate_credential() for _ in range(4)]
        self.assertEqual(seen, ["alt1", "alt2", "any", "default"],
                         "slot 0 is the user's own auth, so rotation wraps through it")

    def test_the_rotation_pointer_survives_the_process(self):
        self.assertEqual(GitLabForge("gl.example.com", "g/r").rotate_credential(), "alt1")
        self.assertEqual(GitLabForge("gl.example.com", "g/r").rotate_credential(), "alt2")

    def test_going_back_to_default_drops_the_token(self):
        f = GitLabForge("gl.example.com", "g/r")
        f.use_credential("alt1")
        f.use_credential("default")
        self.assertNotIn("GITLAB_TOKEN", f.env)
        self.assertNotIn("GITLAB_HOST", f.env,
                         "a leftover host override sends the default login to the wrong host")
        self.assertEqual(f.identity, "default")

    def test_a_refused_credential_is_parked_and_the_call_retried_as_default(self):
        """Measured: rotation landed on an expired token and every read after it
        failed with 401, blinding the watcher while the MR went mergeable."""
        f = GitLabForge("gl.example.com", "g/r")
        f.use_credential("alt1")
        calls = []

        def fake_run(argv, *, stdin=None, env=None):
            calls.append((env or {}).get("GITLAB_TOKEN"))
            if (env or {}).get("GITLAB_TOKEN") == "tok1":
                raise ForgeError('glab failed (1): glab: HTTP 401 {"error":"invalid_token"}')
            return '{"iid": 7}'

        with mock.patch.object(forge, "run", side_effect=fake_run):
            self.assertEqual(f._api("merge_requests/7"), {"iid": 7})
        self.assertEqual(calls, ["tok1", None])
        self.assertEqual(f.identity, "default")
        self.assertTrue(forge.limited_until("gl.example.com", "alt1"))
        self.assertEqual(f.rotate_credential(), "alt2", "a parked token is never rotated back onto")

    def test_the_default_identity_is_not_retried_on_a_401(self):
        f = GitLabForge("gl.example.com", "g/r")
        with mock.patch.object(forge, "run", side_effect=ForgeError("glab failed (1): HTTP 401")) as run:
            with self.assertRaises(ForgeError):
                f._api("merge_requests/7")
        self.assertEqual(run.call_count, 1)

    def test_other_errors_under_a_credential_are_not_swallowed(self):
        f = GitLabForge("gl.example.com", "g/r")
        f.use_credential("alt1")
        with mock.patch.object(forge, "run", side_effect=ForgeError("glab failed (1): HTTP 404")):
            with self.assertRaises(ForgeError):
                f._api("merge_requests/7")
        self.assertEqual(f.identity, "alt1")
        self.assertFalse(forge.limited_until("gl.example.com", "alt1"))

    def test_rotation_skips_an_account_that_is_out_of_quota(self):
        """Spending a trigger on an account that just said "review limit reached"
        buys nothing, and the slot is gone either way."""
        f = GitLabForge("gl.example.com", "g/r")
        forge.mark_limited("gl.example.com", "alt1")
        self.assertEqual(f.rotate_credential(), "alt2")

    def test_rotation_starts_at_slot_1_when_the_default_is_limited(self):
        f = GitLabForge("gl.example.com", "g/r")
        forge.mark_limited("gl.example.com", "default")
        self.assertEqual([f.rotate_credential() for _ in range(4)],
                         ["alt1", "alt2", "any", "alt1"])

    def test_avoid_limited_steps_off_a_spent_account_before_asking(self):
        f = GitLabForge("gl.example.com", "g/r")
        forge.mark_limited("gl.example.com", "default")
        self.assertEqual(f.avoid_limited(), "alt1")
        self.assertEqual(f.identity, "alt1")
        self.assertEqual(f.env["GITLAB_TOKEN"], "tok1")

    def test_avoid_limited_does_nothing_when_the_current_account_is_fine(self):
        f = GitLabForge("gl.example.com", "g/r")
        self.assertIsNone(f.avoid_limited())
        self.assertEqual(f.identity, "default")

    def test_a_limit_expires_on_its_own_stated_window(self):
        host = "gl.example.com"
        forge.mark_limited(host, "default", until=time.time() - 1)
        self.assertEqual(forge.limited_until(host, "default"), 0)
        self.assertIsNone(GitLabForge(host, "g/r").avoid_limited())

    def test_fresh_credential_is_none_when_every_account_is_spent(self):
        f = GitLabForge("gl.example.com", "g/r")
        for name in ("default", "alt1", "alt2", "any"):
            forge.mark_limited("gl.example.com", name)
        self.assertIsNone(f.fresh_credential())
        self.assertIsNone(f.avoid_limited(), "waiting beats posting into a wall")
        self.assertEqual(f.rotate_credential(), "default",
                         "rotation stays put rather than picking a spent account")

    def test_a_limit_is_per_host(self):
        forge.mark_limited("gl.example.com", "any")
        self.assertEqual(forge.limited_until("github.com", "any"), 0)

    def test_rotation_without_a_file_degrades_to_default(self):
        with mock.patch.object(forge, "CREDS_PATH", "/nonexistent"):
            f = GitHubForge("github.com", "o/r")
            self.assertEqual(f.rotate_credential(), "default")


class TestDetection(unittest.TestCase):
    def test_known_hosts_skip_probing(self):
        with mock.patch.object(forge, "git", return_value=""), \
             mock.patch.object(forge, "_probe",
                               side_effect=AssertionError("must not probe")):
            self.assertEqual(forge.detect_kind("github.com"), "github")
            self.assertEqual(forge.detect_kind("gitlab.com"), "gitlab")

    def test_self_hosted_probes_gitlab_first(self):
        with mock.patch.object(forge, "git", return_value=""), \
             mock.patch.object(forge, "_probe", return_value=401) as probe:
            self.assertEqual(forge.detect_kind("git.example.com"), "gitlab")
        self.assertIn("api/v4/version", probe.call_args[0][0])

    def test_self_hosted_falls_through_to_github_enterprise(self):
        def probe(url):
            return 200 if "api/v3" in url else 0
        with mock.patch.object(forge, "git", return_value=""), \
             mock.patch.object(forge, "_probe", side_effect=probe):
            self.assertEqual(forge.detect_kind("ghe.example.com"), "github")

    def test_undeterminable_host_raises(self):
        with mock.patch.object(forge, "git", return_value=""), \
             mock.patch.object(forge, "_probe", return_value=0):
            with self.assertRaises(ForgeError):
                forge.detect_kind("mystery.example.com")

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"BABYSIT_FORGE": "gitlab"}):
            self.assertEqual(forge.detect_kind("github.com"), "gitlab")

    def test_babysit_repo_retargets_path(self):
        with mock.patch.object(forge, "git", return_value="git@github.com:o/r.git"), \
             mock.patch.dict(os.environ, {"BABYSIT_REPO": "other/repo",
                                          "BABYSIT_FORGE": "github"}):
            self.assertEqual(forge.detect().path, "other/repo")

    def test_no_remote_raises(self):
        with mock.patch.object(forge, "git", return_value=""):
            with self.assertRaises(ForgeError):
                forge.detect()


class TestRun(unittest.TestCase):
    def test_failure_includes_stderr(self):
        with self.assertRaises(ForgeError) as ctx:
            forge.run(["sh", "-c", "echo boom >&2; exit 3"])
        self.assertIn("boom", str(ctx.exception))

    def test_success_returns_stdout(self):
        self.assertEqual(forge.run(["echo", "hi"]).strip(), "hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
