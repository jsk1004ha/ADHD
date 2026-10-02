from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from adhd import dependencies as _dependencies
import tomlkit

from adhd.builtin import (apply_builtin, builtin_status, enable_builtin, mcp_catalog,
                          prepare_mcp_config, rollback_builtin, skill_manifest)
from adhd.core import discover_skills
from adhd.install import install as legacy_install
from adhd.native_install import install_native, rollback_native


class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.codex = self.base / 'codex'
        self.codex.mkdir()
        self.agents = self.base / 'agents'
        self.original = b'# keep original comment\nmodel = "user-model"\n\n[mcp_servers.private]\nurl = "https://private.example/mcp"\n'
        (self.codex / 'config.toml').write_bytes(self.original)

    def _config(self):
        return tomlkit.parse((self.codex / 'config.toml').read_text(encoding='utf-8'))

    def test_bundle_has_50_real_reviewed_trees_and_16_connection_definitions(self):
        manifest = skill_manifest()
        self.assertEqual(len(manifest['skills']), 50)
        science_source = 'https://github.com/K-Dense-AI/scientific-agent-skills'
        self.assertIn(science_source, [source['repo'] for source in manifest['sources']])
        science_rows = [row for row in manifest['skills'] if row['repo'].startswith('K-Dense-AI/')]
        self.assertEqual(len(science_rows), 20)
        self.assertEqual({row['repo'] for row in science_rows},
                         {'K-Dense-AI/scientific-agent-skills'})
        self.assertTrue(all(row['files'] and row['commit'] and row['license_file']
                            for row in manifest['skills']))
        rows = mcp_catalog()
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(row['builtin'].get('command') or row['builtin'].get('url') for row in rows))
        self.assertTrue(all('@latest' not in json.dumps(row['builtin']) for row in rows))

    def test_mcp_registration_preserves_name_and_url_aliases(self):
        existing = (self.original + b'\n[mcp_servers.existing_docs]\n'
                    b'url = "https://developers.openai.com/mcp"\n'
                    b'\n[mcp_servers.exa]\nurl = "https://custom.example/mcp"\n')
        output, entries, statuses = prepare_mcp_config(existing, environment={})
        document = tomlkit.parse(output.decode())
        self.assertEqual(document['mcp_servers']['exa']['url'], 'https://custom.example/mcp')
        self.assertNotIn('openai-docs', document['mcp_servers'])
        self.assertEqual(len(entries), 14)
        self.assertEqual(next(row for row in statuses if row['key'] == 'aside')['registration'], 'added')
        self.assertEqual(next(row for row in statuses if row['key'] == 'openai-docs')['registration'], 'preserved_url_alias')
        self.assertFalse(document['mcp_servers']['tavily']['enabled'])
        self.assertTrue(document['mcp_servers']['firecrawl']['enabled'])
        (self.codex / 'config.toml').write_bytes(existing)
        docs = next(row for row in builtin_status(self.codex)['mcp'] if row['key'] == 'openai-docs')
        self.assertTrue(docs['registered'])
        self.assertEqual(docs['existing_key'], 'existing_docs')

    def test_standalone_apply_preserves_collision_and_unrelated_config_edits_on_rollback(self):
        collision = self.codex / 'skills' / 'pdf' / 'SKILL.md'
        collision.parent.mkdir(parents=True)
        collision.write_text('user-owned pdf skill', encoding='utf-8')
        with patch.dict(os.environ, {'CODEX_HOME': str(self.codex)}):
            record = apply_builtin(self.codex)
            self.assertEqual(record['catalog_count'], 16)
            self.assertEqual(len(record['mcp']), 16)
            self.assertEqual(sum(row['status'] == 'added' for row in record['skills']), 49)
            fallback = next(row['fallback'] for row in record['skills'] if row['name'] == 'pdf')
            self.assertTrue(Path(fallback).is_file())
            self.assertEqual(collision.read_text(encoding='utf-8'), 'user-owned pdf skill')
            routed = [row for row in discover_skills(self.base)
                      if row['origin'] == 'adhd-bundled-fallback' and row['name'] == 'pdf']
            self.assertEqual(len(routed), 1)
            self.assertEqual(routed[0]['path'], fallback)
            self.assertEqual(apply_builtin(self.codex)['status'], 'already_installed')
            (self.codex / 'config.toml').write_text(
                (self.codex / 'config.toml').read_text(encoding='utf-8')
                + '\n[plugins."user@plugin"]\nenabled = true\n', encoding='utf-8')
            result = rollback_builtin(self.codex)
            self.assertGreater(result['restored_files'], 49)
            config = self._config()
            self.assertEqual(config['model'], 'user-model')
            self.assertEqual(config['mcp_servers']['private']['url'], 'https://private.example/mcp')
            self.assertEqual(config['plugins']['user@plugin']['enabled'], True)
            self.assertNotIn('openai-docs', config['mcp_servers'])
            self.assertEqual(collision.read_text(encoding='utf-8'), 'user-owned pdf skill')
            self.assertFalse((self.codex / 'skills' / 'astropy' / 'SKILL.md').exists())

    def test_managed_mcp_edit_blocks_checked_rollback(self):
        apply_builtin(self.codex)
        document = self._config()
        document['mcp_servers']['openai-docs']['url'] = 'https://user.example/mcp'
        (self.codex / 'config.toml').write_text(tomlkit.dumps(document), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Later edits detected in ADHD MCP entry'):
            rollback_builtin(self.codex)
        self.assertTrue((self.codex / 'skills' / 'astropy' / 'SKILL.md').is_file())

    def test_failed_standalone_install_restores_added_files(self):
        original_replace = os.replace

        def fail_config(source, destination):
            if Path(destination) == self.codex / 'config.toml':
                raise OSError('injected config write failure')
            return original_replace(source, destination)

        with patch('adhd.builtin.os.replace', side_effect=fail_config):
            with self.assertRaisesRegex(OSError, 'injected'):
                apply_builtin(self.codex)
        self.assertEqual((self.codex / 'config.toml').read_bytes(), self.original)
        self.assertFalse((self.codex / 'skills' / 'astropy' / 'SKILL.md').exists())
        self.assertFalse((self.codex / 'adhd' / 'builtin-installation.json').exists())

    def test_explicit_enable_updates_checked_selector(self):
        with patch('adhd.builtin._program_available', return_value=False):
            apply_builtin(self.codex)
        self.assertFalse(self._config()['mcp_servers']['arxiv']['enabled'])
        with patch('adhd.builtin._program_available', return_value=True):
            self.assertEqual(enable_builtin(self.codex, 'arxiv')['status'], 'enabled')
        self.assertTrue(self._config()['mcp_servers']['arxiv']['enabled'])
        rollback_builtin(self.codex)
        self.assertEqual((self.codex / 'config.toml').read_bytes(), self.original)
        again = apply_builtin(self.codex)
        self.assertEqual(sum(row['status'] == 'added' for row in again['skills']), 50)
        self.assertTrue((self.codex / 'skills' / 'astropy' / 'SKILL.md').is_file())
        rollback_builtin(self.codex)

    def test_native_default_install_and_rollback_include_builtins(self):
        (self.codex / 'AGENTS.md').write_text('user guidance\n', encoding='utf-8')
        (self.codex / 'hooks.json').write_text('{"hooks":{}}', encoding='utf-8')
        record = install_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(len(record['builtin_skills']), 50)
        self.assertEqual(len(record['builtin_mcp']), 16)
        self.assertTrue((self.agents / 'skills' / 'astropy' / 'SKILL.md').is_file())
        self.assertTrue((Path(record['release']) / 'bundled' / 'skills' / 'astropy' / 'SKILL.md').is_file())
        self.assertIn('openai-docs', self._config()['mcp_servers'])
        rollback_native(self.codex)
        self.assertFalse((self.agents / 'skills' / 'astropy' / 'SKILL.md').exists())
        self.assertEqual((self.codex / 'config.toml').read_bytes(), self.original)

    def test_runtime_cache_does_not_invalidate_snapshot_manifest(self):
        record = apply_builtin(self.codex)
        root = Path(record['release'])
        cache = root / 'bundled' / 'skills' / 'astropy' / 'scripts' / '__pycache__' / 'demo.pyc'
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b'cache')
        self.assertEqual(len(skill_manifest(root)['skills']), 50)
        self.assertEqual(builtin_status(self.codex)['skills_total'], 50)

    def test_recipe_change_creates_new_immutable_standalone_release(self):
        first = apply_builtin(self.codex)
        rollback_builtin(self.codex)
        changed_source = self.base / 'changed-source'
        shutil.copytree(first['release'], changed_source)
        catalog_path = changed_source / 'config' / 'mcp-selection.json'
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        catalog['recipe_checked_date'] = 'fixture-revision'
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
        second = apply_builtin(self.codex, source_root=changed_source)
        self.assertNotEqual(first['release'], second['release'])
        self.assertTrue(Path(first['release']).is_dir())
        self.assertTrue(Path(second['release']).is_dir())
        rollback_builtin(self.codex)

    def test_native_model_migration_preserves_mcp_additions_and_restores_role(self):
        role = self.codex / 'agents' / 'custom.toml'
        role.parent.mkdir(parents=True)
        original_role = b'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n'
        role.write_bytes(original_role)
        (self.codex / 'AGENTS.md').write_text('user guidance\n', encoding='utf-8')
        (self.codex / 'hooks.json').write_text('{"hooks":{}}', encoding='utf-8')
        record = install_native(self.codex, self.agents, migrate_models=True, fixture_mode=True)
        self.assertTrue(record['migrated_roles'])
        self.assertIn('openai-docs', self._config()['mcp_servers'])
        rollback_native(self.codex)
        self.assertEqual(role.read_bytes(), original_role)
        self.assertEqual((self.codex / 'config.toml').read_bytes(), self.original)

    def test_legacy_copy_excludes_private_runtime_state_but_keeps_hidden_assets(self):
        record = legacy_install(self.codex)
        runner = Path(record['runner'])
        current = runner.parent
        self.assertFalse((current / '.adhd').exists())
        self.assertFalse((current / '.code-review-graph').exists())
        self.assertTrue((current / 'bundled' / 'skills' / 'astropy' / 'SKILL.md').is_file())
        from adhd.install import _legacy_copy_ignore
        self.assertNotIn('.env.example', _legacy_copy_ignore(
            str(current / 'bundled' / 'skills' / 'astropy'), ['.env.example']))


if __name__ == '__main__':
    unittest.main()
