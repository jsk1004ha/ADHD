from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from apzn import extensions
from apzn.core import digest


class ExtensionLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.codex=self.root/'codex';self.codex.mkdir();(self.codex/'config.toml').write_text('# retained\n',encoding='utf-8')
        self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.codex),'APZN_HOME':str(self.codex/'apzn')});self.env.start()
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.script=self.runtime/'server.py';self.script.write_text('print("server")\n',encoding='utf-8')

    def tearDown(self):self.env.stop();self.temp.cleanup()

    def manifest(self,**execution):
        spec={'kind':'script','command':str(Path(sys.executable).resolve()),'entrypoint':str(self.script),
              'runtime_root':str(self.runtime),'runtime_files':[str(self.script)],
              'cwd':str(self.root),'args':[]}
        spec.update(execution)
        return {'id':'sample-mcp','kind':'mcp-stdio','source':'local-test@1 MIT','execution':spec,'env_vars':[]}

    def test_script_entrypoint_required(self):
        manifest=self.manifest();del manifest['execution']['entrypoint']
        with self.assertRaises(ValueError):extensions.stage(manifest)

    def test_script_changed_after_review_rejected(self):
        extensions.stage(self.manifest());extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        self.script.write_text('print("changed")\n',encoding='utf-8')
        with self.assertRaises(ValueError):extensions.launch('sample-mcp',probe_mode=True)

    def test_script_empty_runtime_closure_rejected(self):
        with self.assertRaisesRegex(ValueError,'runtime_files closure'):
            extensions.stage(self.manifest(runtime_files=[]))

    def test_script_omitted_local_helper_rejected(self):
        (self.runtime/'helper.py').write_text('VALUE=1\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'complete local runtime_root closure'):
            extensions.stage(self.manifest())

    def test_script_helper_changed_after_review_rejected(self):
        helper=self.runtime/'helper.py';helper.write_text('VALUE=1\n',encoding='utf-8')
        manifest=self.manifest(runtime_files=[str(self.script),str(helper)])
        extensions.stage(manifest);extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        helper.write_text('VALUE=2\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'runtime closure changed'):
            extensions.launch('sample-mcp',probe_mode=True)

    def test_script_helper_removed_after_review_rejected(self):
        helper=self.runtime/'helper.py';helper.write_text('VALUE=1\n',encoding='utf-8')
        manifest=self.manifest(runtime_files=[str(self.script),str(helper)])
        extensions.stage(manifest);extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        helper.unlink()
        with self.assertRaisesRegex(ValueError,'runtime closure changed'):
            extensions.launch('sample-mcp',probe_mode=True)

    def test_script_helper_added_after_review_rejected_before_apply(self):
        extensions.stage(self.manifest())
        (self.runtime/'helper.py').write_text('VALUE=1\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'runtime closure changed'):
            extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)

    def test_script_runs_local_helper_without_external_site_packages(self):
        helper=self.runtime/'helper.py';helper.write_text('VALUE=7\n',encoding='utf-8')
        self.script.write_text('import helper\nprint(helper.VALUE)\n',encoding='utf-8')
        spec=extensions.validate_execution_spec(self.manifest(
            runtime_files=[str(self.script),str(helper)]))
        record={'execution':spec,'env_vars':[]}
        process=subprocess.run(extensions._target_argv(record),cwd=spec['cwd'],
            env=extensions._runtime_env(record),capture_output=True,check=False)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(process.stdout.strip(),b'7')
        self.script.write_text('import filelock\n',encoding='utf-8')
        spec=extensions.validate_execution_spec(self.manifest(
            runtime_files=[str(self.script),str(helper)]))
        record={'execution':spec,'env_vars':[]}
        process=subprocess.run(extensions._target_argv(record),cwd=spec['cwd'],
            env=extensions._runtime_env(record),capture_output=True,check=False)
        self.assertNotEqual(process.returncode,0)
        self.assertIn(b'ModuleNotFoundError',process.stderr)

    def test_python_interpreter_env_is_rejected_and_external_path_is_not_inherited(self):
        for name in ('PYTHONPATH','PYTHONHOME','PYTHONUSERBASE','PYTHONSTARTUP','PYTHONINSPECT'):
            manifest=self.manifest();manifest['env_vars']=[name]
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'Python interpreter environment'):
                extensions.stage(manifest)
        external=self.root/'external';external.mkdir()
        (external/'outsidepkg.py').write_text('VALUE=73\n',encoding='utf-8')
        self.script.write_text('import outsidepkg\nprint(outsidepkg.VALUE)\n',encoding='utf-8')
        spec=extensions.validate_execution_spec(self.manifest())
        record={'execution':spec,'env_vars':[]}
        with patch.dict(os.environ,{'PYTHONPATH':str(external)}):
            runtime_env=extensions._runtime_env(record)
            self.assertNotIn('PYTHONPATH',runtime_env)
            process=subprocess.run(extensions._target_argv(record),cwd=spec['cwd'],
                env=runtime_env,capture_output=True,check=False)
        self.assertNotEqual(process.returncode,0)
        self.assertIn(b'ModuleNotFoundError',process.stderr)

    def test_launcher_checks_every_restart_and_preserves_exit_code(self):
        extensions.stage(self.manifest());extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        record=extensions.status('sample-mcp');record['status']='active_reload_required'
        (self.codex/'apzn/extensions/sample-mcp/record.json').write_text(json.dumps(record),encoding='utf-8')
        process=type('Process',(),{'wait':lambda self:7})()
        with patch('apzn.extensions.subprocess.Popen',return_value=process) as popen:
            self.assertEqual(extensions.launch('sample-mcp'),7)
        self.assertFalse(popen.call_args.kwargs['shell']);self.assertIsNone(popen.call_args.kwargs['stdout'])

    def test_launcher_code_change_is_rejected_at_restart(self):
        extensions.stage(self.manifest());extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        record=extensions.status('sample-mcp');record['status']='active_reload_required'
        (self.codex/'apzn/extensions/sample-mcp/record.json').write_text(json.dumps(record),encoding='utf-8')
        with patch('apzn.native_install.release_identity',return_value={'code_digest':'0'*64,'native_schema_version':2,'build_id':'changed'}):
            with self.assertRaises(ValueError):extensions.launch('sample-mcp')

    def test_disabled_server_only_probe_can_launch(self):
        extensions.stage(self.manifest());extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        with self.assertRaises(ValueError):extensions.launch('sample-mcp')
        with patch('apzn.extensions.subprocess.Popen') as popen:
            popen.return_value.wait.return_value=0;self.assertEqual(extensions.launch('sample-mcp',probe_mode=True),0)

    def test_module_file_changed_with_same_lock_rejected(self):
        package=self.root/'pkg';package.mkdir();module=package/'sample.py';module.write_text('x=1')
        lock=self.root/'requirements.lock';lock.write_text('sample==1')
        manifest={'id':'module-mcp','kind':'mcp-stdio','source':'local-test@1 MIT','env_vars':[],
                  'execution':{'kind':'module','command':str(Path(sys.executable).resolve()),'module':'sample',
                               'package_root':str(package),'installed_files':[str(module)],'lock_file':str(lock),
                               'cwd':str(package),'args':[]}}
        extensions.stage(manifest);extensions.apply('module-mcp',reviewed=True,codex_home=self.codex);module.write_text('x=2')
        with self.assertRaises(ValueError):extensions.launch('module-mcp',probe_mode=True)

    def test_rollback_preserves_unrelated_config_edit(self):
        extensions.stage(self.manifest());extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        with (self.codex/'config.toml').open('a',encoding='utf-8') as f:f.write('\n[user]\ntheme="dark"\n')
        extensions.rollback('sample-mcp');text=(self.codex/'config.toml').read_text(encoding='utf-8')
        self.assertIn('theme',text);self.assertNotIn('sample-mcp',text)

    def test_reviewed_read_only_call_enables_only_after_result(self):
        manifest=self.manifest()
        manifest['probe_call']={'tool':'ping','arguments':{},'read_only':True,
                                'purpose':'Harmless local status check'}
        extensions.stage(manifest);extensions.apply('sample-mcp',reviewed=True,codex_home=self.codex)
        call={'tool':'ping','request_sha256':digest({}),'result_sha256':'a'*64,
              'read_only_hint_observed':True,'status':'succeeded'}
        with (patch('apzn.extensions.importlib.util.find_spec',return_value=object()),
              patch('apzn.extensions._probe_stdio',new=AsyncMock(return_value={
                  'server':{'name':'fixture'},'tools':['ping'],'tool_call':call}))):
            result=extensions.probe('sample-mcp')
        self.assertEqual(result['status'],'active_reload_required')
        self.assertTrue(result['entry']['enabled'])
        self.assertTrue(result['tool_call_verified'])


if __name__=='__main__':unittest.main()
