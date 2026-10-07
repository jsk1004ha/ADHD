"""Release exports keep the goal and Obsidian runtime usable after extraction."""
from pathlib import Path
import hashlib
import json
import unittest

from adhd.core import ROOT
from adhd.native_install import _release_files
from build.package_adhd import public_files


class ReleaseAssetsTests(unittest.TestCase):
    def test_public_and_installed_release_include_goal_and_wiki_assets(self):
        required = {
            'skills/adhd-goal/SKILL.md', 'skills/adhd-goal/agents/openai.yaml',
            'adhd/host_capabilities.py', 'adhd/obsidian.py', 'adhd/obsidian_index.py',
            'adhd/experience.py', 'adhd/experience_procedures.py', 'adhd/experience_storage.py',
            'adhd/obsidian_assets/__init__.py', 'adhd/obsidian_assets/bridge.py',
            'adhd/obsidian_assets/evidence/evidence_archive.py',
            'adhd/obsidian_assets/evidence/evidence_codec.py',
            'adhd/obsidian_assets/evidence/sources.json',
            'schemas/wiki-packet.json', 'docs/obsidian.md', 'docs/releases/0.1.6.md',
            'examples/obsidian/config.json', 'examples/obsidian/feedback.json',
            'scripts/benchmark_obsidian.py', 'tests/fixtures/obsidian/evaluation.json',
        }
        required.update('adhd/obsidian_assets/templates/' + name + '.md'
                        for name in ('common', 'decision', 'incident', 'success', 'procedure'))
        required.update('adhd/obsidian_assets/views/' + name + '.base'
                        for name in ('ADHD 검토', 'ADHD 절차'))
        for label, files in (('public ZIP', public_files()), ('installed release', _release_files(ROOT))):
            exported = {path.relative_to(ROOT).as_posix() for path in files}
            self.assertFalse(required - exported, (label, sorted(required - exported)))
            self.assertFalse(any(name.startswith(('.adhd/', 'verification/')) for name in exported))

    def test_vendored_archive_backend_matches_declared_source_hashes(self):
        backend = ROOT / 'adhd/obsidian_assets/evidence'
        provenance = json.loads((backend / 'sources.json').read_text(encoding='utf-8'))
        self.assertEqual(set(provenance['files']), {'evidence_archive.py', 'evidence_codec.py'})
        for name, expected in provenance['files'].items():
            self.assertEqual(hashlib.sha256((backend / name).read_bytes()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
