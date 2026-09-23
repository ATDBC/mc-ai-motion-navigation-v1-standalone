"""Exclusive, sealed JSONL segments. Exhaust readers before accepting evidence.

Hashes detect corruption, not an adversary able to rewrite the entire evidence set.
Active/unsealed directories are never accepted as completed streams.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import threading
import time
from typing import Iterator

from mc2p.runtime.trace import trace_projection

MAX_RECORD_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
_SEGMENT_KEYS = {'schema_version', 'index', 'filename', 'byte_count', 'record_count',
                 'first_record_ordinal', 'last_record_ordinal', 'sha256'}
_COMPLETE_KEYS = {'schema_version', 'segment_count', 'record_count', 'byte_count', 'manifest_sha256'}


def _safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('evidence path is a symlink/reparse point')
    return path


def _positive(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(name + ' must be a positive integer')
    return value


def _encode(record: dict) -> bytes:
    if type(record) is not dict:
        raise TypeError('evidence records must be objects')
    return (json.dumps(trace_projection(record), ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _decode(line: bytes) -> dict:
    def invalid(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    def finite_float(value):
        parsed=float(value)
        if not math.isfinite(parsed): invalid(value)
        return parsed
    value = json.loads(line.decode('utf-8'), object_pairs_hook=_object, parse_constant=invalid,parse_float=finite_float)
    if type(value) is not dict:
        raise ValueError('evidence row is not an object')
    # JSON decoding already limits node types; validate overflow while parsing, without deep-copying every node.
    return value


def _lines(stream, maximum: int) -> Iterator[bytes]:
    while line := stream.readline(maximum + 1):
        if len(line) > maximum or not line.endswith(b'\n'):
            raise ValueError('oversized or incomplete evidence line')
        yield line


class SegmentedJsonlWriter:
    def __init__(self, directory: Path, *, max_segment_bytes: int = 16 * 1024 * 1024,
                 max_record_bytes: int = MAX_RECORD_BYTES) -> None:
        self.max_segment_bytes = _positive(max_segment_bytes, 'max_segment_bytes')
        self.max_record_bytes = min(_positive(max_record_bytes, 'max_record_bytes'), MAX_RECORD_BYTES)
        self.directory = _safe_path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self._stream = None
        self._manifest = None
        self._closed = False
        self.primary_failure: BaseException | None = None
        self.cleanup_failures: list[BaseException] = []
        self._index = self._total_records = self._total_bytes = 0
        self._segment_records = self._segment_bytes = 0
        self._segment_hash = hashlib.sha256()
        self._manifest_hash = hashlib.sha256()
        self._disk_checked = 0.0
        self._lock = threading.RLock()
        try:
            self._check_disk(force=True)
            self._manifest = (self.directory / 'manifest.jsonl').open('xb')
        except BaseException as exc:
            self._fail(exc)
            raise

    def _check_disk(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._disk_checked >= 1:
            if shutil.disk_usage(self.directory).free < MIN_FREE_BYTES:
                raise OSError('evidence_disk_low: less than 512 MiB free')
            self._disk_checked = now

    def _fail(self, exc: BaseException) -> None:
        if self.primary_failure is None:
            self.primary_failure = exc
        else:
            self.cleanup_failures.append(exc)
        self._closed = True
        for name in ('_stream', '_manifest'):
            stream = getattr(self, name)
            setattr(self, name, None)
            if stream is not None:
                try:
                    stream.close()
                except BaseException as cleanup:
                    self.cleanup_failures.append(cleanup)

    def write(self, record: dict) -> None:
        data = _encode(record)
        if len(data) > min(self.max_record_bytes, self.max_segment_bytes):
            raise ValueError('evidence record exceeds byte limit')
        with self._lock:
            if self._closed:
                raise ValueError('evidence writer is closed or failed')
            try:
                rotating = self._segment_bytes + len(data) > self.max_segment_bytes
                self._check_disk(force=rotating or self._stream is None)
                if rotating:
                    self._finish_segment()
                if self._stream is None:
                    if self._index > 99_999_999:
                        raise ValueError('evidence segment index exhausted')
                    self._stream = _safe_path(self.directory / f'segment-{self._index:08d}.jsonl').open('xb')
                if self._stream.write(data) != len(data):
                    raise OSError('short evidence write')
                self._stream.flush()
                self._segment_hash.update(data)
                self._segment_bytes += len(data)
                self._segment_records += 1
                self._total_records += 1
                self._total_bytes += len(data)
            except BaseException as exc:
                self._fail(exc)
                raise

    def _finish_segment(self) -> None:
        if self._stream is None:
            return
        self._stream.flush()
        self._stream.close()
        self._stream = None
        data = _encode({'schema_version': 'mc2p.segment.v1', 'index': self._index,
                        'filename': f'segment-{self._index:08d}.jsonl',
                        'byte_count': self._segment_bytes, 'record_count': self._segment_records,
                        'first_record_ordinal': self._total_records - self._segment_records,
                        'last_record_ordinal': self._total_records - 1,
                        'sha256': self._segment_hash.hexdigest()})
        if self._manifest.write(data) != len(data):
            raise OSError('short evidence manifest write')
        self._manifest.flush()
        self._manifest_hash.update(data)
        self._index += 1
        self._segment_records = self._segment_bytes = 0
        self._segment_hash = hashlib.sha256()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._finish_segment()
                self._manifest.flush()
                self._manifest.close()
                self._manifest = None
                data = _encode({'schema_version': 'mc2p.segment-complete.v1', 'segment_count': self._index,
                                'record_count': self._total_records, 'byte_count': self._total_bytes,
                                'manifest_sha256': self._manifest_hash.hexdigest()})
                temporary = _safe_path(self.directory / 'complete.pending')
                with temporary.open('xb') as stream:
                    if stream.write(data) != len(data):
                        raise OSError('short evidence completion write')
                    stream.flush()
                    os.fsync(stream.fileno())
                destination = _safe_path(self.directory / 'complete.json')
                if destination.exists():
                    raise FileExistsError(destination)
                temporary.rename(destination)
                self._closed = True
            except BaseException as exc:
                self._fail(exc)
                raise


def iter_segmented_jsonl(directory: Path) -> Iterator[dict]:
    """Validate the entire stream on exhaustion, retaining only one row at a time."""
    root = _safe_path(directory)
    marker = _safe_path(root / 'complete.json')
    if not marker.is_file():
        raise ValueError('evidence stream was not normally completed')
    with marker.open('rb') as stream:
        data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError('oversized completion marker')
    complete = _decode(data)
    if set(complete) != _COMPLETE_KEYS or complete['schema_version'] != 'mc2p.segment-complete.v1':
        raise ValueError('invalid completion schema')
    for key in ('segment_count', 'record_count', 'byte_count'):
        if type(complete[key]) is not int or complete[key] < 0:
            raise ValueError('invalid completion count')
    manifest_hash = hashlib.sha256()
    index = ordinal = total_bytes = 0
    with _safe_path(root / 'manifest.jsonl').open('rb') as manifest:
        for data in _lines(manifest, 8192):
            manifest_hash.update(data)
            row = _decode(data)
            if set(row) != _SEGMENT_KEYS or row['schema_version'] != 'mc2p.segment.v1':
                raise ValueError('invalid segment schema')
            for key in ('index', 'byte_count', 'record_count', 'first_record_ordinal', 'last_record_ordinal'):
                if type(row[key]) is not int or row[key] < 0:
                    raise ValueError('invalid segment count')
            if (row['index'] != index or row['filename'] != f'segment-{index:08d}.jsonl'
                    or row['first_record_ordinal'] != ordinal or row['record_count'] == 0):
                raise ValueError('segment order or ordinal mismatch')
            digest = hashlib.sha256()
            count = size = 0
            with _safe_path(root / row['filename']).open('rb') as segment:
                for line in _lines(segment, MAX_RECORD_BYTES):
                    digest.update(line)
                    count += 1
                    size += len(line)
                    yield _decode(line)
            if (size != row['byte_count'] or count != row['record_count']
                    or digest.hexdigest() != row['sha256'] or row['last_record_ordinal'] != ordinal + count - 1):
                raise ValueError('segment content, hash or ordinal mismatch')
            index += 1
            ordinal += count
            total_bytes += size
    if (index != complete['segment_count'] or ordinal != complete['record_count']
            or total_bytes != complete['byte_count'] or manifest_hash.hexdigest() != complete['manifest_sha256']):
        raise ValueError('stream completion totals or manifest hash mismatch')
    # Check unexpected files without retaining a list proportional to stream length.
    entries = 0
    with os.scandir(root) as children:
        for child in children:
            _safe_path(Path(child.path))
            name = child.name
            if name not in ('complete.json', 'manifest.jsonl'):
                if (len(name) != 22 or not name.startswith('segment-') or not name.endswith('.jsonl')
                        or not name[8:16].isascii() or not name[8:16].isdigit()
                        or int(name[8:16]) >= index):
                    raise ValueError('unexpected evidence file')
            if not child.is_file(follow_symlinks=False):
                raise ValueError('evidence entry is not a regular file')
            entries += 1
    if entries != index + 2:
        raise ValueError('evidence directory entry count mismatch')


class SegmentedTraceWriter:
    def __init__(self, directory: Path, **limits) -> None:
        self._writer = SegmentedJsonlWriter(directory, **limits)

    def write(self, record_type: str, payload: object) -> None:
        if not isinstance(record_type, str) or not record_type:
            raise ValueError('record_type must be non-empty')
        self._writer.write({'schema_version': 'mc2p.trace-record.v0', 'record_type': record_type,
                            'payload': trace_projection(payload)})

    def close(self) -> None:
        self._writer.close()
