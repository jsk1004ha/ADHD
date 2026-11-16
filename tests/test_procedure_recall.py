"""Real follow-up wording must not hide a relevant completed procedure."""
from pathlib import Path
import tempfile
import unittest

from adhd.memory import Memory


FIRST = ('Build and use a unittest log report CLI for actual ADHD logs. Support UTF-8 and '
         'Windows UTF-16 logs, deliver report JSON and Markdown with 197 tests from '
         'regression and packaging logs, and retain a reusable procedure. Reproduce and '
         'repair the UTF-16 compatibility failure as a controlled failure-path check.')
FOLLOWUP = ("Generate a new unittest log report using this project's completed work and learned "
            'procedure. Process inputs/core-utf16.log and inputs/repairs.log into '
            'reports/second.json and reports/second.md. Consult relevant project learning '
            'and document which previous failure influenced your next checks. Keep the existing tool unchanged.')


class ProcedureRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root / 'memory.sqlite3')
        self.addCleanup(self.memory.close)
        self.rid = self.memory.save_procedure(self.root, 'coding', FIRST,
            ['Run the unittest log report CLI'], 'completion.json', 'env-a')

    def test_changed_inputs_and_wording_recall_success(self):
        rows = self.memory.procedure_recipes(self.root, 'coding', FOLLOWUP, 'env-a')
        self.assertEqual([row['id'] for row in rows], [self.rid])

    def test_generic_work_words_do_not_recall_unrelated_procedure(self):
        self.assertEqual(self.memory.procedure_recipes(self.root, 'coding',
            'Build and use a new calendar for the project with appointments and reminders', 'env-a'), [])

    def test_project_environment_and_mode_stay_scoped(self):
        for workspace, mode, env in [(self.root / 'other', 'coding', 'env-a'),
                                     (self.root, 'coding', 'env-b'), (self.root, 'report', 'env-a')]:
            self.assertEqual(self.memory.procedure_recipes(workspace, mode, FIRST, env), [])


if __name__ == '__main__':
    unittest.main()
