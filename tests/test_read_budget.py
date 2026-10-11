"""Smaller read set for the verifier and director, plus the opt-in helper effort override."""
from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

from adhd.core import ROOT, atomic_json, store
from adhd.models import MODELS, apply_role_effort, route, validate_role_effort
from adhd.native import DEFAULTS, bridge, load_policy
from tests import test_native as fixtures
from tests import test_native_goal

REFS = ROOT / 'skills' / 'adhd-native' / 'references'
CARD = REFS / 'verifier-card.md'
QUICKSTART = REFS / 'protocol-quickstart.md'
PROTOCOL = REFS / 'protocol.md'
VERIFIER_TOML = ROOT / 'native' / 'agents' / 'adhd-verifier.toml'


def card_example(index=0) -> dict:
    blocks = re.findall(r'```json\n(.+?)\n```', CARD.read_text(encoding='utf-8'), re.S)
    return json.loads(blocks[index])


class VerifierCardTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    event = fixtures.NativeTests.event
    state = fixtures.NativeTests.state
    save = fixtures.NativeTests.save
    request = fixtures.NativeTests.request
    candidate = fixtures.NativeTests.candidate
    spawn = fixtures.NativeTests.spawn
    begin = test_native_goal.NativeGoalTests.begin

    def test_card_example_is_a_verdict_the_real_hook_accepts(self):
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        self.spawn(aid='carded')
        state = self.state()
        verdict = card_example()
        replacements = {'EXACT_CANDIDATE_DIGEST': state['candidate']['digest'],
                        'EXACT_CONTRACT_HASH': state['contract_hash'],
                        'EXACT_CANDIDATE_RECEIPT': state['candidate']['criterion_results'][0]['evidence_ids'][0]}
        def bind(value):
            if isinstance(value, dict): return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                if value == ['ORIGINAL_USER_TURN_ID']: return [row['turn_id'] for row in state['prompts']]
                return [bind(item) for item in value]
            return replacements.get(value, value) if isinstance(value, str) else value
        verdict = bind(verdict)
        self.event('SubagentStop', agent_type='adhd-verifier', agent_id='carded',
                   last_assistant_message=json.dumps(verdict))
        self.assertEqual(self.state()['status'], 'complete')

    def test_card_covers_every_field_the_controller_demands(self):
        text = CARD.read_text(encoding='utf-8')
        for name in ('verdict', 'reviewed_digest', 'reviewed_contract_hash', 'reviewed_turn_ids',
                     'intent_alignment', 'criterion_results', 'findings', 'evidence_ids',
                     'observation_ids', 'evidence_review', 'target_covering_receipts',
                     'document_reviews', 'v12-gates.md', 'coding-discipline.md'):
            self.assertIn(name, text)
        self.assertLess(len(text.encode()), 4000)

    def test_verifier_profile_reads_the_card_not_the_whole_skill(self):
        profile = tomllib.loads(VERIFIER_TOML.read_text(encoding='utf-8'))
        instructions = profile['developer_instructions']
        self.assertIn('references/verifier-card.md', instructions)
        self.assertNotIn('adhd-native/SKILL.md', instructions)
        self.assertIn('Only when an artifact is an Office/PDF document', instructions)
        self.assertEqual(profile['model_reasoning_effort'], 'max')  # verifier effort is never lowered

    def test_subagent_start_points_at_the_card(self):
        self.begin()
        self.assertTrue(self.candidate()['ok'])
        tool_use_id = 'card-start'
        self.event('PreToolUse', tool_name='spawn_agent', tool_use_id=tool_use_id,
                   tool_input={'agent_type': 'adhd-verifier', 'message': 'Review the candidate.'})
        out = self.event('SubagentStart', agent_type='adhd-verifier', agent_id='pointer',
                         model=route('adhd-verifier')['model'], tool_use_id=tool_use_id)
        text = out['hookSpecificOutput']['additionalContext']
        self.assertIn('verifier-card.md', text)
        self.assertNotIn('adhd-native/SKILL.md', text)

    def test_stop_block_stops_asking_for_an_unchanged_view(self):
        self.begin()
        first = self.event('Stop')['reason']
        self.assertIn('Read ', first)
        self.assertIn('view.json', first)
        second = self.event('Stop')['reason']
        self.assertIn('do not reread view.json', second)
        state = self.state()
        state['feedback'] = 'New reviewer feedback'
        self.save(state)
        third = self.event('Stop')['reason']
        self.assertIn('view.json', third)
        self.assertNotIn('do not reread', third)

    def test_reject_example_keeps_all_binding_fields(self):
        self.begin(); self.assertTrue(self.candidate()['ok']); self.spawn(aid='rejected')
        state = self.state(); verdict = card_example(1)
        self.assertEqual(verdict['verdict'], 'reject')
        verdict['reviewed_digest'] = state['candidate']['digest']
        verdict['reviewed_contract_hash'] = state['contract_hash']
        verdict['reviewed_turn_ids'] = [row['turn_id'] for row in state['prompts']]
        verdict['criterion_results'][0]['evidence_ids'][0] = state['candidate']['criterion_results'][0]['evidence_ids'][0]
        self.event('SubagentStop', agent_type='adhd-verifier', agent_id='rejected', last_assistant_message=json.dumps(verdict))
        self.assertEqual(self.state()['status'], 'revising')
        self.assertEqual(len(self.state()['review_findings']), 1)

    def test_child_and_observation_changes_request_a_new_read(self):
        self.begin(); self.event('Stop'); self.event('Stop')
        state = self.state(); state['children']['reader'] = {'role': 'adhd-scout', 'status': 'running'}
        self.save(state)
        self.assertNotIn('do not reread', self.event('Stop')['reason'])
        state = self.state(); state['tool_observations'].append({'id': 'new-observation'})
        self.save(state)
        self.assertNotIn('do not reread', self.event('Stop')['reason'])


class QuickstartTests(unittest.TestCase):
    def test_every_section_it_points_to_exists(self):
        text = QUICKSTART.read_text(encoding='utf-8')
        protocol_headings = set(re.findall(r'(?m)^## .+$', PROTOCOL.read_text(encoding='utf-8')))
        gate_headings = set(re.findall(r'(?m)^## .+$', (REFS / 'v12-gates.md').read_text(encoding='utf-8')))
        pointers = re.findall(r'`(## [^`]+)`', text)
        self.assertGreaterEqual(len(pointers), 5)
        for heading in pointers:
            self.assertTrue(heading in protocol_headings or heading in gate_headings, heading)

    def test_it_names_every_transport_operation_and_stays_small(self):
        text = QUICKSTART.read_text(encoding='utf-8')
        for operation in ('begin', 'plan', 'checkpoint', 'candidate', 'sync-intent', 'pause', 'blocked', 'status'):
            self.assertIn('`' + operation + '`', text)
        self.assertIn('adhd-verifier', text)
        self.assertIn('Coding scope', text)
        self.assertLess(len(text.encode()), 5000)

    def test_skill_entrypoint_reads_the_quickstart_first(self):
        skill = (ROOT / 'skills' / 'adhd-native' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('references/protocol-quickstart.md', skill)
        self.assertNotIn('read `ADHD_ROOT/skills/adhd-native/references/protocol.md` and', skill)

    def test_the_quickstart_json_examples_are_valid_json(self):
        blocks = re.findall(r'```json\n(.+?)\n```', QUICKSTART.read_text(encoding='utf-8'), re.S)
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            json.loads(block)

    def test_all_cli_transport_choices_are_discoverable(self):
        import ast
        tree = ast.parse((ROOT / 'adhd' / 'cli.py').read_text(encoding='utf-8'))
        choices = next(ast.literal_eval(keyword.value) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'add_argument' and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'operation'
            for keyword in node.keywords if keyword.arg == 'choices')
        text = QUICKSTART.read_text(encoding='utf-8')
        for operation in choices: self.assertIn('`' + operation + '`', text)


class RoleEffortTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    event = fixtures.NativeTests.event
    state = fixtures.NativeTests.state
    request = fixtures.NativeTests.request
    begin = fixtures.NativeTests.begin

    def write_policy(self, data):
        atomic_json(store() / 'native-policy.json', data)

    def test_default_pins_are_unchanged(self):
        for role in ('adhd-scout', 'adhd-light', 'adhd-verifier', 'adhd-implementer', 'adhd-planner'):
            self.assertEqual(route(role)['effort'], 'max')
        self.assertEqual(route('adhd-architect')['effort'], 'low')
        self.assertEqual(MODELS['luna']['effort'], 'max')

    def test_opt_in_lowers_only_the_two_luna_helpers(self):
        self.write_policy({'role_effort': {'adhd-scout': 'low', 'adhd-light': 'low'}})
        self.assertEqual(route('adhd-scout', load_policy()['role_effort'])['effort'], 'low')
        self.assertEqual(route('adhd-light', load_policy()['role_effort'])['effort'], 'low')
        self.assertEqual(route('adhd-scout')['model'], MODELS['luna']['model'])
        self.assertEqual(route('adhd-verifier')['effort'], 'max')
        self.assertEqual(MODELS['luna']['effort'], 'max')

    def test_the_policy_file_is_validated_at_begin_time(self):
        for bad in ({'adhd-verifier': 'low'}, {'adhd-scout': 'medium'}, ['adhd-scout'], {'adhd-scout': []}, {'adhd-scout': {}}, {'adhd-scout': None}):
            self.write_policy({'role_effort': bad})
            with self.assertRaises(ValueError):
                load_policy('standard')
        self.write_policy({'role_effort': {'adhd-scout': 'low'}, 'max_rounds': 5})
        self.assertEqual(load_policy('standard')['max_rounds'], 5)
        self.assertEqual(load_policy('standard')['role_effort'], {'adhd-scout': 'low'})

    def test_a_broken_policy_never_changes_routing(self):
        self.write_policy({'role_effort': {'adhd-verifier': 'low'}})
        self.assertEqual(route('adhd-verifier')['effort'], 'max')
        self.assertEqual(route('adhd-scout')['effort'], 'max')

    def test_installed_agent_file_follows_the_policy(self):
        template = (ROOT / 'native' / 'agents' / 'adhd-scout.toml').read_text(encoding='utf-8')
        self.assertEqual(apply_role_effort('adhd-scout', template), template)
        self.write_policy({'role_effort': {'adhd-scout': 'low'}})
        rendered = apply_role_effort('adhd-scout', template, load_policy()['role_effort'])
        self.assertEqual(tomllib.loads(rendered)['model_reasoning_effort'], 'low')
        self.assertEqual(tomllib.loads(rendered)['model'], tomllib.loads(template)['model'])
        verifier = VERIFIER_TOML.read_text(encoding='utf-8')
        self.assertEqual(apply_role_effort('adhd-verifier', verifier), verifier)

    def test_run_freezes_effort_and_denies_hot_profile_drift(self):
        overrides = {'adhd-scout': 'low'}
        path = self.ch / 'agents' / 'adhd-scout.toml'; path.parent.mkdir(parents=True)
        source = (ROOT / 'native' / 'agents' / 'adhd-scout.toml').read_text(encoding='utf-8')
        path.write_text(apply_role_effort('adhd-scout', source, overrides), encoding='utf-8')
        self.write_policy({'role_effort': overrides}); self.event('SessionStart'); self.begin(kind='behavior')
        self.write_policy({'role_effort': {'adhd-scout': 'max'}})
        out = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='frozen',
            tool_input={'agent_type': 'adhd-scout', 'reasoning_effort': 'low'})
        self.assertNotEqual(out.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertEqual(self.state()['reservations']['frozen']['selected_effort'], 'low')
        self.event('SubagentStart', agent_type='adhd-scout', agent_id='frozen-reader',
                   tool_use_id='frozen', model=route('adhd-scout')['model'], reasoning_effort='low')
        self.assertEqual(self.state()['children']['frozen-reader']['configured_effort'], 'low')
        path.write_text(source, encoding='utf-8')
        out = self.event('PreToolUse', tool_name='spawn_agent', tool_use_id='drift',
            tool_input={'agent_type': 'adhd-scout', 'reasoning_effort': 'low'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_opt_in_without_install_is_rejected_at_begin(self):
        self.write_policy({'role_effort': {'adhd-scout': 'low'}})
        result = self.request('begin', {'mode': 'coding', 'criteria': [
            {'id': 'R1', 'text': 'Actual behavior', 'kind': 'behavior'}], 'artifacts': ['answer.txt']})
        self.assertFalse(result['ok']); self.assertIn('drift', result['message'])


if __name__ == '__main__':
    unittest.main()
