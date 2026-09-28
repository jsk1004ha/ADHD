from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from adhd.memory import Memory


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.ws=self.base/'workspace';self.ws.mkdir();self.legacy=self.base/'memory.sqlite3'
        db=sqlite3.connect(self.legacy)
        db.execute('''CREATE TABLE recipes (id TEXT PRIMARY KEY,workspace TEXT,mode TEXT,goal TEXT,
          environment TEXT,steps TEXT,evidence TEXT,successes INTEGER,failures INTEGER,updated REAL)''')
        db.execute('INSERT INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?)',('legacy-id',str(self.ws),'coding',
          'fix parser','env-a',json.dumps(['run regression']), 'completion.json',2,0,1.0))
        db.commit();db.close();self.memory=Memory(self.base/'memory-v2.sqlite3')

    def tearDown(self): self.memory.close();self.tmp.cleanup()

    def test_backup_and_alias_migration_are_idempotent(self):
        first=self.memory.migrate_legacy_recipes(self.legacy)
        self.assertEqual(first['migrated'],1);self.assertTrue(Path(first['backup']).is_file())
        self.assertEqual(self.memory.procedure_recipes(self.ws,'coding','fix parser','env-a')[0]['steps'],['run regression'])
        canonical=self.memory._canonical('legacy-id');self.assertIsNotNone(canonical)
        second=self.memory.migrate_legacy_recipes(self.legacy)
        self.assertEqual(second['status'],'already_migrated')
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM records').fetchone()[0],1)

    def test_forget_by_legacy_id_blocks_canonical_recall(self):
        self.memory.migrate_legacy_recipes(self.legacy);self.memory.forget('legacy-id')
        self.assertEqual(self.memory.procedure_recipes(self.ws,'coding','fix parser','env-a'),[])

    def test_quarantined_legacy_recipe_stays_inactive(self):
        db=sqlite3.connect(self.legacy);db.execute("UPDATE recipes SET failures=1 WHERE id='legacy-id'");db.commit();db.close()
        self.memory.migrate_legacy_recipes(self.legacy)
        self.assertEqual(self.memory.procedure_recipes(self.ws,'coding','fix parser','env-a'),[])

    def test_backup_is_a_readable_sqlite_snapshot(self):
        result=self.memory.migrate_legacy_recipes(self.legacy)
        backup=sqlite3.connect(result['backup'])
        try: self.assertEqual(backup.execute('SELECT id FROM recipes').fetchone()[0],'legacy-id')
        finally: backup.close()


if __name__=='__main__': unittest.main()
