"""Unit tests for versiontool.py, against synthetic repos on disk.

The discovery cases encode the rule that matters: the repo's own bumper beats a
hand-rolled search, and a project skill beats everything.
"""
import io
import json
import re
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import versiontool
from versiontool import classify, main


def out_of(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return buf.getvalue(), code


KV_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


def kv_lines(text):
    """Parse the emitted KV, honouring quoted values."""
    return [{k: v.strip('"') for k, v in KV_RE.findall(line)}
            for line in text.strip().splitlines()]


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def discover(self):
        return out_of(["--dir", str(self.root), "discover"])


class TestDiscover(RepoCase):
    def test_empty_repo_reports_none_and_exits_nonzero(self):
        text, code = self.discover()
        self.assertEqual(code, 1)
        self.assertIn("result=none", text)

    def test_project_skill_is_found(self):
        self.write(".claude/skills/release/SKILL.md", "---\nname: release\n---\n")
        text, code = self.discover()
        self.assertEqual(code, 0)
        rows = kv_lines(text)
        self.assertEqual(rows[0]["kind"], "project-skill",
                         "a project skill must be reported first")

    def test_project_skill_outranks_a_task(self):
        self.write(".claude/skills/release/SKILL.md")
        self.write("mise-tasks/release", "#!/usr/bin/env bash\n")
        rows = kv_lines(self.discover()[0])
        self.assertEqual([r["kind"] for r in rows[:2]],
                         ["project-skill", "mise-task"])

    def test_mise_task_file(self):
        self.write("mise-tasks/release", "#!/usr/bin/env bash\n")
        rows = kv_lines(self.discover()[0])
        self.assertEqual(rows[0]["kind"], "mise-task")
        self.assertEqual(rows[0]["name"], "release")
        self.assertEqual(rows[0]["hint"], "mise run release",
                         "a hint with spaces must survive parsing intact")

    def test_mise_toml_tasks(self):
        self.write("mise.toml", "[tasks.build]\nrun = 'x'\n\n[tasks.release]\nrun = 'y'\n")
        rows = kv_lines(self.discover()[0])
        names = [r["name"] for r in rows if r.get("kind") == "mise-task"]
        self.assertEqual(names, ["release"], "only release-ish tasks")

    def test_npm_scripts(self):
        self.write("package.json", json.dumps(
            {"scripts": {"test": "jest", "release": "np", "bump:minor": "x"}}))
        names = {r.get("name") for r in kv_lines(self.discover()[0])}
        self.assertIn("release", names)
        self.assertIn("bump:minor", names)
        self.assertNotIn("test", names)

    def test_malformed_package_json_does_not_crash(self):
        self.write("package.json", "{not json")
        _, code = self.discover()
        self.assertEqual(code, 1)

    def test_make_target(self):
        self.write("Makefile", "build:\n\tcc x\n\nrelease:\n\t./do-release\n")
        rows = [r for r in kv_lines(self.discover()[0]) if r.get("kind") == "make-target"]
        self.assertEqual([r["name"] for r in rows], ["release"])

    def test_make_variable_is_not_a_target(self):
        self.write("Makefile", "VERSION := 1.0\n")
        _, code = self.discover()
        self.assertEqual(code, 1, "':=' assignment must not look like a target")

    def test_just_recipe(self):
        self.write("justfile", "build:\n  cargo build\n\nrelease version:\n  echo {{version}}\n")
        rows = [r for r in kv_lines(self.discover()[0]) if r.get("kind") == "just-recipe"]
        self.assertEqual([r["name"] for r in rows], ["release"])

    def test_scripts_dir(self):
        self.write("scripts/bump-version.sh", "#!/bin/sh\n")
        rows = kv_lines(self.discover()[0])
        self.assertEqual(rows[0]["kind"], "script")

    def test_tool_configs(self):
        self.write("release.toml", "[workspace]\n")
        self.write(".bumpversion.cfg", "[bumpversion]\n")
        names = {r.get("name") for r in kv_lines(self.discover()[0])}
        self.assertIn("cargo-release", names)
        self.assertIn("bumpversion", names)

    def test_unrelated_files_are_not_release_machinery(self):
        self.write("scripts/deploy.sh")
        self.write("Makefile", "all:\n\tcc\n")
        _, code = self.discover()
        self.assertEqual(code, 1)


class TestClassify(unittest.TestCase):
    def test_lockfiles_are_regenerate(self):
        for name in ("Cargo.lock", "package-lock.json", "pnpm-lock.yaml", "uv.lock"):
            kind, hint = classify(f"some/dir/{name}")
            self.assertEqual(kind, "regenerate", name)
            self.assertTrue(hint, "must say how to regenerate it")

    def test_changelogs_are_not_bumps(self):
        for name in ("CHANGELOG.md", "NEWS.md", "HISTORY.md"):
            self.assertEqual(classify(name)[0], "changelog")

    def test_everything_else_is_edit(self):
        for name in ("Cargo.toml", "gnome/x/metadata.json", "packaging/app.wxs"):
            self.assertEqual(classify(name)[0], "edit")


class GitRepoCase(RepoCase):
    def setUp(self):
        super().setUp()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.write("Cargo.toml", 'version = "0.30.4"\n')
        self.write("gnome/metadata.json", '{"version-name": "0.30.4"}\n')
        self.write("Cargo.lock", 'name = "x"\nversion = "0.30.4"\n')
        self.write("CHANGELOG.md", "## [0.30.4] - 2026-01-01\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True, env=env)


class TestLocations(GitRepoCase):
    def test_finds_keys_that_are_not_spelled_version(self):
        """The whole point of a literal search: "version-name" is invisible to a
        search for a `version` key."""
        text, code = out_of(["--dir", str(self.root), "locations", "0.30.4"])
        self.assertEqual(code, 0)
        paths = {r["path"] for r in kv_lines(text) if "path" in r}
        self.assertIn("gnome/metadata.json", paths)

    def test_classifies_each_hit(self):
        text, _ = out_of(["--dir", str(self.root), "locations", "0.30.4"])
        by_path = {r["path"]: r["kind"] for r in kv_lines(text) if "path" in r}
        self.assertEqual(by_path["Cargo.lock"], "regenerate")
        self.assertEqual(by_path["CHANGELOG.md"], "changelog")
        self.assertEqual(by_path["Cargo.toml"], "edit")

    def test_absent_version_reports_none(self):
        text, code = out_of(["--dir", str(self.root), "locations", "9.9.9"])
        self.assertEqual(code, 1)
        self.assertIn("result=none", text)


class TestVerify(GitRepoCase):
    def test_stale_version_fails(self):
        text, code = out_of(["--dir", str(self.root), "verify", "0.30.4"])
        self.assertEqual(code, 1)
        self.assertIn("result=STALE", text)

    def test_changelog_history_is_not_stale(self):
        """Old versions live in the changelog forever; only editable files count."""
        for p in ("Cargo.toml", "gnome/metadata.json", "Cargo.lock"):
            (self.root / p).write_text("version = \"0.31.0\"\n")
        text, code = out_of(["--dir", str(self.root), "verify", "0.30.4"])
        self.assertEqual(code, 0, text)
        self.assertIn("result=clean", text)

    def test_clean_when_version_absent(self):
        _, code = out_of(["--dir", str(self.root), "verify", "1.2.3"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
