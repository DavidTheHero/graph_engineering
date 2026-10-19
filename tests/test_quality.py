from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

from graph_engineering.quality import parallel_checks
from graph_engineering.runtime import Store


class ParallelQualityTests(unittest.TestCase):
    def test_real_child_processes_fan_out_and_join_only_after_every_result(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);store=Store(root/'controller')
            try:
                store.create_run({'repo':str(root)},'quality1')
                flow=SimpleNamespace(run_id='quality1',store=store,cancelled=lambda:False)
                good=[sys.executable,'-c',"import time;time.sleep(.3);print('Ran 1 test in 0.3s');print('OK')"]
                bad=[sys.executable,'-c',"import time;time.sleep(.4);print('build failed');raise SystemExit(1)"]
                state={'config':{'test_command':good,'build_commands':[bad],'trusted_local':True,'command_timeout':5}}
                test,builds=parallel_checks(flow,state,root)
                self.assertTrue(test['passed']);self.assertFalse(builds[0]['passed'])
                kinds=[e['kind'] for e in store.events('quality1')]
                self.assertEqual(kinds.count('child_started'),2)
                join=store.events('quality1')[-1];self.assertEqual(join['kind'],'quality_join')
                self.assertTrue(join['data']['all_dependencies_completed']);self.assertFalse(join['data']['passed'])
            finally:store.close()
