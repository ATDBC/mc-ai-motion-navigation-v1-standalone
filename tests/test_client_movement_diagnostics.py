import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from mc2p.runtime.segmented_trace import iter_segmented_jsonl

ROOT = Path(__file__).resolve().parents[1]


class ClientMovementDiagnosticsTests(unittest.TestCase):
    def test_opt_in_actual_flags_missing_player_clock_link_and_sealed_stream(self):
        source = ROOT/'mc2p/backends/runtime_overlays/mc121_diagnostics/ClientMovementDiagnostics.java'
        self.assertTrue(source.is_file(),'actual movement sidecar missing')
        java = ROOT/'.venv/Library/lib/jvm/bin'
        gson = next((ROOT/'.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1').glob('*/*.jar'))
        sources = [source,*(source.parent/name for name in ('ClientTimeDiagnostics.java','ClientTimeTrace.java',
                   'ClientTimeSegmentWriter.java','ClientControlDiagnostics.java','ClientPhysicsTickDiagnostics.java')),
                   ROOT/'mc2p/backends/runtime_overlays/mc121_observation/ClientSampleClock.java',
                   *(ROOT/'tests/java/diagnostics_stubs').rglob('*.java'),ROOT/'tests/java/ClientMovementDiagnosticsTest.java']
        with TemporaryDirectory(prefix='mc2p-movement-diagnostics-') as temporary:
            root = Path(temporary)
            result = subprocess.run([str(java/'javac.exe'),'-J-Duser.language=en','-encoding','UTF-8','-cp',str(gson),
                '-d',str(root),*map(str,sources)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            for timing,movement in ((False,False),(False,True),(True,False),(True,True)):
                run = root/f'{timing}-{movement}'; run.mkdir()
                environment = dict(os.environ,MC2P_TIME_DIAGNOSTICS='1' if timing else '0',
                                   MC2P_TIME_SEGMENTED='1' if timing else '0',MC2P_MOVEMENT_DIAGNOSTICS='1' if movement else '0',
                                   MC2P_CONTROL_DIAGNOSTICS='0')
                result = subprocess.run([str(java/'java.exe'),'-cp',str(root)+';'+str(gson),'ClientMovementDiagnosticsTest'],
                    cwd=run,env=environment,capture_output=True,text=True,timeout=30)
                if movement and not timing:
                    self.assertNotEqual(result.returncode,0)
                    self.assertEqual(list(run.iterdir()),[])
                    continue
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                if not movement:
                    self.assertFalse((run/'movement-events').exists())
                    continue
                rows = list(iter_segmented_jsonl(run/'movement-events'))
                timing_rows = list(iter_segmented_jsonl(run/'time-events'))
                self.assertEqual(len(rows),3)
                self.assertEqual([r['generation_id'] for r in rows],[0,1,2])
                self.assertEqual([r['event_sequence'] for r in rows],[1,2,3])
                self.assertFalse(rows[0]['actual_sprinting'])
                self.assertTrue(rows[1]['actual_sprinting'])
                self.assertTrue(rows[1]['actual_sneaking'])
                self.assertFalse(rows[1]['on_ground'])
                self.assertEqual(rows[1]['pose'],'crouching')
                self.assertEqual(rows[1]['velocity'],{'x':.2,'y':.42,'z':.1})
                self.assertFalse(rows[2]['available'])
                self.assertIsNone(rows[2]['actual_sprinting'])
                self.assertIsNone(rows[2]['velocity'])
                for row,event in zip(rows,timing_rows):
                    self.assertEqual(row['schema_version'],'mc2p.client-movement-event.v2')
                    self.assertEqual(row['clock_id'],'jvm-diagnostic-test')
                    self.assertLess(row['sampled_at_monotonic_ns'],10)
                    self.assertEqual((row['session_id'],row['world_id'],row['client_ticks'],row['time_event_sequence']),
                                     (event['session_id'],event['world_id'],event['client_ticks'],event['event_sequence']))
            run = root/'time-close-failed'; run.mkdir()
            environment = dict(os.environ,MC2P_TIME_DIAGNOSTICS='1',MC2P_TIME_SEGMENTED='1',
                               MC2P_MOVEMENT_DIAGNOSTICS='1',MC2P_CONTROL_DIAGNOSTICS='0')
            result = subprocess.run([str(java/'java.exe'),'-cp',str(root)+';'+str(gson),'ClientMovementDiagnosticsTest','time-close-fail'],
                cwd=run,env=environment,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse((run/'time-events/complete.json').exists())
            self.assertEqual(len(list(iter_segmented_jsonl(run/'movement-events'))),3)
