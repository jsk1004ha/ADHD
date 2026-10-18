from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from adhd.immutable_policy import authorize, load_policy


class ImmutablePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        (self.workspace / ".adhd").mkdir()
        self.path = self.workspace / ".adhd" / "immutable-policy.json"
        self.policy = {"schema_version": 1, "policy_revision": "r1",
                       "allowed_privacy": ["private"], "allowed_actions": ["read"]}

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_explicit_current_policy_is_authority_and_request_only_narrows(self):
        self.write(self.policy)
        self.assertEqual(load_policy(self.workspace), self.policy)
        authorize(self.workspace, action="read", expected_revision="r1", privacy="private",
                  requested_privacy=["private"])
        for changes in ({"action": "capture"}, {"expected_revision": "r2"},
                        {"privacy": "sensitive"},
                        {"requested_privacy": ["private", "sensitive"]},
                        {"requested_privacy": []}, {"requested_privacy": "private"}):
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                authorize(self.workspace, **{ "action": "read", "expected_revision": "r1",
                          "privacy": "private", "requested_privacy": ["private"], **changes})

    def test_absent_unknown_malformed_and_oversized_policies_deny(self):
        with self.assertRaises(PermissionError):
            load_policy(self.workspace)
        for policy in ({**self.policy, "unknown": True}, {**self.policy, "schema_version": True},
                       {**self.policy, "allowed_actions": ["shell"]},
                       {**self.policy, "allowed_privacy": ["private", "private"]},
                       {**self.policy, "policy_revision": ""},
                       {**self.policy, "policy_revision": "x" * 129},
                       {**self.policy, "allowed_privacy": [None]}, []):
            with self.subTest(policy=policy):
                self.write(policy)
                with self.assertRaises(PermissionError):
                    load_policy(self.workspace)
        for raw in ('{"schema_version":1,"schema_version":1}', " " * 8193, "{broken"):
            self.path.write_text(raw, encoding="utf-8")
            with self.assertRaises(PermissionError):
                load_policy(self.workspace)

    def test_policy_symlink_is_rejected(self):
        target = self.workspace / "policy.json"
        target.write_text(json.dumps(self.policy), encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("symlink unavailable on this host")
        with self.assertRaises(PermissionError):
            load_policy(self.workspace)
