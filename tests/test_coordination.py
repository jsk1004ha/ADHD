from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd.coordination import Coordinator


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / 'project'
        self.ws.mkdir()
        self.env = patch.dict(os.environ, {'ADHD_HOME': str(self.root / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.co = Coordinator(self.ws)

    def card(self, name, writes=(), **extra):
        return {'task_id': name, 'contract_revision': 1, 'requirements': ['R1'],
                'read_paths': [], 'write_paths': list(writes), 'resources': [],
                'base_revision': 'base', **extra}

    def test_disjoint_isolated_writers_and_same_file_queue(self):
        for name, paths in [('a', ['a.py']), ('b', ['b.py']), ('c', ['a.py'])]:
            self.co.register(self.card(name, paths))
        a = self.co.acquire('a', isolated=True)
        b = self.co.acquire('b', isolated=True)
        self.assertEqual(a['status'], 'running')
        self.assertEqual(b['status'], 'running')
        self.assertEqual(self.co.acquire('c', isolated=True)['status'], 'waiting')
        self.co.finish('a', a['generation'])
        self.assertEqual(self.co.acquire('c', isolated=True)['status'], 'running')

    def test_same_checkout_arbitrary_writers_remain_serial(self):
        for name in ('a', 'b'):
            self.co.register(self.card(name, [name + '.py']))
        a = self.co.acquire('a')
        self.assertEqual(self.co.acquire('b')['status'], 'waiting')
        self.co.finish('a', a['generation'])
        self.assertEqual(self.co.acquire('b')['status'], 'running')

    def test_snapshot_readers_and_live_readers(self):
        self.co.register(self.card('writer', ['x']))
        self.co.acquire('writer', isolated=True)
        self.co.register(self.card('live', read_paths=['x']))
        self.co.register(self.card('snapshot', read_paths=['x'], snapshot_revision='verified-v1'))
        self.assertEqual(self.co.acquire('live', isolated=True)['status'], 'waiting')
        self.assertEqual(self.co.acquire('snapshot', isolated=True)['status'], 'running')

    def test_resources_are_all_or_nothing_and_generation_is_fenced(self):
        self.co.register(self.card('a', resources=['port:8080']))
        self.co.register(self.card('b', resources=['port:8080', 'db:fixture']))
        self.co.register(self.card('c', resources=['db:fixture']))
        a = self.co.acquire('a', isolated=True)
        self.assertEqual(self.co.acquire('b', isolated=True)['status'], 'waiting')
        self.assertEqual(self.co.acquire('c', isolated=True)['status'], 'running')
        with self.assertRaisesRegex(ValueError, 'generation'):
            self.co.finish('a', a['generation'] + 1)
        self.co.assert_owner('a', a['generation'])

    def test_repeated_conflict_has_at_most_two_messages_and_stale_reply_rejected(self):
        self.co.register(self.card('a', ['x']))
        self.co.register(self.card('b', ['x']))
        self.co.acquire('a', isolated=True)
        waiting = self.co.acquire('b', isolated=True)
        key = waiting['conflict_id']
        for message in ({'action': 'sequence', 'first': 'a'}, {'action': 'sequence', 'first': 'a'}):
            self.co.negotiate(key, message)
        for _ in range(100):
            self.assertEqual(self.co.acquire('b', isolated=True)['conflict_id'], key)
        with self.assertRaisesRegex(ValueError, 'two|2'):
            self.co.negotiate(key, {'action': 'sequence', 'first': 'a'})
        self.co.register(self.card('b', ['y'], contract_revision=2))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.co.negotiate(key, {'action': 'sequence', 'first': 'a'})

    def test_cycle_and_active_scope_change_are_rejected(self):
        self.co.register(self.card('a', ['x']))
        self.co.register(self.card('b', ['y'], depends_on=['a']))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.co.register(self.card('a', ['x'], contract_revision=2, depends_on=['b']))
        a = self.co.acquire('a', isolated=True)
        with self.assertRaisesRegex(ValueError, 'active'):
            self.co.register(self.card('a', ['z'], contract_revision=2))
        self.co.assert_owner('a', a['generation'])

    def test_expired_heartbeat_does_not_steal_owner(self):
        self.co.register(self.card('a', ['x']))
        self.co.register(self.card('b', ['x']))
        self.co.acquire('a', isolated=True)
        self.assertEqual(self.co.acquire('b', isolated=True)['status'], 'waiting')
        self.assertEqual(self.co.status()['tasks'][0]['status'], 'running')

    def test_card_limit_and_path_escape(self):
        for path in ('../escape', '/absolute', '.git/config', r'C:\\escape', 'a/../../b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.co.register(self.card('a', [path]))
        with self.assertRaisesRegex(ValueError, 'bounded|2048'):
            self.co.register(self.card('huge', resources=['x' * 3000]))

    def test_owned_command_observes_previous_writer_result(self):
        code = "from pathlib import Path;p=Path('shared');p.write_text((p.read_text() if p.exists() else '')+'X')"
        for name in ('first', 'second'):
            result = self.co.run(self.card(name, ['shared']), [sys.executable, '-B', '-c', code])
            self.assertEqual(result['status'], 'complete')
        self.assertEqual((self.ws/'shared').read_text(), 'XX')

    def test_cannot_release_a_live_attached_process(self):
        self.co.register(self.card('self', ['x']))
        row = self.co.acquire('self')
        self.co.attach_process('self', row['generation'], os.getpid())
        with self.assertRaisesRegex(ValueError, 'termination'):
            self.co.finish('self', row['generation'])

    def test_duplicate_acquire_has_no_owner_capability(self):
        self.co.register(self.card('a',['x']))
        token=self.co.acquire('a')
        duplicate=Coordinator(self.ws).acquire('a')
        self.assertEqual(duplicate['status'],'waiting')
        self.assertNotIn('owner',duplicate)
        with self.assertRaisesRegex(ValueError,'owner'):
            Coordinator(self.ws).finish('a',token['generation'])
        self.assertNotIn('owner_hash',self.co.status()['tasks'][0])
        self.co.finish('a',token['generation'])

    def test_arbitrary_command_always_takes_checkout_writer(self):
        self.co.register(self.card('a',resources=['checkout-writer:'+self.co.checkout_id]))
        self.co.acquire('a')
        result=Coordinator(self.ws).run(self.card('unscoped'),[sys.executable,'-c','pass'],wait_seconds=0)
        self.assertEqual(result['status'],'waiting')

    def test_shared_output_dependency_and_current_hash(self):
        self.co.register(self.card('a',['x']))
        token=self.co.acquire('a')
        output=self.ws/'x';output.write_text('verified')
        self.co.finish('a',token['generation'],outputs={'x':output})
        self.co.register(self.card('b',read_paths=['x'],depends_on=['a']))
        reader=self.co.acquire('b',isolated=True)
        self.assertEqual(self.co.inputs('b',reader['generation'])['x']['contract_revision'],1)
        output.write_text('changed')
        with self.assertRaisesRegex(ValueError,'bytes changed'):
            self.co.inputs('b',reader['generation'])

    def test_cross_process_reservation_has_single_winner(self):
        script = '''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from adhd.coordination import Coordinator
c=Coordinator(Path(sys.argv[1]));n=sys.argv[2]
c.register({'task_id':n,'contract_revision':1,'requirements':['R1'],'read_paths':[],'write_paths':['x'],'resources':[],'base_revision':'base'})
r=c.acquire(n,isolated=True);print(r['status'],flush=True)
'''
        procs = [subprocess.Popen([sys.executable, '-B', '-c', script, str(self.ws), name, str(Path(__file__).resolve().parents[1])],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for name in ('a', 'b')]
        outputs = [p.communicate(timeout=20) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], outputs)
        self.assertEqual(sorted(out.strip() for out, _ in outputs), ['running', 'waiting'])

    def test_completed_dependency_ancestors_remain_resolvable(self):
        for name,dependencies in [('a',[]),('b',['a']),('c',['b'])]:
            self.co.register(self.card(name,depends_on=dependencies))
            token=self.co.acquire(name,isolated=True)
            self.co.finish(name,token['generation'])
        self.assertEqual(self.co.register(self.card('d',depends_on=['c']))['status'],'queued')

    def test_failed_new_generation_does_not_admit_stale_output(self):
        self.co.register(self.card('a',['x']));token=self.co.acquire('a')
        output=self.ws/'x';output.write_text('old')
        self.co.finish('a',token['generation'],outputs={'x':output})
        self.co.register(self.card('a',['x'],contract_revision=2));token=self.co.acquire('a')
        self.co.finish('a',token['generation'],success=False)
        self.co.register(self.card('b',['x']))
        result=self.co.acquire('b')
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['waiting_for'],['a'])

    def test_deleted_output_is_inherited_without_resurrecting_a_file(self):
        self.co.register(self.card('a',['x']))
        token=self.co.acquire('a')
        self.co.finish('a',token['generation'],outputs={'x':None})
        self.co.register(self.card('b',['x']))
        token=self.co.acquire('b')
        self.assertTrue(self.co.inputs('b',token['generation'])['x']['deleted'])


if __name__ == '__main__':
    unittest.main()
