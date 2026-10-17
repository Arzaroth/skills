#!/usr/bin/env python3
"""The property that matters: the restore point survives a crash between
recording it and flipping the setting, and a re-run cannot overwrite it."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


def load(state_path):
    os.environ["MERGE_STACK_STATE"] = str(state_path)
    spec = importlib.util.spec_from_file_location(
        "squashctl", Path(__file__).with_name("squashctl.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeForge:
    def __init__(self, squash="always", method="rebase_merge"):
        self.project = {"squash_option": squash, "merge_method": method,
                        "default_branch": "master"}
        self.puts = []
        self.fail_put = False

    def __call__(self, args, host):
        if args and args[0] == "--method":
            if self.fail_put:
                raise SystemExit("glab api failed: 403")
            self.puts.append(args[2])
            self.project["squash_option"] = args[2].split("squash_option=")[1]
            return "{}"
        return json.dumps(self.project)


class SquashCtl(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"
        self.m = load(self.tmp)
        self.forge = FakeForge()
        self.m._glab = self.forge

    def state(self):
        return json.loads(self.tmp.read_text()) if self.tmp.exists() else {}

    def test_records_restore_point_before_changing(self):
        self.m.set_value("g/p", None, "never")
        self.assertEqual(self.state()["projects"]["g/p"]["original_squash_option"], "always")
        self.assertEqual(self.forge.project["squash_option"], "never")

    def test_restore_returns_original_and_clears(self):
        self.m.set_value("g/p", None, "never")
        rc = self.m.restore("g/p")
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.project["squash_option"], "always")
        self.assertEqual(self.state()["projects"], {})

    def test_second_set_keeps_the_true_original(self):
        self.m.set_value("g/p", None, "never")
        self.m.set_value("g/p", None, "never")
        self.assertEqual(self.state()["projects"]["g/p"]["original_squash_option"], "always")

    def test_restore_with_nothing_pending_is_a_noop(self):
        self.assertEqual(self.m.restore(None), 0)

    def test_failed_restore_keeps_the_state_and_reports(self):
        self.m.set_value("g/p", None, "never")
        self.forge.fail_put = True
        rc = self.m.restore("g/p")
        self.assertEqual(rc, 1)
        self.assertIn("g/p", self.state()["projects"])

    def test_pending_is_nonzero_while_a_change_is_outstanding(self):
        self.assertEqual(self.m.pending(), 0)
        self.m.set_value("g/p", None, "never")
        self.assertEqual(self.m.pending(), 1)

    def test_rejects_a_value_the_forge_does_not_accept(self):
        with self.assertRaises(SystemExit):
            self.m.set_value("g/p", None, "off")


if __name__ == "__main__":
    unittest.main()
