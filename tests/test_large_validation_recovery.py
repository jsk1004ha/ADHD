import copy
import unittest
import tests.test_large_tasks as fixtures


class ValidationInputPartitionTests(unittest.TestCase):
    def setUp(self):fixtures.LargeTaskTests.setUp(self)
    def spec(self,**kwargs):return fixtures.LargeTaskTests.spec(self,**kwargs)
    def store(self):return fixtures.LargeTaskTests.store(self)
    def plan(self,count=501):
        return {'requirements':['R1','R2'],'mandatory_checks':['gate'],
            'checks':[{'id':'gate','argv':['python','-m','unittest'],'subject_paths':['input-'+str(i) for i in range(count)],
                'requirements':['R1','R2'],'critical':True},
                {'id':'after','argv':['python','-c','print(1)'],'subject_paths':['small'],
                 'depends_on':['gate'],'requirements':['R1'],'critical':True}]}

    def test_repair_repeats_command_covers_every_input_and_preserves_dependencies(self):
        store=self.store();plan=self.plan(1001);store.declare_validation_plan(plan)
        before=store.status();result=store.partition_invalid_validation_inputs()
        gates=[c for c in result['checks'] if c['id'].startswith('gate')]
        self.assertEqual([n for c in gates for n in c['subject_paths']],plan['checks'][0]['subject_paths'])
        self.assertTrue(all(len(c['subject_paths'])<=500 and c['argv']==plan['checks'][0]['argv'] and c['critical'] for c in gates))
        self.assertEqual(set(result['mandatory_checks']),{c['id'] for c in gates})
        self.assertEqual(result['checks'][-1]['depends_on'],[c['id'] for c in gates])
        after=store.status()
        for field in ['spec','tasks','policy_ceiling','repair_wave_count','dispatch_count','created_at']:
            self.assertEqual(before[field],after[field])
        with self.assertRaisesRegex(ValueError,'remain locked'):store.partition_invalid_validation_inputs()
        self.assertEqual(store.events()[-1]['kind'],'invalid_validation_inputs_partitioned')

    def test_valid_declaration_cannot_be_weakened_or_unlocked(self):
        store=self.store();store.declare_validation_plan(self.plan(500))
        before=copy.deepcopy(store.status()['validation_plan'])
        with self.assertRaisesRegex(ValueError,'remain locked'):store.partition_invalid_validation_inputs()
        self.assertEqual(store.status()['validation_plan'],before)


if __name__=='__main__':unittest.main()
