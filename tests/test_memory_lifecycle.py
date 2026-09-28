from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from adhd.memory import Memory, namespace
from adhd.wiki import import_zip


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.ws=self.base/'workspace';self.ws.mkdir()
        self.env=patch.dict(os.environ,{'ADHD_HOME':str(self.base/'state'),'CODEX_HOME':str(self.base/'codex')})
        self.env.start();self.db=self.base/'memory.sqlite3';self.memory=Memory(self.db);self.scope=namespace(self.ws)

    def tearDown(self):
        self.memory.close();self.env.stop();self.tmp.cleanup()

    def record(self, **overrides):
        data={'scope':self.scope,'kind':'document','key':'wiki:stable','content':'same source excerpt',
              'source':'archive.zip!/old.md','locator':'lines 1..2','basis':'source_read',
              'ttl_days':10,'content_identity':'content-digest','source_alias':'archive.zip!/old.md'}
        data.update(overrides);return data

    def test_rename_does_not_bypass_forget(self):
        first=self.memory.put(**self.record());self.memory.forget(first['id'])
        again=self.memory.put(**self.record(source='renamed.zip!/new.md',source_alias='renamed.zip!/new.md'))
        self.assertEqual(again['status'],'forgotten');self.assertEqual(again['id'],first['id'])

    def test_quarantine_applies_to_every_alias(self):
        first=self.memory.put(**self.record())
        with self.memory.db:
            self.memory.add_alias('legacy-recipe','old-id',first['id'])
        self.memory.quarantine(['old-id'])
        self.assertEqual(self.memory.recall('source excerpt',self.scope)['records'],[])
        again=self.memory.put(**self.record(source_alias='new-name'))
        self.assertEqual(again['status'],'quarantined')

    def test_replayed_observation_does_not_renew_ttl(self):
        now=time.time();args=self.record(observed=now-100,ttl_days=1)
        first=self.memory.observe(observation_id='obs-1',evidence_digest='evidence-a',
            source_revision='revision-a',**args)
        expiry=self.memory.db.execute('SELECT expires FROM records WHERE id=?',(first['id'],)).fetchone()['expires']
        replay=self.memory.observe(observation_id='obs-2',evidence_digest='evidence-a',
            source_revision='revision-a',**self.record(observed=now,ttl_days=30,source_alias='renamed'))
        current=self.memory.db.execute('SELECT expires FROM records WHERE id=?',(first['id'],)).fetchone()['expires']
        self.assertEqual(replay['observation'],'duplicate');self.assertEqual(current,expiry)

    def test_new_verified_evidence_can_renew_ttl(self):
        now=time.time();first=self.memory.observe(observation_id='obs-1',evidence_digest='a',
            source_revision='rev-1',**self.record(observed=now-100,ttl_days=1))
        old=self.memory.db.execute('SELECT expires FROM records WHERE id=?',(first['id'],)).fetchone()['expires']
        second=self.memory.observe(observation_id='obs-2',evidence_digest='b',source_revision='rev-2',
            **self.record(observed=now,ttl_days=30))
        new=self.memory.db.execute('SELECT expires FROM records WHERE id=?',(first['id'],)).fetchone()['expires']
        self.assertEqual(second['observation'],'recorded');self.assertGreater(new,old)

    def test_observation_id_cannot_be_rebound(self):
        self.memory.observe(observation_id='obs-shared',evidence_digest='a',source_revision='rev-a',**self.record())
        with self.assertRaises(ValueError):
            self.memory.observe(observation_id='obs-shared',evidence_digest='b',source_revision='rev-b',
                                **self.record(content='different content',content_identity='other-content'))

    def test_scope_keeps_same_content_separate(self):
        other=self.base/'other';other.mkdir()
        a=self.memory.put(**self.record())
        b=self.memory.put(**self.record(scope=namespace(other)))
        self.assertNotEqual(a['id'],b['id'])

    def test_renamed_wiki_member_cannot_resurrect_forgotten_content(self):
        def archive(path: Path, member: str):
            with zipfile.ZipFile(path,'w') as z:z.writestr('root/20 지식/'+member,'same wiki text')
        first_zip=self.base/'first.zip';second_zip=self.base/'second.zip'
        archive(first_zip,'old.md');archive(second_zip,'renamed.md')
        first=import_zip(first_zip,self.ws,db=self.db)['records'][0]
        self.memory.forget(first['id'])
        second=import_zip(second_zip,self.ws,db=self.db)['records'][0]
        self.assertEqual(second['status'],'forgotten');self.assertEqual(second['id'],first['id'])


if __name__=='__main__': unittest.main()
