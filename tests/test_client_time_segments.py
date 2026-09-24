from pathlib import Path
import os
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mc2p.runtime.segmented_trace import iter_segmented_jsonl

ROOT = Path(__file__).resolve().parents[1]


class ClientTimeSegmentTests(unittest.TestCase):
    def test_opt_in_lifecycle_only_seals_enabled_stream_and_stops_sampling(self):
        gson = next((ROOT/'.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1').glob('*/*.jar'))
        java = ROOT/'.venv/Library/lib/jvm/bin'
        sources = [ROOT/'mc2p/backends/runtime_overlays/mc121_diagnostics'/name for name in
                   ('ClientTimeTrace.java', 'ClientTimeDiagnostics.java', 'ClientTimeSegmentWriter.java',
                    'ClientMovementDiagnostics.java', 'ClientControlDiagnostics.java')]
        sources.insert(5, ROOT/'mc2p/backends/runtime_overlays/mc121_diagnostics/ClientPhysicsTickDiagnostics.java')
        sources += list((ROOT/'tests/java/diagnostics_stubs').rglob('*.java'))
        sources += [ROOT/'mc2p/backends/runtime_overlays/mc121_observation/ClientSampleClock.java']
        sources.append(ROOT/'tests/java/ClientTimeDiagnosticsLifecycleTest.java')
        with TemporaryDirectory(prefix='mc2p-diagnostic-lifecycle-') as temporary:
            root = Path(temporary)
            compiled = subprocess.run([str(java/'javac.exe'), '-J-Duser.language=en', '-encoding', 'UTF-8', '-cp', str(gson),
                '-d', str(root), *map(str, sources)], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
            for enabled, segmented in ((False, False), (True, False), (True, True), (False, True)):
                run = root/f'{enabled}-{segmented}'
                run.mkdir()
                env = dict(os.environ, MC2P_TIME_DIAGNOSTICS='1' if enabled else '0',
                           MC2P_TIME_SEGMENTED='1' if segmented else '0', MC2P_MOVEMENT_DIAGNOSTICS='0',
                           MC2P_CONTROL_DIAGNOSTICS='0')
                env['MC2P_PHYSICS_TICK_DIAGNOSTICS'] = '0'
                result = subprocess.run([str(java/'java.exe'), '-cp', str(root)+';'+str(gson),
                    'ClientTimeDiagnosticsLifecycleTest'], cwd=run, env=env,
                    capture_output=True, text=True, timeout=30)
                if segmented and not enabled:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(list(run.iterdir()), [])
                    continue
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                if not enabled:
                    self.assertEqual(list(run.iterdir()), [])
                elif segmented:
                    rows = list(iter_segmented_jsonl(run/'time-events'))
                    self.assertEqual([r['event'] for r in rows], ['observation', 'world_tick', 'observation'])
                    self.assertEqual([r['event_sequence'] for r in rows], [1, 2, 3])
                    self.assertFalse((run/'mc2p-client-time.jsonl').exists())
                else:
                    self.assertEqual(len((run/'mc2p-client-time.jsonl').read_text().splitlines()), 3)
                    self.assertFalse((run/'time-events').exists())

    def test_real_java_segments_are_readable_by_python_and_failures_are_unsealed(self):
        source = ROOT/'mc2p/backends/runtime_overlays/mc121_diagnostics/ClientTimeSegmentWriter.java'
        self.assertTrue(source.is_file(), 'shared Java segment writer missing')
        gson = next((ROOT/'.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1').glob('*/*.jar'))
        java = ROOT/'.venv/Library/lib/jvm/bin'
        with TemporaryDirectory(prefix='mc2p-java-segments-') as temporary:
            root = Path(temporary)
            compiled = subprocess.run([str(java/'javac.exe'), '-J-Duser.language=en', '-encoding', 'UTF-8', '-cp', str(gson),
                '-d', str(root), str(source), str(ROOT/'tests/java/ClientTimeSegmentWriterTest.java')],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
            result = subprocess.run([str(java/'java.exe'), '-cp', str(root)+';'+str(gson),
                'com.mc2p.diagnostics.ClientTimeSegmentWriterTest', str(root)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('CLIENT_TIME_SEGMENTS_OK', result.stdout)
            self.assertEqual(list(iter_segmented_jsonl(root/'good')),
                             [{'number': n, 'text': '����'} for n in range(1000)])
            raw = b''.join(p.read_bytes() for p in sorted((root/'good').glob('segment-*.jsonl')))
            expected = ''.join('{"number":'+str(n)+',"text":"����"}\n' for n in range(1000)).encode('utf-8')
            self.assertEqual(raw, expected)
            for name in ('half', 'seal'):
                with self.assertRaises(ValueError): list(iter_segmented_jsonl(root/name))


if __name__ == '__main__': unittest.main()
