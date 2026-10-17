"""Unit tests for docscheck.py, against real temp git repos.

Most cases here are false positives that made the first version useless: it
reported 273 broken citations and 62 impacted docs on a repo whose real numbers
were 2 and 2. A docs checker nobody runs is worse than none.
"""
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from docscheck import main

KV = re.compile(r'(\w+)=("[^"]*"|\S+)')
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def rows(text):
    return [{k: v.strip('"') for k, v in KV.findall(line)}
            for line in text.strip().splitlines() if line.strip()]


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              env=ENV, capture_output=True, text=True).stdout

    def write(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def commit(self, msg="c"):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def check(self, *argv):
        # Not named `run`: TestCase.run(result) is the test runner itself, and
        # shadowing it makes unittest invoke this instead of running the test.
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["--dir", str(self.root), *argv])
        return rows(buf.getvalue()), code


class TestScope(RepoCase):
    def test_last_commit_when_there_is_no_merge(self):
        self.write("a.py", "x = 1\n")
        self.commit("first")
        self.write("b.py", "y = 2\n")
        self.commit("second")
        out, code = self.check("scope")
        self.assertEqual(code, 0)
        self.assertIn("last commit", out[0]["scope"])
        self.assertEqual([r["path"] for r in out if r.get("kind") == "code"], ["b.py"])

    def test_last_merged_branch_is_preferred_over_last_commit(self):
        self.write("a.py", "x = 1\n")
        self.commit("base")
        trunk = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.git("checkout", "-qb", "feat")
        self.write("feature.py", "f = 1\n")
        self.commit("feat work")
        self.git("checkout", "-q", trunk)
        self.git("merge", "--no-ff", "-q", "feat", "-m", "merge feat")
        out, _ = self.check("scope")
        self.assertIn("last merged branch", out[0]["scope"])
        self.assertIn("feature.py", [r.get("path") for r in out])

    def test_since_ref(self):
        self.write("a.py", "x\n")
        self.commit("one")
        first = self.git("rev-parse", "HEAD").strip()
        self.write("b.py", "y\n")
        self.commit("two")
        out, _ = self.check("scope", "--since", first)
        self.assertIn("since", out[0]["scope"])
        self.assertEqual([r["path"] for r in out if r.get("kind") == "code"], ["b.py"])

    def test_full_lists_everything_tracked(self):
        self.write("a.py", "x\n")
        self.write("docs/guide.md", "# g\n")
        self.commit()
        out, _ = self.check("scope", "--full")
        self.assertEqual(out[0]["scope"], "full")
        self.assertEqual(int(out[0]["changed"]), 2)

    def test_docs_already_touched_are_separated_from_code(self):
        self.write("a.py", "x\n")
        self.write("README.md", "# r\n")
        self.commit()
        out, _ = self.check("scope", "--full")
        kinds = {r.get("path"): r.get("kind") for r in out if "path" in r}
        self.assertEqual(kinds["a.py"], "code")
        self.assertEqual(kinds["README.md"], "doc-already-touched")


class TestMarkers(RepoCase):
    def test_detects_common_spellings(self):
        for i, body in enumerate([
            "Last updated: 2026-01-15\n",
            "<!-- last-updated: abc1234 -->\n",
            "Last reviewed = v1.2.3\n",
            "as of 2026-02-01\n",
        ]):
            self.write(f"d{i}.md", body)
        self.commit()
        out, code = self.check("markers")
        self.assertEqual(code, 0)
        self.assertEqual(int(out[-1]["docs"]), 4)

    def test_no_markers_exits_nonzero_with_a_hint(self):
        self.write("README.md", "# no marker here\n")
        self.commit()
        out, code = self.check("markers")
        self.assertEqual(code, 1)
        self.assertEqual(out[-1]["result"], "none")


class TestImpacted(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("apps/web/package.json", '{"name":"w"}\n')
        self.write("apps/web/src/dms.ts", "export const a = 1\n")
        self.write("brain/features/dms.md", "Lives in `apps/web/src/dms.ts`.\n")
        self.write("brain/architecture/client.md", "The app is under `apps/web/`.\n")
        self.commit("base")

    def test_doc_citing_the_changed_file_is_reported(self):
        self.write("apps/web/src/dms.ts", "export const a = 2\n")
        self.commit("change dms")
        out, code = self.check("impacted")
        self.assertEqual(code, 0)
        docs = [r["doc"] for r in out if "doc" in r]
        self.assertEqual(docs, ["brain/features/dms.md"])

    def test_parent_directory_mentions_do_not_count(self):
        """Regression: matching the parent dir too made `apps/web/` hit 55 of 62
        docs on a monorepo, so every change 'impacted' everything."""
        self.write("apps/web/package.json", '{"name":"w2"}\n')
        self.commit("bump")
        out, _ = self.check("impacted")
        docs = [r["doc"] for r in out if "doc" in r]
        self.assertNotIn("brain/architecture/client.md", docs)

    def test_doc_only_change_reports_no_code(self):
        self.write("brain/features/dms.md", "edited\n")
        self.commit("docs only")
        out, code = self.check("impacted")
        self.assertEqual(code, 1)
        self.assertEqual(out[0]["result"], "no_code_changes")

    def test_no_doc_mentions_the_file(self):
        self.write("apps/web/src/other.ts", "x\n")
        self.commit("new file")
        out, code = self.check("impacted")
        self.assertEqual(code, 1)
        self.assertEqual(out[-1]["result"], "none")


class TestLinks(RepoCase):
    def test_broken_relative_link(self):
        self.write("docs/a.md", "See [b](./b.md) and [c](../README.md).\n")
        self.write("README.md", "# r\n")
        self.commit()
        out, code = self.check("links")
        self.assertEqual(code, 1)
        broken = [r for r in out if r.get("kind") == "link"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["target"], "b.md")

    def test_external_and_anchor_links_are_skipped(self):
        self.write("docs/a.md",
                   "[x](https://e.com) [y](#section) [z](mailto:a@b.c)\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_dead_source_citation_is_reported(self):
        self.write("brain/f.md", "Implemented in `src/gone.ts`.\n")
        self.commit()
        out, code = self.check("links")
        self.assertEqual(code, 1)
        self.assertEqual(out[0]["kind"], "source")

    def test_monorepo_relative_citation_resolves(self):
        """A doc may cite `server/api/x.ts` for a file at `apps/web/server/api/x.ts`."""
        self.write("apps/web/server/api/x.ts", "x\n")
        self.write("brain/f.md", "Handled by `server/api/x.ts`.\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_narrative_docs_skip_source_citations(self):
        """A changelog cites files that were later deleted; a TODO cites files
        not written yet. Neither is rot."""
        for name in ("CHANGELOG.md", "TODO.md", "ROADMAP.md",
                     "i18n/changelogs/fr.md"):
            self.write(name, "Added `src/never-existed.ts`.\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_brace_expansion_and_placeholders_are_not_paths(self):
        self.write("brain/f.md",
                   "Files `app/use{Cabinet,Showcase}.ts`, `app/use<Feature>.ts`, "
                   "`dist/_nuxt/*.js`, `drizzle/NNNN_name.sql`.\n")
        self.commit()
        out, code = self.check("links")
        self.assertEqual(code, 0, f"unexpected findings: {out}")

    def test_gitignored_build_output_is_not_rot(self):
        self.write(".gitignore", ".output/\n")
        self.write("brain/f.md", "Emitted to `.output/public/build.json`.\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_leading_dot_slash_is_normalised(self):
        self.write("db/schema.ts", "x\n")
        self.write("brain/f.md", "Schema in `./db/schema.ts`.\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_prose_with_slashes_is_not_treated_as_a_path(self):
        self.write("brain/f.md", "Use `and/or` semantics, see `a b/c.ts`.\n")
        self.commit()
        _, code = self.check("links")
        self.assertEqual(code, 0)

    def test_single_doc_scope(self):
        self.write("a.md", "[x](nope.md)\n")
        self.write("b.md", "[y](alsonope.md)\n")
        self.commit()
        out, _ = self.check("links", "--docs", "a.md")
        self.assertEqual(len([r for r in out if "doc" in r]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
