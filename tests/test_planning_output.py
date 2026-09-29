import tempfile
import unittest
from pathlib import Path
from codex_workbench.planning_output import PlanningOutput

class PlanningOutputTest(unittest.TestCase):
    def test_default_off_and_exact_retrieval_after_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            output=PlanningOutput(Path(directory)/'cache',selector=lambda value:['logs'])
            value={'logs':['ordinary line']*40+[{'error':'failed: evidence missing'}],
                   'constraints':'不得扩大权限 '*500,'long_text':'x'*5000,'permission':{'allowed':False}}
            self.assertFalse(output.filter(value)['enabled'])
            self.assertFalse(output.root.exists())
            result=output.filter(value,enabled=True)
            self.assertEqual(value['constraints'],result['value']['constraints'])
            self.assertEqual(value['permission'],result['value']['permission'])
            self.assertIn({'error':'failed: evidence missing'},result['value']['logs'])
            self.assertEqual(value,output.read(result['source_id'])['value'])
            self.assertEqual(value['long_text'],output.read(result['source_id'],'/long_text')['value'])
            self.assertLess(result['retained_bytes'],result['raw_bytes'])
            with self.assertRaises(ValueError):output.read('../other')

    def test_selector_failure_keeps_errors_after_long_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            def unavailable(value):raise ValueError('unavailable')
            output=PlanningOutput(directory,selector=unavailable)
            result=output.filter({'logs':'ok\n'*1000+'ERROR invariant failed'},enabled=True)
            self.assertIn('ERROR invariant failed',result['value']['logs'])
            self.assertIn('/logs',result['omitted_pointers'])
