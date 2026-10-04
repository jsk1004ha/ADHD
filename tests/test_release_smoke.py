"""Run the distribution's CLI and installed hooks in a fresh offline venv."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from adhd.core import ROOT, digest
from build import package_adhd


class ReleaseSmokeTests(unittest.TestCase):
    def test_zip_cli_and_registered_hooks_in_fresh_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / 'release.zip'
            with patch.object(package_adhd, 'OUTPUT', archive_path):
                package_adhd.main()
            extracted = root / 'extracted'
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIsNone(archive.testzip())
                manifest = archive.read(package_adhd.ARCHIVE_ROOT + '/SHA256SUMS.txt').decode('utf-8')
                for line in manifest.splitlines():
                    sha, name = line.split('  ', 1)
                    self.assertEqual(hashlib.sha256(archive.read(
                        package_adhd.ARCHIVE_ROOT + '/' + name)).hexdigest(), sha, name)
                archive.extractall(extracted)
            source = extracted / package_adhd.ARCHIVE_ROOT
            venv = root / 'venv'
            created = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)],
                capture_output=True, timeout=90, check=False)
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors='replace'))
            python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            codex = root / 'clean codex'
            agents = root / 'clean agents'
            workspace = root / 'workspace'
            codex.mkdir(); workspace.mkdir()
            environment = dict(os.environ)
            for name in list(environment):
                if name.upper().startswith('PYTHON') or name.startswith(('ADHD_', 'OMX_')):
                    environment.pop(name)
            environment.update(CODEX_HOME=str(codex), ADHD_HOME=str(codex / 'adhd'),
                ADHD_EXEC_OWNER='', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                PATH=str(python.parent))

            def run(argv, input=None):
                process = subprocess.run(argv, input=input, cwd=workspace, env=environment,
                    capture_output=True, timeout=120, check=False)
                self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
                return process.stdout.decode('utf-8')

            def cli(*args):
                return json.loads(run([str(python), str(source / 'adhd.py'), *args]))

            self.assertIn('native', run([str(python), str(source / 'adhd.py'), '--help']))
            installed = cli('install', '--codex-home', str(codex), '--agents-home', str(agents))
            release = Path(installed['release'])
            self.assertTrue(release.is_relative_to(codex / 'adhd' / 'releases'))
            report = cli('doctor', '--codex-home', str(codex))
            self.assertTrue(report['native_installation'])
            self.assertEqual(report['installation_drift'], [])
            hooks = json.loads((codex / 'hooks.json').read_text(encoding='utf-8'))['hooks']
            commands = [hook['commandWindows' if os.name == 'nt' else 'command']
                for groups in hooks.values() for group in groups for hook in group['hooks']]
            self.assertTrue(commands)
            self.assertTrue(all(str(release / 'hook.py') in command for command in commands))
            self.assertTrue(all(str(ROOT / 'hook.py') not in command for command in commands))
            origin = json.loads(run([str(python), '-I', '-c',
                'import sys,json;sys.path.insert(0,sys.argv[1]);'
                'import adhd;print(json.dumps(adhd.__file__))', str(release)]))
            self.assertTrue(Path(origin).is_relative_to(release))

            session_id = 'release-smoke'
            key = digest(session_id)[:24]
            turn = 0
            def hook(name, **fields):
                nonlocal turn
                turn += 1
                command = hooks[name][-1]['hooks'][0]['commandWindows' if os.name == 'nt' else 'command']
                argv = command if os.name == 'nt' else shlex.split(command)
                result = json.loads(run(argv, json.dumps({'hook_event_name': name,
                    'session_id': session_id, 'cwd': str(workspace), 'turn_id': str(turn),
                    **fields}).encode('utf-8')))
                self.assertNotIn('systemMessage', result, result)
                return result

            hook('SessionStart')
            hook('UserPromptSubmit', prompt='Write result.txt in this smoke fixture')
            payload = workspace / 'begin.json'
            payload.write_text(json.dumps({'mode': 'coding',
                'criteria': [{'id': 'R1', 'text': 'Write result', 'kind': 'artifact'}],
                'artifacts': ['result.txt'], 'plan': {
                    'objective': 'Write result', 'approach': 'Write a local file',
                    'alternatives': ['Use direct file write'], 'risks': ['Unverified output'],
                    'preflight': ['Inspect isolated workspace'], 'verification': 'Inspect result',
                    'steps': [{'id': 'S1', 'action': 'Write and verify',
                               'depends_on': [], 'requirements': ['R1']}]}}), encoding='utf-8')
            queued = cli('native', 'begin', '--session', key, '--workspace', str(workspace),
                         '--payload-file', str(payload))
            hook('PostToolUse', tool_name='Bash')
            self.assertTrue(json.loads(Path(queued['receipt']).read_text())['ok'])
            view_path = workspace / '.adhd' / 'bridge' / key / 'view.json'
            self.assertEqual(json.loads(view_path.read_text())['status'], 'working')
            hook('PreToolUse', tool_name='spawn_agent', tool_use_id='writer', tool_input={
                'agent_type': 'adhd-implementer', 'message': 'Write the fixture result'})
            hook('SubagentStart', agent_type='adhd-implementer', agent_id='writer',
                 tool_use_id='writer', model='gpt-6-sol')
            cli('native', 'pause', '--session', key, '--workspace', str(workspace))
            hook('PostToolUse', tool_name='Bash')
            self.assertEqual(json.loads(view_path.read_text())['status'], 'interrupt_pending')
            hook('SubagentStop', agent_id='writer', agent_type='adhd-implementer',
                 last_assistant_message='Fixture finished')
            self.assertEqual(json.loads(view_path.read_text())['status'], 'paused')
            hook('SessionEnd')
            rollback = cli('rollback-native', '--codex-home', str(codex))
            self.assertGreater(rollback['restored_files'], 0)


if __name__ == '__main__':
    unittest.main()
