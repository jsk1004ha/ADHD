from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apzn.native_install import install_native,release_identity,rollback_native,upgrade_native


class NativeUpgradeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.codex=self.root/'codex';self.agents=self.root/'agents'
        self.codex.mkdir();(self.codex/'config.toml').write_text('model="gpt-6-sol"\n');(self.codex/'AGENTS.md').write_text('user guidance\n')
        (self.codex/'hooks.json').write_text(json.dumps({'hooks':{}}))

    def tearDown(self):self.temp.cleanup()

    def test_same_digest_returns_noop(self):
        installed=install_native(self.codex,self.agents,fixture_mode=True)
        result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['status'],'already_installed');self.assertEqual(result['release_identity'],installed['release_identity'])
        rollback_native(self.codex)

    def test_new_install_uses_adhd_executable_path(self):
        installed=install_native(self.codex,self.agents,fixture_mode=True)
        release=Path(installed['release'])
        self.assertEqual(installed['display_version'],'ADHD — Autonomous Delegation Harness Director v0.1.3')
        self.assertTrue(release.is_relative_to(self.codex/'adhd'/'releases'))
        self.assertTrue((release/'adhd.py').is_file())
        self.assertTrue((release/'apzn.py').is_file())
        self.assertTrue((self.codex/'adhd'/'native-installation.json').is_file())
        hooks=json.loads((self.codex/'hooks.json').read_text(encoding='utf-8'))
        commands=[hook['commandWindows'] for groups in hooks['hooks'].values()
                  for group in groups for hook in group['hooks']]
        self.assertTrue(all(str(release/'hook.py') in command for command in commands))
        rollback_native(self.codex)

    def test_old_apzn_install_upgrades_without_losing_native_state(self):
        with patch('apzn.native_install.INSTALL_SUBDIR','apzn'):
            old=install_native(self.codex,self.agents,fixture_mode=True)
        preserved=self.codex/'apzn'/'native'/'keep.json'
        preserved.parent.mkdir(parents=True,exist_ok=True)
        preserved.write_text('{"keep":true}\n',encoding='utf-8')
        old_record=self.codex/'apzn'/'native-installation.json'
        self.assertTrue(old_record.is_file())
        result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['upgraded_from'],old['version'])
        self.assertTrue(Path(result['release']).is_relative_to(self.codex/'adhd'/'releases'))
        self.assertTrue((self.codex/'adhd'/'native-installation.json').is_file())
        self.assertFalse(old_record.exists())
        self.assertEqual(preserved.read_text(encoding='utf-8'),'{"keep":true}\n')
        self.assertEqual((self.codex/'config.toml').read_text(), 'model="gpt-6-sol"\n')
        rollback_native(self.codex)

    def test_old_apzn_install_is_not_silently_installed_side_by_side(self):
        with patch('apzn.native_install.INSTALL_SUBDIR','apzn'):
            install_native(self.codex,self.agents,fixture_mode=True)
        with self.assertRaisesRegex(ValueError,'upgrade'):
            install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue((self.codex/'apzn'/'native-installation.json').is_file())
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())
        rollback_native(self.codex)

    def test_failed_path_migration_restores_old_installation(self):
        with patch('apzn.native_install.INSTALL_SUBDIR','apzn'):
            install_native(self.codex,self.agents,fixture_mode=True)
        old_record=self.codex/'apzn'/'native-installation.json'
        before={path:path.read_bytes() for path in
                (old_record,self.codex/'hooks.json',self.codex/'AGENTS.md')}
        with patch('apzn.native_install._install_native',side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError,'injected'):
                upgrade_native(self.codex,self.agents,fixture_mode=True)
        for path,content in before.items():
            self.assertEqual(path.read_bytes(),content)
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())
        rollback_native(self.codex)

    def test_same_digest_with_managed_drift_is_not_noop(self):
        install_native(self.codex,self.agents,fixture_mode=True)
        hooks=json.loads((self.codex/'hooks.json').read_text());hooks['hooks']['Stop'][-1]['hooks'][0]['timeout']=999
        (self.codex/'hooks.json').write_text(json.dumps(hooks))
        with self.assertRaises(ValueError):upgrade_native(self.codex,self.agents,fixture_mode=True)

    def test_same_display_version_new_digest_upgrades(self):
        installed=install_native(self.codex,self.agents,fixture_mode=True);new={**release_identity(),'code_digest':'f'*64,'build_id':'test-new-build'}
        with patch('apzn.native_install.release_identity',return_value=new):
            result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['upgraded_from'],installed['version'])
        self.assertEqual(result['release_identity'],release_identity(Path(result['release'])))
        rollback_native(self.codex)

    def test_legacy_release_record_migrates_with_managed_files_checked(self):
        with patch('apzn.native_install.INSTALL_SUBDIR','apzn'):
            installed=install_native(self.codex,self.agents,fixture_mode=True)
        record_path=self.codex/'apzn'/'native-installation.json'
        legacy=json.loads(record_path.read_text(encoding='utf-8'))
        legacy['version']='0.1.2'
        legacy['release_identity'].pop('files')
        legacy['release_identity']['native_schema_version']=2
        legacy.pop('source_identity',None)
        record_path.write_text(json.dumps(legacy),encoding='utf-8')
        result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['upgraded_from'],legacy['version'])
        self.assertEqual(result['release_identity'],release_identity(Path(result['release'])))
        rollback_native(self.codex)

    def test_legacy_release_cannot_self_approve_a_noop_upgrade(self):
        with patch('apzn.native_install.INSTALL_SUBDIR','apzn'):
            install_native(self.codex,self.agents,fixture_mode=True)
        record_path=self.codex/'apzn'/'native-installation.json'
        legacy=json.loads(record_path.read_text(encoding='utf-8'))
        legacy['version']='0.1.2'
        legacy['release_identity'].pop('files')
        legacy['release_identity']['native_schema_version']=2
        legacy.pop('source_identity',None)
        record_path.write_text(json.dumps(legacy),encoding='utf-8')
        with patch('apzn.native_install.ROOT',Path(legacy['release'])):
            with self.assertRaisesRegex(ValueError,'cannot self-upgrade'):
                upgrade_native(self.codex,self.agents,fixture_mode=True)

    def test_release_copy_excludes_session_and_plan_state(self):
        result=install_native(self.codex,self.agents,fixture_mode=True);release=Path(result['release'])
        self.assertFalse((release/'.apzn').exists());self.assertFalse((release/'docs/plans').exists());self.assertFalse((release/'evidence').exists())
        rollback_native(self.codex)

    def test_unrelated_hook_and_guidance_edits_survive_rollback(self):
        install_native(self.codex,self.agents,fixture_mode=True)
        hooks=json.loads((self.codex/'hooks.json').read_text());hooks['hooks']['PostToolUse'].insert(0,{'matcher':'code-review-graph','hooks':[{'type':'command','command':'fixed-user-hook'}]})
        (self.codex/'hooks.json').write_text(json.dumps(hooks))
        with (self.codex/'AGENTS.md').open('a',encoding='utf-8') as f:f.write('\nLater user guidance\n')
        rollback_native(self.codex)
        restored=json.loads((self.codex/'hooks.json').read_text())
        self.assertEqual(restored['hooks']['PostToolUse'][0]['matcher'],'code-review-graph')
        self.assertIn('Later user guidance',(self.codex/'AGENTS.md').read_text())


if __name__=='__main__':unittest.main()
