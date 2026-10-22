from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adhd.core import atomic_json as actual_atomic_json, file_hash as actual_file_hash
from adhd.native_install import install_native,release_identity,rollback_native,upgrade_native,_release_files,_restore_moved_edit
from adhd import DISPLAY_NAME, __version__


class NativeUpgradeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.codex=self.root/'codex';self.agents=self.root/'agents'
        self.codex.mkdir();(self.codex/'config.toml').write_text('model="gpt-6-sol"\n');(self.codex/'AGENTS.md').write_text('user guidance\n')
        (self.codex/'hooks.json').write_text(json.dumps({'hooks':{}}))

    def tearDown(self):self.temp.cleanup()

    def _minimal_failure_source(self):
        # Failure tests exercise real publication/rollback without copying the
        # full bundled release on every injected fault.
        source=self.root/'failure-source'
        skill=source/'skills'/'adhd-native'/'SKILL.md'
        skill.parent.mkdir(parents=True,exist_ok=True)
        skill.write_text('# Fixture native skill\n',encoding='utf-8')
        (source/'hook.py').write_text('# fixture hook\n',encoding='utf-8')
        return patch.multiple('adhd.native_install',ROOT=source,
            skill_manifest=lambda _root:{},mcp_catalog=lambda _root:[],
            skill_changes=lambda _home,_release:({},[]),
            prepare_mcp_config=lambda original,_release:(original,[],[]))

    def test_transient_windows_release_publication_denial_retries_safely(self):
        original_replace = os.replace
        attempts = []
        def replace(source, target):
            if Path(source).name.startswith('.staging-'):
                attempts.append((Path(source), Path(target)))
                if len(attempts) == 1:
                    error = PermissionError('transient release publication denial')
                    error.winerror = 5
                    raise error
            return original_replace(source, target)
        with self._minimal_failure_source(), patch('adhd.native_install.os.replace', side_effect=replace), patch('adhd.native_install.time.sleep'):
            result = install_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(result['migrated_roles'], [])
        self.assertEqual(len(attempts), 2)
        self.assertTrue((self.agents/'skills'/'adhd-native'/'SKILL.md').is_file())
        self.assertIn('model="gpt-6-sol"', (self.codex/'config.toml').read_text())

    def test_permanent_publication_denial_is_bounded_and_preserves_profile(self):
        original_replace = os.replace
        originals = {p:p.read_bytes() for p in self.codex.iterdir() if p.is_file()}
        attempts = []
        def replace(source, target):
            if Path(source).name.startswith('.staging-'):
                attempts.append((source, target))
                error = PermissionError('persistent release publication denial')
                error.winerror = 5
                raise error
            return original_replace(source, target)
        with self._minimal_failure_source(), patch('adhd.native_install.os.replace', side_effect=replace), patch('adhd.native_install.time.sleep'):
            with self.assertRaises(PermissionError):
                install_native(self.codex, self.agents, fixture_mode=True)
        self.assertGreaterEqual(len(attempts), 1)
        self.assertLessEqual(len(attempts), 5)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_non_windows_publication_error_is_not_retried(self):
        original_replace = os.replace
        attempts = []
        def replace(source, target):
            if Path(source).name.startswith('.staging-'):
                attempts.append((source, target))
                raise PermissionError('ordinary permission denial')
            return original_replace(source, target)
        with self._minimal_failure_source(), patch('adhd.native_install.os.replace', side_effect=replace), patch('adhd.native_install.time.sleep'):
            with self.assertRaises(PermissionError):
                install_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(len(attempts), 1)

    def test_publication_retry_never_replaces_an_unexpected_destination(self):
        original_replace = os.replace
        destinations = []
        def replace(source, target):
            if Path(source).name.startswith('.staging-'):
                destinations.append(Path(target))
                Path(target).mkdir()
                (Path(target)/'preserve.txt').write_text('other publisher')
                error = PermissionError('sharing violation with conflicting destination')
                error.winerror = 32
                raise error
            return original_replace(source, target)
        with self._minimal_failure_source(), patch('adhd.native_install.os.replace', side_effect=replace), patch('adhd.native_install.time.sleep'):
            with self.assertRaises(OSError):
                install_native(self.codex, self.agents, fixture_mode=True)
        self.assertEqual(len(destinations), 1)
        self.assertEqual((destinations[0]/'preserve.txt').read_text(), 'other publisher')

    def test_release_inputs_exclude_caches_and_include_optimization_guide(self):
        source=self.root/'source'
        included=('adhd/public.py','docs/optimization.md','scripts/evaluate_context_components.py')
        excluded=('adhd/cache/private.json','adhd/tmp/result.log','adhd/data.sqlite3',
                  'adhd/auth.json','adhd/.env.local','tests/.pytest_cache/state.json')
        for name in (*included,*excluded):
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture')
        actual={path.relative_to(source).as_posix() for path in _release_files(source)}
        self.assertEqual(actual,set(included))

    @unittest.skipUnless(os.name=='nt','Windows directory junction regression')
    def test_release_inputs_exclude_directory_junctions(self):
        source=self.root/'junction-source'
        public=source/'adhd'/'public.py'
        public.parent.mkdir(parents=True)
        public.write_text('# public fixture\n',encoding='utf-8')
        outside=self.root/'outside-release'
        outside.mkdir()
        (outside/'private.py').write_text('# excluded fixture\n',encoding='utf-8')
        links=(source/'adhd'/'nested',source/'tests')
        for link in links:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
                "-Path $env:ADHD_TEST_JUNCTION -Target $env:ADHD_TEST_TARGET | Out-Null"],
                env={**os.environ,'ADHD_TEST_JUNCTION':str(link),
                     'ADHD_TEST_TARGET':str(outside)},capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
        actual={path.relative_to(source).as_posix() for path in _release_files(source)}
        self.assertEqual(actual,{'adhd/public.py'})
        self.assertEqual((outside/'private.py').read_text(encoding='utf-8'),'# excluded fixture\n')

    def test_same_digest_returns_noop(self):
        installed=install_native(self.codex,self.agents,fixture_mode=True)
        backups_before={path.name for path in (self.codex/'adhd'/'backups').iterdir()}
        result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['status'],'already_installed');self.assertEqual(result['release_identity'],installed['release_identity'])
        self.assertEqual({path.name for path in (self.codex/'adhd'/'backups').iterdir()},backups_before)
        rollback_native(self.codex)

    def test_changed_upgrade_keeps_one_verified_backup(self):
        fixture=self._minimal_failure_source()
        source=self.root/'failure-source'/'hook.py'
        with fixture:
            install_native(self.codex,self.agents,fixture_mode=True)
            source.write_text('# changed hook\n',encoding='utf-8')
            result=upgrade_native(self.codex,self.agents,fixture_mode=True)
            self.assertNotEqual(result.get('status'),'already_installed')
            self.assertNotEqual(result['retention'].get('status'),'cleanup_pending')
            backups=[path for path in (self.codex/'adhd'/'backups').iterdir() if path.is_dir()]
            self.assertEqual(len(backups),1)
            record=json.loads((self.codex/'adhd'/'native-installation.json').read_text(encoding='utf-8'))
            self.assertEqual(Path(record['backup']),backups[0])
            hooks_row=next(row for row in record['files'] if row['path']==str(self.codex/'hooks.json'))
            self.assertEqual(Path(hooks_row['backup']).read_bytes(),b'{"hooks": {}}')
            for _ in range(20):
                self.assertEqual(upgrade_native(self.codex,self.agents,fixture_mode=True)['status'],
                                 'already_installed')
            self.assertEqual(len(list((self.codex/'adhd'/'backups').iterdir())),1)
            rollback_native(self.codex)

    def test_cleanup_failure_does_not_rollback_committed_upgrade(self):
        fixture=self._minimal_failure_source()
        source=self.root/'failure-source'/'hook.py'
        with fixture:
            first=install_native(self.codex,self.agents,fixture_mode=True)
            source.write_text('# next hook\n',encoding='utf-8')
            with patch('adhd.storage._prune_after_upgrade_locked',side_effect=OSError('cleanup denied')):
                upgraded=upgrade_native(self.codex,self.agents,fixture_mode=True)
            self.assertEqual(upgraded['retention']['status'],'cleanup_pending')
            self.assertNotEqual(upgraded['release'],first['release'])
            record=json.loads((self.codex/'adhd'/'native-installation.json').read_text(encoding='utf-8'))
            self.assertEqual(record['release'],upgraded['release'])
            self.assertTrue(Path(record['backup']).is_dir())
            rollback_native(self.codex)

    def test_new_install_uses_adhd_executable_path(self):
        installed=install_native(self.codex,self.agents,fixture_mode=True)
        release=Path(installed['release'])
        self.assertEqual(installed['display_version'],f'{DISPLAY_NAME} v{__version__}')
        self.assertTrue(release.is_relative_to(self.codex/'adhd'/'releases'))
        self.assertTrue((release/'adhd.py').is_file())
        self.assertTrue((release/'adhd.py').is_file())
        self.assertTrue((self.codex/'adhd'/'native-installation.json').is_file())
        hooks=json.loads((self.codex/'hooks.json').read_text(encoding='utf-8'))
        commands=[hook['commandWindows'] for groups in hooks['hooks'].values()
                  for group in groups for hook in group['hooks']]
        self.assertTrue(all(str(release/'hook.py') in command for command in commands))
        rollback_native(self.codex)

    def test_post_publish_hash_failure_restores_existing_file(self):
        target=self.codex/'hooks.json';original=target.read_bytes()
        raised=False
        def fail_once(path):
            nonlocal raised
            if Path(path)==target and not raised and target.read_bytes()!=original:
                raised=True
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=fail_once):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(raised)
        self.assertEqual(target.read_bytes(),original)
        self.assertEqual((self.codex/'AGENTS.md').read_text(),'user guidance\n')
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())

    def test_post_publish_hash_failure_removes_new_file(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        raised=False
        def fail_once(path):
            nonlocal raised
            if Path(path)==target and not raised and target.is_file():
                raised=True
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        original_hooks=(self.codex/'hooks.json').read_bytes()
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=fail_once):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(raised)
        self.assertFalse(target.exists())
        self.assertEqual((self.codex/'hooks.json').read_bytes(),original_hooks)
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())

    def test_post_publish_concurrent_edit_is_preserved(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        later=b'user edited this file after publication\n'
        raised=False
        def edit_then_fail(path):
            nonlocal raised
            if Path(path)==target and not raised and target.is_file():
                raised=True
                target.write_bytes(later)
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        original_hooks=(self.codex/'hooks.json').read_bytes()
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=edit_then_fail):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(raised)
        self.assertEqual(target.read_bytes(),later)
        self.assertEqual((self.codex/'hooks.json').read_bytes(),original_hooks)
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())
        self.assertTrue((self.codex/'adhd'/'native-install-recovery.json').is_file())

    def test_persistent_post_publish_hash_failure_restores_every_row(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        original_hooks=(self.codex/'hooks.json').read_bytes()
        calls=0
        def fail_persistently(path):
            nonlocal calls
            if Path(path)==target and target.is_file():
                calls+=1
                raise MemoryError('persistent injected hash failure')
            return actual_file_hash(path)
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=fail_persistently):
            with self.assertRaisesRegex(MemoryError,'persistent injected'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertGreaterEqual(calls,1)
        self.assertFalse(target.exists())
        self.assertEqual((self.codex/'hooks.json').read_bytes(),original_hooks)
        self.assertFalse((self.codex/'adhd'/'native-install-recovery.json').exists())

    @unittest.skipUnless(os.name=='posix','POSIX permission bits required')
    def test_post_publish_concurrent_private_edit_retains_permissions(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        later=b'private user edit after publication\n'
        raised=False
        def edit_then_fail(path):
            nonlocal raised
            if Path(path)==target and not raised and target.is_file():
                raised=True
                target.write_bytes(later)
                target.chmod(0o600)
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        previous_umask=os.umask(0o022)
        try:
            with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=edit_then_fail):
                with self.assertRaisesRegex(MemoryError,'post-publish'):
                    install_native(self.codex,self.agents,fixture_mode=True)
        finally:
            os.umask(previous_umask)
        self.assertTrue(raised)
        self.assertEqual(target.read_bytes(),later)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
        recovery=json.loads((self.codex/'adhd'/'native-install-recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'],'rollback_incomplete')
        moved=next(Path(row['quarantine']) for row in recovery['remaining'] if row['path']==str(target))
        self.assertEqual(moved.read_bytes(),later)
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode),0o600)

    def test_after_hash_concurrent_edit_keeps_recovery_material(self):
        target=self.codex/'hooks.json'
        new_file=self.agents/'skills'/'adhd-native'/'SKILL.md'
        later=b'user edit after rollback verification\n'
        original_link=os.link
        edited=False
        failed=False
        def link_after_edit(src,dst,*args,**kwargs):
            nonlocal edited
            if Path(dst)==target and not edited:
                edited=True
                target.write_bytes(later)
            return original_link(src,dst,*args,**kwargs)
        def fail_once(path):
            nonlocal failed
            if Path(path)==new_file and not failed and new_file.is_file():
                failed=True
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=fail_once), \
                patch('adhd.native_install.os.link',side_effect=link_after_edit):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(edited)
        self.assertEqual(target.read_bytes(),later)
        marker=self.codex/'adhd'/'native-install-recovery.json'
        recovery=json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'],'rollback_incomplete')
        self.assertTrue(Path(recovery['release']).is_dir())
        self.assertTrue(Path(recovery['backup']).is_dir())
        self.assertTrue(any(row['path']==str(target) for row in recovery['remaining']))
        with self.assertRaisesRegex(ValueError,'recovery'):
            install_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(target.read_bytes(),later)

    def test_failed_recovery_marker_update_preserves_initial_marker_and_dependencies(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        raised=False
        def edit_then_fail(path):
            nonlocal raised
            if Path(path)==target and not raised and target.is_file():
                raised=True
                target.write_bytes(b'concurrent edit\n')
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        def fail_final_marker(path,value):
            if Path(path).name=='native-install-recovery.json' and value.get('status')=='rollback_incomplete':
                raise OSError('injected marker update failure')
            return actual_atomic_json(path,value)
        with self._minimal_failure_source(), \
                patch('adhd.native_install.file_hash',side_effect=edit_then_fail), \
                patch('adhd.native_install.atomic_json',side_effect=fail_final_marker):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        marker=self.codex/'adhd'/'native-install-recovery.json'
        recovery=json.loads(marker.read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'],'rollback_pending')
        self.assertTrue(Path(recovery['release']).is_dir())
        self.assertTrue(Path(recovery['backup']).is_dir())
        self.assertEqual(target.read_bytes(),b'concurrent edit\n')
        with self.assertRaisesRegex(ValueError,'recovery'):
            install_native(self.codex,self.agents,fixture_mode=True)

    def test_upgrade_does_not_hide_incomplete_new_install(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        with self._minimal_failure_source():
            install_native(self.codex,self.agents,fixture_mode=True)
            (self.root/'failure-source'/'skills'/'adhd-native'/'SKILL.md').write_bytes(b'# changed skill\n')
            def edit_then_fail(path):
                if Path(path)==target and target.is_file() and target.read_bytes()==b'# changed skill\n':
                    target.write_bytes(b'user edit during upgrade\n')
                    raise MemoryError('injected post-publish hash failure')
                return actual_file_hash(path)
            with patch('adhd.native_install.file_hash',side_effect=edit_then_fail):
                with self.assertRaisesRegex(ValueError,'recovery'):
                    upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(target.read_bytes(),b'user edit during upgrade\n')
        marker=json.loads((self.codex/'adhd'/'native-install-recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['status'],'rollback_incomplete')
        self.assertTrue(Path(marker['release']).is_dir())
        self.assertTrue(Path(marker['backup']).is_dir())

    def test_after_hash_symlink_swap_does_not_follow_target(self):
        target=self.codex/'hooks.json'
        new_file=self.agents/'skills'/'adhd-native'/'SKILL.md'
        third=self.root/'third-party.txt';third.write_bytes(b'third party original\n')
        probe=self.root/'probe-link'
        try:
            probe.symlink_to(third)
        except OSError:
            self.skipTest('symlink creation unavailable')
        probe.unlink()
        original_link=os.link
        swapped=False
        failed=False
        def link_after_swap(src,dst,*args,**kwargs):
            nonlocal swapped
            if Path(dst)==target and not swapped:
                swapped=True
                target.symlink_to(third)
            return original_link(src,dst,*args,**kwargs)
        def fail_once(path):
            nonlocal failed
            if Path(path)==new_file and not failed and new_file.is_file():
                failed=True
                raise MemoryError('injected post-publish hash failure')
            return actual_file_hash(path)
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=fail_once), \
                patch('adhd.native_install.os.link',side_effect=link_after_swap):
            with self.assertRaisesRegex(MemoryError,'post-publish'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(swapped)
        self.assertTrue(target.is_symlink())
        self.assertEqual(third.read_bytes(),b'third party original\n')
        self.assertTrue((self.codex/'adhd'/'native-install-recovery.json').is_file())

    def test_post_publish_symlink_swap_restores_link_without_touching_target(self):
        target=self.agents/'skills'/'adhd-native'/'SKILL.md'
        third=self.root/'third-party.txt';third.write_bytes(b'third party original\n')
        raw_target=os.path.relpath(third,target.parent)
        probe=self.root/'probe-link'
        try:
            probe.symlink_to(third)
        except OSError:
            self.skipTest('symlink creation unavailable')
        probe.unlink()
        original_hooks=(self.codex/'hooks.json').read_bytes()
        swapped=False
        def swap_then_fail(path):
            nonlocal swapped
            if Path(path)==target and not swapped and target.is_file():
                swapped=True
                target.unlink()
                target.symlink_to(raw_target)
                raise MemoryError('injected post-publish link swap')
            return actual_file_hash(path)
        with self._minimal_failure_source(),patch('adhd.native_install.file_hash',side_effect=swap_then_fail):
            with self.assertRaisesRegex(MemoryError,'link swap'):
                install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue(swapped)
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target),raw_target)
        self.assertEqual(third.read_bytes(),b'third party original\n')
        self.assertEqual((self.codex/'hooks.json').read_bytes(),original_hooks)
        recovery=json.loads((self.codex/'adhd'/'native-install-recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(recovery['status'],'rollback_incomplete')
        moved=[row for row in recovery['remaining'] if row['path']==str(target)]
        self.assertEqual(len(moved),1)
        self.assertTrue(Path(moved[0]['quarantine']).is_symlink())

    def test_moved_symlink_restore_refuses_occupied_name(self):
        third=self.root/'third-party.txt';third.write_bytes(b'third party original\n')
        moved=self.root/'moved-link';destination=self.root/'destination'
        try:
            moved.symlink_to(third)
        except OSError:
            self.skipTest('symlink creation unavailable')
        destination.write_bytes(b'concurrent user edit\n')
        with self.assertRaises(FileExistsError):
            _restore_moved_edit(moved,destination,None)
        self.assertEqual(destination.read_bytes(),b'concurrent user edit\n')
        self.assertTrue(moved.is_symlink())
        self.assertEqual(third.read_bytes(),b'third party original\n')

    def test_prior_install_upgrades_without_losing_native_state(self):
        with patch('adhd.native_install.INSTALL_SUBDIR','prior-harness'):
            old=install_native(self.codex,self.agents,fixture_mode=True)
        preserved=self.codex/'prior-harness'/'native'/'keep.json'
        preserved.parent.mkdir(parents=True,exist_ok=True)
        preserved.write_text('{"keep":true}\n',encoding='utf-8')
        old_record=self.codex/'prior-harness'/'native-installation.json'
        self.assertTrue(old_record.is_file())
        result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['upgraded_from'],old['version'])
        self.assertTrue(Path(result['release']).is_relative_to(self.codex/'adhd'/'releases'))
        self.assertTrue((self.codex/'adhd'/'native-installation.json').is_file())
        self.assertFalse(old_record.exists())
        self.assertEqual(preserved.read_text(encoding='utf-8'),'{"keep":true}\n')
        import tomllib
        config=tomllib.loads((self.codex/'config.toml').read_text())
        self.assertEqual(config['model'],'gpt-6-sol')
        self.assertIn('openai-docs',config['mcp_servers'])
        rollback_native(self.codex)
        self.assertEqual((self.codex/'config.toml').read_text(), 'model="gpt-6-sol"\n')

    def test_prior_install_is_not_silently_installed_side_by_side(self):
        with patch('adhd.native_install.INSTALL_SUBDIR','prior-harness'):
            install_native(self.codex,self.agents,fixture_mode=True)
        with self.assertRaisesRegex(ValueError,'upgrade'):
            install_native(self.codex,self.agents,fixture_mode=True)
        self.assertTrue((self.codex/'prior-harness'/'native-installation.json').is_file())
        self.assertFalse((self.codex/'adhd'/'native-installation.json').exists())
        rollback_native(self.codex)

    def test_failed_path_migration_restores_old_installation(self):
        with patch('adhd.native_install.INSTALL_SUBDIR','prior-harness'):
            install_native(self.codex,self.agents,fixture_mode=True)
        old_record=self.codex/'prior-harness'/'native-installation.json'
        before={path:path.read_bytes() for path in
                (old_record,self.codex/'hooks.json',self.codex/'AGENTS.md')}
        with patch('adhd.native_install._install_native',side_effect=RuntimeError('injected')):
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
        with patch('adhd.native_install.release_identity',return_value=new):
            result=upgrade_native(self.codex,self.agents,fixture_mode=True)
        self.assertEqual(result['upgraded_from'],installed['version'])
        self.assertEqual(result['release_identity'],release_identity(Path(result['release'])))
        rollback_native(self.codex)

    def test_legacy_release_record_migrates_with_managed_files_checked(self):
        with patch('adhd.native_install.INSTALL_SUBDIR','adhd'):
            installed=install_native(self.codex,self.agents,fixture_mode=True)
        record_path=self.codex/'adhd'/'native-installation.json'
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
        with patch('adhd.native_install.INSTALL_SUBDIR','adhd'):
            install_native(self.codex,self.agents,fixture_mode=True)
        record_path=self.codex/'adhd'/'native-installation.json'
        legacy=json.loads(record_path.read_text(encoding='utf-8'))
        legacy['version']='0.1.2'
        legacy['release_identity'].pop('files')
        legacy['release_identity']['native_schema_version']=2
        legacy.pop('source_identity',None)
        record_path.write_text(json.dumps(legacy),encoding='utf-8')
        with patch('adhd.native_install.ROOT',Path(legacy['release'])):
            with self.assertRaisesRegex(ValueError,'cannot self-upgrade'):
                upgrade_native(self.codex,self.agents,fixture_mode=True)

    def test_release_copy_excludes_session_and_plan_state(self):
        result=install_native(self.codex,self.agents,fixture_mode=True);release=Path(result['release'])
        self.assertFalse((release/'.adhd').exists());self.assertFalse((release/'docs/plans').exists());self.assertFalse((release/'evidence').exists())
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
