from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adhd.change_journal import apply_toml_entry, recover, rollback
from adhd.core import atomic_json
import tomlkit


class ChangeJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=self.root/'config.toml';self.journal=self.root/'journal'
        self.config.write_text('# keep this comment\nmodel = "gpt-6-sol"\n\n[mcp_servers.other]\ncommand = "keep"\n',encoding='utf-8')

    def tearDown(self):self.temp.cleanup()

    def test_unrelated_user_edit_preserved(self):
        op=apply_toml_entry(self.config,['mcp_servers','sample'],{'command':'python','enabled':False},owner='extension:sample',journal_root=self.journal,require_absent=True)
        doc=tomlkit.parse(self.config.read_text(encoding='utf-8'));doc['user_preference']='later'
        self.config.write_text(tomlkit.dumps(doc),encoding='utf-8')
        rollback(op,journal_root=self.journal)
        text=self.config.read_text(encoding='utf-8');self.assertIn('user_preference',text);self.assertIn('other',text);self.assertNotIn('sample',text)

    def test_managed_entry_user_edit_conflicts(self):
        op=apply_toml_entry(self.config,['mcp_servers','sample'],{'command':'python','enabled':False},owner='extension:sample',journal_root=self.journal,require_absent=True)
        doc=tomlkit.parse(self.config.read_text(encoding='utf-8'));doc['mcp_servers']['sample']['command']='changed'
        self.config.write_text(tomlkit.dumps(doc),encoding='utf-8')
        with self.assertRaises(ValueError):rollback(op,journal_root=self.journal)
        self.assertIn('changed',self.config.read_text(encoding='utf-8'))

    def test_modified_journal_after_value_is_not_trusted(self):
        op=apply_toml_entry(self.config,['mcp_servers','sample'],{'command':'python'},owner='extension:sample',journal_root=self.journal,require_absent=True)
        op['after']['value_digest']='0'*64
        with self.assertRaises(ValueError):rollback(op,journal_root=self.journal)

    def test_interrupted_applied_change_is_conditionally_recovered(self):
        op=apply_toml_entry(self.config,['mcp_servers','sample'],{'command':'python'},owner='extension:sample',journal_root=self.journal,require_absent=True)
        op['phase']='applied';atomic_json(self.journal/f"{op['operation_id']}.json",op)
        self.assertEqual(recover(self.journal),[]);self.assertNotIn('sample',self.config.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
