from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_local_install import unmanaged_hooks


class AuditLocalInstallTests(unittest.TestCase):
    def test_new_and_legacy_harness_hooks_are_not_counted_as_user_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user = {'hooks': [{'type': 'command', 'command': 'python C:\\tools\\user-hook.py'}]}
            legacy = {'hooks': [{'type': 'command', 'command':
                                 'python C:\\example\\.codex\\apzn\\releases\\old\\hook.py'}]}
            renamed = {'hooks': [{'type': 'command', 'command':
                                  'python C:\\example\\.codex\\adhd\\releases\\new\\hook.py'}]}
            hooks = {'hooks': {'SessionStart': [user, legacy, renamed]}}
            (root / 'hooks.json').write_text(json.dumps(hooks), encoding='utf-8')
            self.assertEqual(unmanaged_hooks(root)['hooks']['SessionStart'], [user])


if __name__ == '__main__':
    unittest.main()
