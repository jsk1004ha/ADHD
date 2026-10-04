from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from adhd import builtin, extensions
from adhd.core import atomic_json, digest


CALL = {'tool': 'get_status', 'arguments': {}, 'read_only': True,
        'purpose': 'Verify the connection using a minimal read'}


class BuiltinReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.codex = self.root / 'codex'
        (self.codex / 'adhd').mkdir(parents=True)
        self.recipe = {'key': 'fixture', 'url': 'https://fixture.invalid/mcp',
                       'oauth_required': True}
        self.catalog = patch('adhd.builtin.mcp_catalog',
                             side_effect=lambda *args: [{'id': 'M1', 'builtin': self.recipe}])
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        self.env = patch.dict(os.environ, {'FIXTURE_OAUTH': 'test-credential'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = self.codex / 'config.toml'
        self.config.write_text('# preserve\nmodel="chosen-model"\n'
            '[mcp_servers.fixture]\nurl="https://fixture.invalid/mcp"\nenabled=false\n',
            encoding='utf-8')
        entry = {'url': self.recipe['url'], 'enabled': False}
        atomic_json(self.codex / 'adhd' / 'builtin-installation.json', {'files': [{
            'path': str(self.config), 'selector': {'kind': 'toml_mcp_entries',
            'entries': [{'key': 'fixture', 'digest': builtin._digest(entry)}]}}]})

    def result(self, *, read=True):
        return {'server': {'name': 'fixture', 'version': '1'}, 'tools': ['get_status'],
                'connected': True, 'tool_call': {
                    'tool': CALL['tool'], 'request_sha256': digest(CALL['arguments']),
                    'result_sha256': 'a' * 64, 'read_only_hint_observed': True,
                    'status': 'succeeded', 'observed_at': time.time()} if read else None}

    def probe(self, result=None, **kwargs):
        with patch('adhd.builtin._sdk_available', return_value=True), patch(
                'adhd.builtin._probe_builtin', new=AsyncMock(
                    return_value=self.result() if result is None else result)):
            return builtin.probe_builtin(self.codex, 'fixture', probe_call=CALL,
                consent=True, oauth_token_env='FIXTURE_OAUTH', **kwargs)

    def test_no_consent_never_connects(self):
        before = self.config.read_bytes()
        with patch('adhd.builtin._probe_builtin', new=AsyncMock()) as connect:
            with self.assertRaisesRegex(ValueError, 'consent'):
                builtin.probe_builtin(self.codex, 'fixture', probe_call=CALL)
        connect.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_success_records_separate_stages_and_enables_oauth(self):
        result = self.probe()
        self.assertEqual(result['stage'], 'read_verified')
        self.assertTrue(all(result['stages'].values()))
        self.assertTrue(result['usable'])
        self.assertNotIn('test-credential', json.dumps(result))
        self.assertIn('enabled=false', self.config.read_text())
        enabled = builtin.enable_builtin(self.codex, 'fixture')
        self.assertEqual(enabled['status'], 'enabled')
        self.assertIn('model="chosen-model"', self.config.read_text())
        self.assertIn('# preserve', self.config.read_text())
        self.assertIn('bearer_token_env_var', self.config.read_text())
        self.assertNotIn('test-credential', self.config.read_text())
        with patch('adhd.builtin.skill_manifest', return_value={'skills': []}):
            status = builtin.builtin_status(self.codex)['mcp'][0]
        self.assertTrue(status['usable'])
        self.assertEqual(status['stage'], 'read_verified')

    def test_handshake_alone_cannot_enable(self):
        result = self.probe(self.result(read=False))
        self.assertEqual(result['stage'], 'connected')
        self.assertFalse(result['usable'])
        with self.assertRaisesRegex(ValueError, 'read'):
            builtin.enable_builtin(self.codex, 'fixture')

    def test_expired_probe_cannot_enable(self):
        self.probe()
        with patch('adhd.builtin.time.time', return_value=time.time() + 3601):
            with self.assertRaises(ValueError):
                builtin.enable_builtin(self.codex, 'fixture')

    def test_changed_auth_recipe_or_entry_invalidates_probe(self):
        self.probe()
        with patch.dict(os.environ, {'FIXTURE_OAUTH': 'rotated'}):
            with self.assertRaises(ValueError):
                builtin.enable_builtin(self.codex, 'fixture')
        self.recipe['source'] = 'changed recipe'
        with self.assertRaises(ValueError):
            builtin.enable_builtin(self.codex, 'fixture')
        del self.recipe['source']
        self.config.write_text(self.config.read_text().replace('fixture.invalid', 'changed.invalid'))
        with self.assertRaises(ValueError):
            builtin.enable_builtin(self.codex, 'fixture')

    def test_engine_integration_requires_successful_read(self):
        self.recipe.clear()
        self.recipe.update(key='fixture', command=str(Path(sys.executable).resolve()),
                           args=['server.py'], requires_program='unity',
                           engine_integration_required=True)
        entry = {'command': self.recipe['command'], 'args': ['server.py'], 'enabled': False}
        self.config.write_text('model="chosen-model"\n[mcp_servers.fixture]\n'
            f'command={json.dumps(entry["command"])}\nargs=["server.py"]\nenabled=false\n')
        record = {'files': [{'path': str(self.config), 'selector': {
            'kind': 'toml_mcp_entries', 'entries': [{'key': 'fixture',
            'digest': builtin._digest(entry)}]}}]}
        atomic_json(self.codex / 'adhd' / 'builtin-installation.json', record)
        with patch('adhd.builtin._program_available', return_value=True):
            pending = self.probe(self.result(read=False))
            self.assertFalse(pending['stages']['auth_integration_verified'])
            done = self.probe()
            self.assertTrue(done['stages']['auth_integration_verified'])
            self.assertEqual(builtin.enable_builtin(self.codex, 'fixture')['status'], 'enabled')

    def test_failed_or_unrelated_read_cannot_enable(self):
        bad = self.result()
        bad['tool_call']['tool'] = 'get_unrelated'
        with self.assertRaises(ValueError):
            self.probe(bad)
        with self.assertRaises(ValueError):
            builtin.enable_builtin(self.codex, 'fixture')

    def test_probe_binds_nonempty_arguments_using_shared_digest(self):
        call = {**CALL, 'arguments': {'scope': 'minimal'}}
        result = self.result()
        result['tool_call']['request_sha256'] = digest(call['arguments'])
        with patch('adhd.builtin._sdk_available', return_value=True), patch(
                'adhd.builtin._probe_builtin', new=AsyncMock(return_value=result)):
            status = builtin.probe_builtin(self.codex, 'fixture', probe_call=call,
                consent=True, oauth_token_env='FIXTURE_OAUTH')
        self.assertTrue(status['usable'])

    def test_failed_reprobe_revokes_old_success(self):
        self.probe()
        with patch('adhd.builtin._sdk_available', return_value=True), patch(
                'adhd.builtin._probe_builtin', new=AsyncMock(side_effect=RuntimeError('failure'))):
            result = builtin.probe_builtin(self.codex, 'fixture', probe_call=CALL,
                consent=True, oauth_token_env='FIXTURE_OAUTH')
        self.assertFalse(result['usable'])
        with self.assertRaises(ValueError):
            builtin.enable_builtin(self.codex, 'fixture')

    def test_no_sdk_preserves_config_and_reports_dependency(self):
        before = self.config.read_bytes()
        with patch('adhd.builtin._sdk_available', return_value=False):
            result = builtin.probe_builtin(self.codex, 'fixture', probe_call=CALL,
                consent=True, oauth_token_env='FIXTURE_OAUTH')
        self.assertEqual(result['status'], 'sdk_missing')
        self.assertFalse(result['usable'])
        self.assertEqual(self.config.read_bytes(), before)


class SharedProbeSessionTests(unittest.TestCase):
    def session(self, *, read_only=True, error=False, content=None, modern=False):
        name = 'read_only_hint' if modern else 'readOnlyHint'
        error_name = 'is_error' if modern else 'isError'
        info_name = 'server_info' if modern else 'serverInfo'
        serialized = {'content': [{'type': 'text', 'text': 'ok'}] if content is None else content}
        response = SimpleNamespace(**{error_name: error},
            model_dump=lambda **kwargs: serialized)
        return SimpleNamespace(
            initialize=AsyncMock(return_value=SimpleNamespace(**{info_name: SimpleNamespace(
                model_dump=lambda **kwargs: {'name': 'fixture', 'version': '1'})})),
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(
                name='get_status', annotations=SimpleNamespace(**{name: read_only}))])),
            call_tool=AsyncMock(return_value=response))

    def test_observed_read_supports_sdk_attribute_versions(self):
        for modern in (False, True):
            with self.subTest(modern=modern):
                result = asyncio.run(extensions.probe_session(self.session(modern=modern), CALL))
                self.assertTrue(result['connected'])
                self.assertEqual(result['tool_call']['status'], 'succeeded')

    def test_missing_annotation_error_or_empty_result_is_rejected(self):
        for kwargs in ({'read_only': False}, {'error': True}, {'content': []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                asyncio.run(extensions.probe_session(self.session(**kwargs), CALL))

    def test_mutating_probe_is_rejected_before_initialize(self):
        session = self.session()
        with self.assertRaises(ValueError):
            asyncio.run(extensions.probe_session(session, {**CALL, 'tool': 'delete_data'}))
        session.initialize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
