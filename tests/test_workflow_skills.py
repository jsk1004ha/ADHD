"""Exercise shipped ADHD helpers through discovery, install, upgrade and packaging."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

from adhd.cli import main as cli_main
from adhd.core import ROOT, discover_skills, skill_search
from adhd.native_install import (WORKFLOW_SKILLS, _copy_release, install_native,
                                 rollback_native, upgrade_native)
from adhd.skill_router import route_skills
from build import package_adhd


def check_local_links(test: unittest.TestCase, path: Path) -> None:
    for reference in re.findall(r'\[[^\]]+\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
        if reference.startswith(('https://', 'http://', '#')):
            continue
        target = (path.parent / reference.split('#', 1)[0]).resolve()
        test.assertTrue(target.is_file(), f'{path}: missing {reference}')


class WorkflowDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.codex = self.base / 'codex'
        self.workspace = self.base / 'workspace'
        self.codex.mkdir()
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.codex),
                                          'ADHD_HOME': str(self.base / 'state')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_source_helpers_are_discoverable_and_explicitly_routable(self):
        entries = discover_skills(self.workspace)
        for name in WORKFLOW_SKILLS:
            with self.subTest(name=name):
                skill = ROOT / 'skills' / name / 'SKILL.md'
                self.assertIn(str(skill.resolve()), [row['path'] for row in entries])
                routed = route_skills('$' + name + ' 이 작업을 처리해 줘', workspace=self.workspace)
                self.assertEqual(routed['engine'], 'local-metadata-fallback')
                self.assertEqual(routed['primary'][0]['name'], name)
                self.assertEqual(routed['primary'][0]['path'], str(skill.resolve()))

    def test_source_fallback_respects_disable_and_host_catalog(self):
        path = ROOT / 'skills' / 'adhd-shape' / 'SKILL.md'
        config = '[[skills.config]]\npath=' + json.dumps(str(path)) + '\nenabled=false\n'
        (self.codex / 'config.toml').write_text(config, encoding='utf-8')
        self.assertNotIn(str(path.resolve()), [row['path'] for row in discover_skills(self.workspace)])
        self.assertEqual(discover_skills(self.workspace, host_catalog=[]), [])

    def test_local_routing_prefers_same_name_user_project_and_configured_skills(self):
        release_skill = ROOT / 'skills' / 'adhd-shape' / 'SKILL.md'
        for origin in ('user', 'project', 'configured'):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory(dir=self.base) as location:
                isolated = Path(location)
                codex = isolated / 'codex'
                workspace = isolated / 'workspace'
                codex.mkdir()
                workspace.mkdir()
                roots = {'user': codex / 'skills',
                         'project': workspace / '.agents' / 'skills',
                         'configured': isolated / 'custom-skills'}
                skill = roots[origin] / 'adhd-shape' / 'SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text('---\nname: adhd-shape\ndescription: user-owned skill\n---\n',
                                 encoding='utf-8')
                if origin == 'configured':
                    (codex / 'config.toml').write_text(
                        '[[skills.config]]\npath=' + json.dumps(str(skill)) + '\n', encoding='utf-8')
                with patch.dict(os.environ, {'CODEX_HOME': str(codex),
                                             'ADHD_HOME': str(isolated / 'state')}), \
                        patch('adhd.core.Path.home', return_value=isolated / 'os-home'):
                    paths = {row['path'] for row in discover_skills(workspace)}
                    self.assertIn(str(skill.resolve()), paths)
                    self.assertIn(str(release_skill.resolve()), paths)
                    routed = route_skills('$adhd-shape', workspace=workspace)
                    self.assertEqual(routed['primary'][0]['path'], str(skill.resolve()))
                    self.assertNotIn(str(release_skill.resolve()),
                                     [row['path'] for row in routed['supporting']])

    def test_skill_search_prefers_same_name_existing_definition(self):
        query = 'adhd-shape Shape a vague idea into a bounded ADHD task'
        for origin in ('user', 'project', 'configured', 'plugin'):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory(dir=self.base) as location:
                isolated = Path(location)
                codex = isolated / 'codex'
                workspace = isolated / 'workspace'
                codex.mkdir()
                workspace.mkdir()
                roots = {'user': codex / 'skills',
                         'project': workspace / '.agents' / 'skills',
                         'configured': isolated / 'custom-skills',
                         'plugin': codex / 'plugins' / 'cache' / 'example' / 'skills'}
                release = isolated / 'release'
                shipped = release / 'skills' / 'adhd-shape' / 'SKILL.md'
                shipped.parent.mkdir(parents=True)
                shutil.copy2(ROOT / 'skills' / 'adhd-shape' / 'SKILL.md', shipped)
                skill = roots[origin] / 'adhd-shape' / 'SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text('---\nname: adhd-shape\ndescription: user-owned skill\n---\n',
                                 encoding='utf-8')
                if origin == 'configured':
                    (codex / 'config.toml').write_text(
                        '[[skills.config]]\npath=' + json.dumps(str(skill)) + '\n', encoding='utf-8')
                with patch.dict(os.environ, {'CODEX_HOME': str(codex),
                                             'ADHD_HOME': str(isolated / 'state')}), \
                        patch('adhd.core.ROOT', release), \
                        patch('adhd.core.Path.home', return_value=isolated / 'os-home'):
                    result = skill_search(query, limit=1, workspace=workspace)
                    self.assertEqual(result[0]['path'], str(skill.resolve()))
                    if origin == 'user':
                        with redirect_stdout(io.StringIO()) as output:
                            rc = cli_main(['skills', query, '--limit', '1',
                                           '--workspace', str(workspace)])
                        self.assertEqual(rc, 0)
                        self.assertEqual(json.loads(output.getvalue())[0]['path'],
                                         str(skill.resolve()))
                    other = codex / 'skills' / 'distinct-task' / 'SKILL.md'
                    other.parent.mkdir(parents=True)
                    other.write_text('---\nname: distinct-task\ndescription: ' + query + '\n---\n',
                                     encoding='utf-8')
                    ranked = skill_search(query, limit=10, workspace=workspace)
                    self.assertEqual(ranked[0]['path'], str(other.resolve()))
                    self.assertIn(str(skill.resolve()), [row['path'] for row in ranked])
                    self.assertNotIn(str(shipped.resolve()), [row['path'] for row in ranked])

    def test_local_routing_keeps_release_fallback_when_user_path_is_disabled(self):
        skill = self.codex / 'skills' / 'adhd-shape' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: adhd-shape\ndescription: user-owned skill\n---\n', encoding='utf-8')
        (self.codex / 'config.toml').write_text(
            '[[skills.config]]\npath=' + json.dumps(str(skill)) + '\nenabled=false\n', encoding='utf-8')
        with patch('adhd.core.Path.home', return_value=self.base / 'os-home'):
            routed = route_skills('$adhd-shape', workspace=self.workspace)
            self.assertEqual(routed['primary'][0]['path'],
                             str((ROOT / 'skills' / 'adhd-shape' / 'SKILL.md').resolve()))
            searched = skill_search('adhd-shape', limit=1, workspace=self.workspace)
            self.assertEqual(searched[0]['path'], routed['primary'][0]['path'])

    def test_shipped_ui_invocations_and_local_references_resolve(self):
        public = {path.relative_to(ROOT).as_posix() for path in package_adhd.public_files()}
        for name in WORKFLOW_SKILLS:
            with self.subTest(name=name):
                skill = ROOT / 'skills' / name / 'SKILL.md'
                check_local_links(self, skill)
                metadata = ROOT / 'skills' / name / 'agents' / 'openai.yaml'
                # The shipped schema uses quoted JSON-compatible YAML strings;
                # keep this regression independent of optional YAML packages.
                fields = dict((key, json.loads(value)) for key, value in re.findall(
                    r'^  (display_name|short_description|default_prompt): (.+)$',
                    metadata.read_text(encoding='utf-8'), re.M))
                self.assertEqual(len(fields), 3)
                self.assertIn('$' + name, fields['default_prompt'])
                self.assertTrue(25 <= len(fields['short_description']) <= 64)
                self.assertIn(skill.relative_to(ROOT).as_posix(), public)
                self.assertIn(metadata.relative_to(ROOT).as_posix(), public)
        shared = ROOT / 'skills' / 'adhd-native' / 'references' / 'skill-handoff.md'
        check_local_links(self, shared)
        self.assertIn(shared.relative_to(ROOT).as_posix(), public)


class WorkflowInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.codex = self.base / 'codex'
        self.codex.mkdir()
        self.config = ('# existing settings\nmodel="user-model"\n'
                       'model_reasoning_effort="high"\napproval_policy="never"\n'
                       '[model_providers.local]\nname="Local"\nbase_url="http://localhost:1"\n')
        (self.codex / 'config.toml').write_text(self.config, encoding='utf-8')
        (self.codex / 'hooks.json').write_text('{"hooks":{}}', encoding='utf-8')
        (self.codex / 'AGENTS.md').write_text('Existing guidance\n', encoding='utf-8')
        (self.codex / 'auth.json').write_bytes(b'fixture-auth-sentinel\n')

    def tearDown(self):
        self.temp.cleanup()

    def assert_preserved_settings(self):
        config = tomllib.loads((self.codex / 'config.toml').read_text(encoding='utf-8'))
        original = tomllib.loads(self.config)
        for key, value in original.items():
            self.assertEqual(config[key], value)
        self.assertEqual((self.codex / 'auth.json').read_bytes(), b'fixture-auth-sentinel\n')

    def assert_installed_helpers(self, record):
        self.assertEqual({row['name'] for row in record['workflow_skills']}, set(WORKFLOW_SKILLS))
        self.assertEqual(len(record['builtin_skills']), 50)
        for name in WORKFLOW_SKILLS:
            skill = self.codex / 'skills' / name / 'SKILL.md'
            self.assertTrue(skill.is_file(), name)
            self.assertTrue((skill.parent / 'agents' / 'openai.yaml').is_file(), name)
            check_local_links(self, skill)
        native = self.codex / 'skills' / 'adhd-native' / 'SKILL.md'
        self.assertNotIn('ADHD_ROOT', native.read_text(encoding='utf-8'))
        self.assertNotIn('ADHD_PYTHON', native.read_text(encoding='utf-8'))
        check_local_links(self, native)
        self.assert_preserved_settings()

    def test_fresh_install_and_rollback_preserve_later_user_additions(self):
        record = install_native(self.codex, fixture_mode=True)
        self.assert_installed_helpers(record)
        self.assertTrue(all(row['status'] == 'added' for row in record['workflow_skills']))
        addition = self.codex / 'skills' / 'adhd-shape' / 'user-note.txt'
        addition.write_bytes(b'keep this user file\n')
        rollback_native(self.codex)
        self.assertEqual(addition.read_bytes(), b'keep this user file\n')
        self.assertFalse((addition.parent / 'SKILL.md').exists())
        for name in set(WORKFLOW_SKILLS) - {'adhd-shape'}:
            self.assertFalse((self.codex / 'skills' / name).exists())
        self.assertEqual((self.codex / 'config.toml').read_text(encoding='utf-8'), self.config)

    def test_existing_whole_folders_are_preserved_and_fallback_is_discoverable(self):
        existing = self.codex / 'skills' / 'adhd-shape'
        existing.mkdir(parents=True)
        skill = existing / 'SKILL.md'
        skill.write_text('---\nname: adhd-shape\ndescription: user-owned skill\n---\nUser skill.\n', encoding='utf-8')
        asset = existing / 'user-data.bin'
        asset.write_bytes(b'\x00\x01user-owned')
        partial = self.codex / 'skills' / 'adhd-challenge'
        partial.mkdir()
        note = partial / 'note.txt'
        note.write_bytes(b'partial user folder\n')
        before = {path: path.read_bytes() for path in (skill, asset, note)}
        record = install_native(self.codex, fixture_mode=True)
        rows = {row['name']: row for row in record['workflow_skills']}
        for name in ('adhd-shape', 'adhd-challenge'):
            self.assertEqual(rows[name]['status'], 'preserved_existing')
            fallback = Path(rows[name]['fallback'])
            self.assertTrue(fallback.is_file())
            check_local_links(self, fallback)
        self.assertFalse((existing / 'agents').exists())
        self.assertFalse((partial / 'SKILL.md').exists())
        with patch.dict(os.environ, {'CODEX_HOME': str(self.codex),
                                    'ADHD_HOME': str(self.base / 'state')}), patch('adhd.core.ROOT', self.base / 'absent'):
            entries = discover_skills(self.base)
            paths = {row['path'] for row in entries}
            self.assertIn(str(skill.resolve()), paths)
            for name in ('adhd-shape', 'adhd-challenge'):
                self.assertIn(str(Path(rows[name]['fallback']).resolve()), paths)
            routed = route_skills('$adhd-shape', workspace=self.base)
            self.assertEqual(routed['primary'][0]['path'], str(skill.resolve()))
            partial_routed = route_skills('$adhd-challenge', workspace=self.base)
            self.assertEqual(partial_routed['primary'][0]['path'],
                             str(Path(rows['adhd-challenge']['fallback']).resolve()))
            searched = skill_search('adhd-shape Shape a vague idea into a bounded ADHD task',
                                    limit=1, workspace=self.base)
            self.assertEqual(searched[0]['path'], str(skill.resolve()))
            partial_searched = skill_search('adhd-challenge', limit=1, workspace=self.base)
            self.assertEqual(partial_searched[0]['path'],
                             str(Path(rows['adhd-challenge']['fallback']).resolve()))
        self.assert_preserved_settings()
        rollback_native(self.codex)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_upgrade_from_source_without_helpers_adds_all_seven(self):
        old_source = self.base / 'old-source'
        _copy_release(ROOT, old_source)
        for name in WORKFLOW_SKILLS:
            folder = (old_source / 'skills' / name).resolve()
            self.assertTrue(folder.is_relative_to(old_source.resolve()))
            shutil.rmtree(folder)
        with patch('adhd.native_install.ROOT', old_source):
            prior = install_native(self.codex, fixture_mode=True)
        self.assertEqual(prior['workflow_skills'], [])
        self.assertTrue(all(not (self.codex / 'skills' / name).exists() for name in WORKFLOW_SKILLS))
        result = upgrade_native(self.codex, fixture_mode=True)
        self.assertEqual(result['upgraded_from'], prior['version'])
        self.assertNotEqual(result['source_identity']['code_digest'], prior['source_identity']['code_digest'])
        self.assert_installed_helpers(result)
        rollback_native(self.codex)
        self.assertEqual((self.codex / 'config.toml').read_text(encoding='utf-8'), self.config)

    def test_public_archive_installs_helpers_with_complete_local_references(self):
        output = self.base / 'ADHD-source.zip'
        with patch.object(package_adhd, 'OUTPUT', output), redirect_stdout(io.StringIO()):
            package_adhd.main()
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            archive.extractall(self.base / 'extracted')
        source = self.base / 'extracted' / package_adhd.ARCHIVE_ROOT
        with patch('adhd.native_install.ROOT', source):
            record = install_native(self.codex, fixture_mode=True)
        self.assert_installed_helpers(record)
        rollback_native(self.codex)

    def test_managed_helper_and_user_addition_both_survive_upgrade(self):
        prior_source = self.base / 'prior-source'
        _copy_release(ROOT, prior_source)
        prior_skill = prior_source / 'skills' / 'adhd-optimize' / 'SKILL.md'
        with prior_skill.open('a', encoding='utf-8') as stream:
            stream.write('\nPrior release marker.\n')
        with patch('adhd.native_install.ROOT', prior_source):
            prior = install_native(self.codex, fixture_mode=True)
        note = self.codex / 'skills' / 'adhd-optimize' / 'user-note.txt'
        note.write_bytes(b'User addition must survive upgrade\n')
        result = upgrade_native(self.codex, fixture_mode=True)
        self.assertEqual(result['upgraded_from'], prior['version'])
        self.assert_installed_helpers(result)
        self.assertEqual(note.read_bytes(), b'User addition must survive upgrade\n')
        self.assertNotIn('Prior release marker', (note.parent / 'SKILL.md').read_text(encoding='utf-8'))
        rows = {row['name']: row for row in result['workflow_skills']}
        self.assertEqual(rows['adhd-optimize']['status'], 'added')
        with patch.dict(os.environ, {'CODEX_HOME': str(self.codex),
                                    'ADHD_HOME': str(self.base / 'state')}), patch('adhd.core.ROOT', self.base / 'absent'):
            self.assertIn(str((note.parent / 'SKILL.md').resolve()),
                          [row['path'] for row in discover_skills(self.base)])
        rollback_native(self.codex)
        self.assertEqual(note.read_bytes(), b'User addition must survive upgrade\n')

    def test_upgrade_does_not_transfer_ownership_to_another_agents_home(self):
        prior_source = self.base / 'prior-source'
        _copy_release(ROOT, prior_source)
        with (prior_source / 'README.md').open('a', encoding='utf-8') as stream:
            stream.write('\nPrior release marker.\n')
        with patch('adhd.native_install.ROOT', prior_source):
            install_native(self.codex, fixture_mode=True)
        destination = self.base / 'other-agents'
        user_folder = destination / 'skills' / 'adhd-optimize'
        user_folder.mkdir(parents=True)
        note = user_folder / 'user-note.txt'
        note.write_bytes(b'Other agents home belongs to the user\n')
        result = upgrade_native(self.codex, destination, fixture_mode=True)
        rows = {row['name']: row for row in result['workflow_skills']}
        self.assertEqual(rows['adhd-optimize']['status'], 'preserved_existing')
        self.assertEqual(note.read_bytes(), b'Other agents home belongs to the user\n')
        self.assertFalse((user_folder / 'SKILL.md').exists())
        self.assertTrue(Path(rows['adhd-optimize']['fallback']).is_file())
        rollback_native(self.codex)
        self.assertEqual(note.read_bytes(), b'Other agents home belongs to the user\n')

    def test_upgrade_collision_preserves_user_file_and_recovers_prior_install(self):
        prior = install_native(self.codex, fixture_mode=True)
        skill = self.codex / 'skills' / 'adhd-optimize' / 'SKILL.md'
        skill_bytes = skill.read_bytes()
        record_path = self.codex / 'adhd' / 'native-installation.json'
        record_bytes = record_path.read_bytes()
        note = skill.parent / 'user-note.txt'
        note.write_bytes(b'User-owned collision\n')
        next_source = self.base / 'next-source'
        _copy_release(ROOT, next_source)
        (next_source / 'skills' / 'adhd-optimize' / 'user-note.txt').write_bytes(b'New release file\n')
        with patch('adhd.native_install.ROOT', next_source):
            with self.assertRaisesRegex(ValueError, 'Unmanaged capability already exists'):
                upgrade_native(self.codex, fixture_mode=True)
        self.assertEqual(note.read_bytes(), b'User-owned collision\n')
        self.assertEqual(skill.read_bytes(), skill_bytes)
        self.assertEqual(record_path.read_bytes(), record_bytes)
        self.assertTrue(Path(prior['release']).is_dir())
        self.assert_preserved_settings()
        rollback_native(self.codex)
        self.assertEqual(note.read_bytes(), b'User-owned collision\n')


if __name__ == '__main__':
    unittest.main()
