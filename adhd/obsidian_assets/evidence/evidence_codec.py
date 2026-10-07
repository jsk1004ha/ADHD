"""Bounded, hash-checked raw/gzip storage for complete check logs."""
from __future__ import annotations

from collections import deque
import gzip
import hashlib
import os
from pathlib import Path
import re
import uuid
import zlib


MAX_DECODED_BYTES = 512 * 1024 * 1024
CHUNK = 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _safe_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or name in {'.', '..'}
            or '/' in name or '\\' in name or ':' in name or '\x00' in name):
        raise ValueError('Invalid log descriptor path')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Log directory cannot be a link or junction')
    path = root / name
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Log path cannot be a link or junction')
    if not path.is_file():
        raise ValueError('Missing log file')
    return path


def _hash_size(path: Path, limit: int) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            size += len(chunk)
            if size > limit:
                raise ValueError('Log exceeds byte limit')
            sha.update(chunk)
    return sha.hexdigest(), size


def store_log(root: Path, raw: Path) -> dict:
    """Publish gzip only when smaller; leave raw in place until its receipt exists."""
    root = Path(root)
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Log directory cannot be a link or junction')
    if raw.parent.resolve() != root:
        raise ValueError('Log must be inside the check directory')
    raw = _safe_file(root, raw.name)
    original_sha, original_bytes = _hash_size(raw, MAX_DECODED_BYTES)
    zipped = root / (raw.name + '.gz')
    if zipped.exists():
        raise ValueError('Compressed log destination already exists')
    temporary = root / (zipped.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with raw.open('rb') as source, temporary.open('xb') as sink:
            with gzip.GzipFile(fileobj=sink, mode='wb', filename='', mtime=0) as compressor:
                for chunk in iter(lambda: source.read(CHUNK), b''):
                    compressor.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if temporary.stat().st_size < original_bytes:
            os.replace(temporary, zipped)
            stored = zipped
            kind = 'gzip'
        else:
            stored = raw
            kind = 'raw'
    finally:
        temporary.unlink(missing_ok=True)
    stored_sha, stored_bytes = _hash_size(stored, MAX_DECODED_BYTES)
    return {'path': stored.name, 'codec': kind,
            'original_sha256': original_sha, 'original_bytes': original_bytes,
            'stored_sha256': stored_sha, 'stored_bytes': stored_bytes}


def _validated(root: Path, descriptor: dict) -> tuple[Path, str]:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        'path', 'codec', 'original_sha256', 'original_bytes', 'stored_sha256', 'stored_bytes'
    }:
        raise ValueError('Invalid log descriptor fields')
    kind = descriptor['codec']
    if kind not in {'raw', 'gzip'}:
        raise ValueError('Unknown log codec')
    if (not isinstance(descriptor['path'], str)
            or (kind == 'gzip') != descriptor['path'].endswith('.gz')):
        raise ValueError('Log codec/path mismatch')
    for field in ('original_sha256', 'stored_sha256'):
        if not isinstance(descriptor[field], str) or not _HASH.fullmatch(descriptor[field]):
            raise ValueError('Invalid log hash')
    for field in ('original_bytes', 'stored_bytes'):
        value = descriptor[field]
        if type(value) is not int or not 0 <= value <= MAX_DECODED_BYTES:
            raise ValueError('Invalid log byte count')
    path = _safe_file(root, descriptor['path'])
    stored_sha, stored_bytes = _hash_size(path, MAX_DECODED_BYTES)
    if (stored_sha != descriptor['stored_sha256']
            or stored_bytes != descriptor['stored_bytes']):
        raise ValueError('Stored log changed')
    if kind == 'raw' and (descriptor['stored_sha256'] != descriptor['original_sha256']
                          or descriptor['stored_bytes'] != descriptor['original_bytes']):
        raise ValueError('Raw log identity mismatch')
    return path, kind


def _chunks(root: Path, descriptor: dict):
    path, kind = _validated(root, descriptor)
    sha = hashlib.sha256()
    size = 0
    try:
        with (gzip.open(path, 'rb') if kind == 'gzip' else path.open('rb')) as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b''):
                size += len(chunk)
                if size > MAX_DECODED_BYTES or size > descriptor['original_bytes']:
                    raise ValueError('Decoded log exceeds declared byte count')
                sha.update(chunk)
                yield chunk
    except (EOFError, gzip.BadGzipFile, OSError, zlib.error) as error:
        raise ValueError('Invalid or truncated compressed log') from error
    if size != descriptor['original_bytes'] or sha.hexdigest() != descriptor['original_sha256']:
        raise ValueError('Original log identity mismatch')


def verify_log(root: Path, descriptor: dict) -> None:
    for _ in _chunks(root, descriptor):
        pass


def read_log(root: Path, descriptor: dict) -> bytes:
    return b''.join(_chunks(root, descriptor))


def write_log(root: Path, descriptor: dict, destination: Path) -> None:
    """Write verified decoded bytes to a new temporary path, never replacing it."""
    with destination.open('xb') as sink:
        for chunk in _chunks(root, descriptor):
            sink.write(chunk)
        sink.flush()
        os.fsync(sink.fileno())


def tail_log(root: Path, descriptor: dict, limit: int = 128_000) -> bytes:
    if type(limit) is not int or not 0 <= limit <= 128_000:
        raise ValueError('Invalid log tail limit')
    tail = deque()
    size = 0
    for chunk in _chunks(root, descriptor):
        if limit:
            tail.append(chunk[-limit:])
            size += len(tail[-1])
            while size > limit:
                first = tail.popleft()
                excess = size - limit
                if excess < len(first):
                    tail.appendleft(first[excess:])
                    size = limit
                else:
                    size -= len(first)
    return b''.join(tail)
