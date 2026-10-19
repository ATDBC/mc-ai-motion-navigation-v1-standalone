"""Segment completeness, bounded memory and failure preservation at the real file boundary."""
import importlib
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch


class SegmentedTraceTests(unittest.TestCase):
    def api(self):
        try:
            return importlib.import_module('mc2p.runtime.segmented_trace')
        except ModuleNotFoundError:
            self.fail('segmented evidence writer/reader is not implemented')

    def make(self, path, count=40):
        api = self.api()
        writer = api.SegmentedJsonlWriter(path, max_segment_bytes=128, max_record_bytes=96)
        for n in range(count):
            writer.write({'number': n, 'text': '汉字'})
        writer.close()
        return api

    def test_roundtrip_unicode_rotation_and_trace_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'records'
            api = self.make(path)
            self.assertEqual(list(api.iter_segmented_jsonl(path)),
                             [{'number': n, 'text': '汉字'} for n in range(40)])
            segments = list(path.glob('segment-*.jsonl'))
            self.assertGreater(len(segments), 3)
            self.assertTrue(all(p.stat().st_size <= 128 for p in segments))
            trace = api.SegmentedTraceWriter(Path(tmp) / 'trace')
            trace.write('example', {'items': (1, 2)})
            trace.close()
            self.assertEqual(list(api.iter_segmented_jsonl(Path(tmp) / 'trace')),
                             [{'schema_version': 'mc2p.trace-record.v0', 'record_type': 'example',
                               'payload': {'items': [1, 2]}}])

    def test_preprojected_trace_payload_is_not_walked_again(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace'
            writer = api.SegmentedTraceWriter(path)
            with patch.object(
                api, 'trace_projection',
                side_effect=AssertionError('projected payload walked again'),
            ):
                writer.write_projected('example', {'items': [1, 2]})
            writer.close()
            self.assertEqual(
                list(api.iter_segmented_jsonl(path)),
                [{'schema_version': 'mc2p.trace-record.v0',
                  'record_type': 'example', 'payload': {'items': [1, 2]}}],
            )

    def test_empty_close_is_complete_and_idempotent(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'empty'
            writer = api.SegmentedJsonlWriter(path)
            writer.close()
            writer.close()
            self.assertEqual(list(api.iter_segmented_jsonl(path)), [])
            with self.assertRaises(ValueError):
                writer.write({'late': True})

    def test_missing_extra_reordered_and_corrupt_segments_fail(self):
        for defect in ('missing', 'extra', 'swap', 'bytes', 'half_line', 'manifest', 'complete'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'records'
                api = self.make(path)
                first = path / 'segment-00000000.jsonl'
                second = path / 'segment-00000001.jsonl'
                if defect == 'missing':
                    first.unlink()
                elif defect == 'extra':
                    (path / 'segment-99999999.jsonl').write_bytes(first.read_bytes())
                elif defect == 'swap':
                    a, b = first.read_bytes(), second.read_bytes()
                    first.write_bytes(b)
                    second.write_bytes(a)
                elif defect == 'bytes':
                    first.write_bytes(first.read_bytes().replace(b'number', b'NUMber'))
                elif defect == 'half_line':
                    first.write_bytes(first.read_bytes()[:-1])
                elif defect == 'manifest':
                    with (path / 'manifest.jsonl').open('ab') as stream:
                        stream.write(b'{}\n')
                else:
                    (path / 'complete.json').write_text('{}', encoding='utf-8')
                with self.assertRaises((ValueError, OSError)):
                    list(api.iter_segmented_jsonl(path))

    def test_unclosed_and_invalid_records_cannot_be_accepted(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'records'
            writer = api.SegmentedJsonlWriter(path, max_segment_bytes=128, max_record_bytes=96)
            writer.write({'first': 1})
            with self.assertRaises(ValueError):
                list(api.iter_segmented_jsonl(path))
            for invalid in ({'large': 'x' * 100}, {'nan': float('nan')}, [1], {1: 'bad key'}):
                with self.assertRaises((ValueError, TypeError)):
                    writer.write(invalid)
            writer.close()
            self.assertEqual(list(api.iter_segmented_jsonl(path)), [{'first': 1}])

    def test_existing_directory_is_not_overwritten(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'records'
            self.make(path)
            before = (path / 'complete.json').read_bytes()
            with self.assertRaises(FileExistsError):
                api.SegmentedJsonlWriter(path)
            self.assertEqual((path / 'complete.json').read_bytes(), before)

    def test_low_disk_fails_without_complete_and_preserves_records(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'records'
            writer = api.SegmentedJsonlWriter(path, max_segment_bytes=32, max_record_bytes=32)
            writer.write({'n': 1})
            with patch.object(api.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 1})()):
                with self.assertRaisesRegex(OSError, 'evidence_disk_low'):
                    writer.write({'padding': 'x' * 15})
            writer.close()
            self.assertFalse((path / 'complete.json').exists())
            self.assertIn(b'"n":1', (path / 'segment-00000000.jsonl').read_bytes())

    def test_write_flush_and_close_failure_do_not_seal_complete(self):
        api = self.api()
        for operation in ('write', 'flush', 'close'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'records'
                writer = api.SegmentedJsonlWriter(path)
                writer.write({'n': 1})
                real = writer._stream
                primary = OSError('injected ' + operation)

                class FaultStream:
                    def write(self, data):
                        if operation == 'write':
                            real.write(data[:3])
                            raise primary
                        return real.write(data)

                    def flush(self):
                        if operation == 'flush':
                            raise primary
                        real.flush()

                    def close(self):
                        real.close()
                        if operation == 'close':
                            raise primary

                writer._stream = FaultStream()
                with self.assertRaises(OSError) as caught:
                    if operation == 'close':
                        writer.close()
                    else:
                        writer.write({'n': 2})
                self.assertIs(caught.exception, primary)
                writer.close()
                self.assertIs(writer.primary_failure, primary)
                self.assertFalse((path / 'complete.json').exists())

    def test_cleanup_error_does_not_replace_primary(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            writer = api.SegmentedJsonlWriter(Path(tmp) / 'records')
            writer.write({'n': 1})
            real = writer._stream
            primary, cleanup = OSError('write'), OSError('close')

            class FaultStream:
                def write(self, data):
                    raise primary

                def close(self):
                    real.close()
                    raise cleanup

            writer._stream = FaultStream()
            with self.assertRaises(OSError) as caught:
                writer.write({'n': 2})
            self.assertIs(caught.exception, primary)
            writer.close()
            self.assertIs(writer.primary_failure, primary)
            self.assertIn(cleanup, writer.cleanup_failures)

    def test_reader_memory_does_not_grow_with_record_count(self):
        api = self.api()
        peaks = []
        with tempfile.TemporaryDirectory() as tmp:
            for count in (1000, 10000):
                path = Path(tmp) / str(count)
                writer = api.SegmentedJsonlWriter(path, max_segment_bytes=1024)
                for n in range(count):
                    writer.write({'n': n})
                writer.close()
                tracemalloc.start()
                seen = sum(1 for _ in api.iter_segmented_jsonl(path))
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
                self.assertEqual(seen, count)
        self.assertLess(peaks[1] - peaks[0], 8 * 1024 * 1024)

    def test_disk_check_runs_without_rotation_after_one_second(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp, patch.object(api.time, 'monotonic', return_value=10) as clock:
            path = Path(tmp) / 'records'
            writer = api.SegmentedJsonlWriter(path)
            writer.write({'n': 1})
            clock.return_value = 11.01
            with patch.object(api.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 1})()):
                with self.assertRaisesRegex(OSError, 'evidence_disk_low'):
                    writer.write({'n': 2})
            writer.close()
            self.assertFalse((path / 'complete.json').exists())

    def test_manifest_failure_never_publishes_complete(self):
        api = self.api()
        for operation in ('write', 'flush', 'close'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'records'
                writer = api.SegmentedJsonlWriter(path)
                writer.write({'n': 1})
                real = writer._manifest
                failure = OSError('manifest ' + operation)

                class FaultManifest:
                    def write(self, data):
                        if operation == 'write':
                            raise failure
                        return real.write(data)

                    def flush(self):
                        if operation == 'flush':
                            raise failure
                        real.flush()

                    def close(self):
                        real.close()
                        if operation == 'close':
                            raise failure

                writer._manifest = FaultManifest()
                with self.assertRaises(OSError):
                    writer.close()
                self.assertIs(writer.primary_failure, failure)
                self.assertFalse((path / 'complete.json').exists())

    def test_symlink_root_and_segment_are_rejected(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / 'actual'
            self.make(actual)
            link = root / 'link'
            try:
                os.symlink(actual, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest('OS does not permit symlink fixture: ' + str(exc))
            with self.assertRaises(ValueError):
                list(api.iter_segmented_jsonl(link))
            with self.assertRaises(ValueError):
                api.SegmentedJsonlWriter(link / 'nested')
            segment = actual / 'segment-00000000.jsonl'
            stored = root / 'stored.jsonl'
            segment.rename(stored)
            os.symlink(stored, segment)
            with self.assertRaises(ValueError):
                list(api.iter_segmented_jsonl(actual))


if __name__ == '__main__':
    unittest.main()
