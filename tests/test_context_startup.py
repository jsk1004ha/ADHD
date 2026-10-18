from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd import core


class StartupTests(unittest.TestCase):
    def test_opted_in_cold_hook_defers_full_vault_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            ws=Path(temp)
            core.atomic_json(ws/'.adhd/context-options.json',{'schema_version':1,'wiki_startup_hints':True})
            with patch('adhd.obsidian.load_config',return_value={'enabled':True,'policy_epoch':'v1'}), \
                 patch('adhd.obsidian_index.hint_snapshot',return_value=(None,0)), \
                 patch('adhd.obsidian.context',side_effect=AssertionError('Full scan in hook')):
                result=core.prepare_wiki_context(ws,'original task')
            self.assertEqual(result['cards'],0)
            packet=core.read_json(ws/'.adhd/wiki-plan-context.json')
            self.assertFalse(packet['complete'])
            self.assertIn('wiki-reconciliation-required',packet['warnings'])
            self.assertTrue(packet['current_user_and_contract_override'])

    def test_existing_default_path_remains_explicit_full_context(self):
        with tempfile.TemporaryDirectory() as temp:
            ws=Path(temp)
            with patch('adhd.obsidian.load_config',return_value={'enabled':True}), \
                 patch('adhd.obsidian.context',return_value={'cards':[],'warnings':[],'policy_epoch':'v','index_generation':2}) as context:
                core.prepare_wiki_context(ws,'task')
                context.assert_called_once()


if __name__=='__main__':unittest.main()
