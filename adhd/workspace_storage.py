"""Bounded, owned workspace scratch and file materialization."""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import time
import uuid
import zipfile

from . import dependencies as _dependencies  # Activate shipped offline libraries.
from filelock import FileLock

from .core import store

GIB = 1024 ** 3
MIB = 1024 ** 2
CHUNK = MIB


def _link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
    except FileNotFoundError:
        return False


def _safe_path(path: Path, boundary: Path, *, missing: bool = False) -> Path:
    path = path.absolute()
    boundary = boundary.absolute()
    if not path.is_relative_to(boundary):
        raise ValueError('Storage path escapes workspace: ' + str(path))
    for member in (path, *path.parents):
        if member == boundary.parent:
            break
        if _link(member):
            raise ValueError('Storage path contains a link or reparse point: ' + str(member))
    if not missing and not path.exists():
        raise FileNotFoundError(path)
    return path


def _relative(name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Invalid relative storage path')
    posix = PurePosixPath(name)
    if (posix.is_absolute() or any(part in ('.', '..', '') for part in name.split('/'))
            or re.match(r'^[a-zA-Z]:', name)):
        raise ValueError('Invalid relative storage path')
    return Path(*posix.parts)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file_bytes(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open('rb') as left, second.open('rb') as right:
        while True:
            one, two = left.read(CHUNK), right.read(CHUNK)
            if one != two:
                return False
            if not one:
                return True


def _process_birth(pid: int) -> str | None:
    """Return a PID-reuse fence, or None when this OS cannot establish one."""
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    proc = Path('/proc') / str(pid) / 'stat'
    try:
        return proc.read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _process_state(pid: int, birth: str | None) -> str:
    if not birth:
        return 'unknown'
    observed = _process_birth(pid)
    if observed == birth:
        if os.name == 'nt':
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return 'unknown'
            try:
                state = kernel.WaitForSingleObject(handle, 0)
                if state == 0:  # WAIT_OBJECT_0: exited, even if another handle remains open.
                    return 'dead'
                return 'live' if state == 0x102 else 'unknown'  # WAIT_TIMEOUT
            finally:
                kernel.CloseHandle(handle)
        return 'live'
    if observed is not None:
        return 'dead'
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH or getattr(error, 'winerror', None) in (87, 1168):
            return 'dead'
        return 'unknown'
    except ValueError:
        return 'unknown'
    return 'unknown'


class WorkspaceStorage:
    """Central SQLite reservations plus scratch owned by one workspace/run."""

    def __init__(self, workspace: Path, *, lock_timeout: float = 15):
        if type(lock_timeout) not in {int, float} or not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ValueError('Storage lock timeout must be positive and finite')
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir() or _link(self.workspace):
            raise ValueError('Workspace must be a regular directory')
        self.volume = str(self.workspace.stat().st_dev)
        self.catalog = store() / 'workspace-storage.sqlite3'
        self.lock_timeout = lock_timeout
        self.lock = FileLock(str(self.catalog) + '.lock', timeout=lock_timeout)
        self.scratch_root = self.workspace / '.adhd' / 'scratch'

    @contextmanager
    def _db(self):
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with closing(sqlite3.connect(self.catalog, timeout=self.lock_timeout)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute('''CREATE TABLE IF NOT EXISTS reservations (
                    token TEXT PRIMARY KEY, workspace TEXT NOT NULL, volume TEXT NOT NULL,
                    owner TEXT NOT NULL, bytes INTEGER NOT NULL, pid INTEGER NOT NULL,
                    birth TEXT)''')
                db.execute('''CREATE TABLE IF NOT EXISTS scratch (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, path TEXT NOT NULL,
                    owner TEXT NOT NULL, pid INTEGER NOT NULL, birth TEXT,
                    status TEXT NOT NULL, reservation TEXT NOT NULL,
                    refs TEXT NOT NULL, children TEXT NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS zip_cache (
                    key TEXT NOT NULL, workspace TEXT NOT NULL, volume TEXT NOT NULL,
                    path TEXT NOT NULL, bytes INTEGER NOT NULL,
                    PRIMARY KEY (key, workspace))''')
                db.execute('''CREATE TABLE IF NOT EXISTS zip_cache_refs (
                    key TEXT NOT NULL, workspace TEXT NOT NULL, reference TEXT NOT NULL,
                    PRIMARY KEY(key,workspace,reference))''')
                if 'consumers' not in {r[1] for r in db.execute('PRAGMA table_info(zip_cache_refs)')}:
                    db.execute('ALTER TABLE zip_cache_refs ADD COLUMN consumers INTEGER NOT NULL DEFAULT 1')
                db.execute('''CREATE TABLE IF NOT EXISTS result_logs(
                    workspace TEXT NOT NULL,owner TEXT NOT NULL,value TEXT NOT NULL,
                    PRIMARY KEY(workspace,owner))''')
                yield db

    def retain_latest_logs(self, output:Path, owner:str) -> dict:
        """Retire only unchanged owned captures; preserve reports and final artifacts."""
        output=_safe_path(output,self.workspace)
        root=self.workspace/'.adhd'/'results'
        if output.parent!=root or not re.fullmatch('[0-9a-f]{32}',output.name):
            raise ValueError('Expected an owned result directory')
        files=[]
        for name in ('stdout.log','stderr.log'):
            path=_safe_path(output/name,self.workspace)
            if path.is_file():files.append({'name':name,'sha256':_file_digest(path),'bytes':path.stat().st_size})
        result={'retired_bytes':0,'protected_files':0,'policy':'latest captures per owner; reports preserved'}
        with self._db() as db:
            previous=db.execute('SELECT value FROM result_logs WHERE workspace=? AND owner=?',
                                (str(self.workspace),owner)).fetchone()
            if previous:
                old=json.loads(previous[0]);folder=Path(old['path'])
                if folder!=output and folder.parent==root and re.fullmatch('[0-9a-f]{32}',folder.name):
                    for row in old['files']:
                        if row['name'] not in {'stdout.log','stderr.log'}:continue
                        path=folder/row['name']
                        try:
                            _safe_path(path,self.workspace)
                            if path.stat().st_size!=row['bytes'] or _file_digest(path)!=row['sha256']:
                                result['protected_files']+=1;continue
                            path.unlink();result['retired_bytes']+=row['bytes']
                        except FileNotFoundError:pass
                        except (OSError,ValueError):result['protected_files']+=1
            db.execute('INSERT INTO result_logs VALUES(?,?,?) ON CONFLICT(workspace,owner) '
                       'DO UPDATE SET value=excluded.value',
                       (str(self.workspace),owner,json.dumps({'path':str(output),'files':files})))
        return result

    def reserve(self, owner: str, bytes: int, minimum_free: int = 2 * GIB) -> str:
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
            raise ValueError('Reservation owner is required')
        if (type(bytes) is not int or bytes < 0 or type(minimum_free) is not int
                or minimum_free < 0):
            raise ValueError('Invalid storage reservation size')
        with self._db() as db:
            outstanding = db.execute('SELECT COALESCE(SUM(bytes), 0) FROM reservations WHERE volume=?',
                                     (self.volume,)).fetchone()[0]
            available = shutil.disk_usage(self.workspace)[2]
            if available - outstanding - bytes < minimum_free:
                raise ValueError('Insufficient free space for storage reservation')
            token = uuid.uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?)',
                       (token, str(self.workspace), self.volume, owner, bytes,
                        os.getpid(), _process_birth(os.getpid())))
            return token

    def release(self, token: str) -> None:
        with self._db() as db:
            row = db.execute('SELECT workspace FROM reservations WHERE token=?', (token,)).fetchone()
            if row and row['workspace'] != str(self.workspace):
                raise ValueError('Reservation belongs to another workspace')
            db.execute('DELETE FROM reservations WHERE token=?', (token,))

    def _root(self) -> Path:
        _safe_path(self.scratch_root, self.workspace, missing=True)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        return self.scratch_root

    def _row(self, db: sqlite3.Connection, path: Path) -> sqlite3.Row:
        row = db.execute('SELECT * FROM scratch WHERE path=? AND workspace=?',
                         (str(path.absolute()), str(self.workspace))).fetchone()
        if row is None:
            raise ValueError('Scratch path is not owned by this workspace')
        return row

    def add_reference(self, path: Path, reference: str) -> None:
        if not reference or len(reference) > 200:
            raise ValueError('Reference is required')
        with self._db() as db:
            row = self._row(db, Path(path))
            refs = set(json.loads(row['refs']))
            refs.add(reference)
            db.execute('UPDATE scratch SET refs=? WHERE id=?', (json.dumps(sorted(refs)), row['id']))

    def remove_reference(self, path: Path, reference: str) -> None:
        with self._db() as db:
            row = self._row(db, Path(path))
            refs = set(json.loads(row['refs']))
            refs.discard(reference)
            db.execute('UPDATE scratch SET refs=? WHERE id=?', (json.dumps(sorted(refs)), row['id']))

    def register_child(self, path: Path, pid: int) -> None:
        birth = _process_birth(pid)
        if birth is None:
            raise ValueError('Child process identity is unavailable')
        with self._db() as db:
            row = self._row(db, Path(path))
            children = json.loads(row['children'])
            children.append({'pid': pid, 'birth': birth})
            db.execute('UPDATE scratch SET children=? WHERE id=?',
                       (json.dumps(children), row['id']))

    def _clean(self, db: sqlite3.Connection, row: sqlite3.Row) -> bool:
        path = Path(row['path'])
        if (path.parent != self.scratch_root or path.name != row['id']
                or not re.fullmatch(r'[0-9a-f]{32}', row['id'])):
            return False
        try:
            _safe_path(path, self.workspace)
            manifest = path / '.ownership.json'
            _safe_path(manifest, path)
            if manifest.stat().st_size > 4096:
                return False
            ownership = json.loads(manifest.read_text(encoding='utf-8'))
            if ownership != {'id': row['id'], 'workspace': row['workspace'],
                             'owner': row['owner'], 'pid': row['pid'], 'birth': row['birth']}:
                return False
            if json.loads(row['refs']):
                return False
            if any(_process_state(child['pid'], child['birth']) != 'dead'
                   for child in json.loads(row['children'])):
                return False
            for folder, dirs, files in os.walk(path, followlinks=False):
                for name in (*dirs, *files):
                    if _link(Path(folder) / name):
                        return False
            _safe_path(path, self.workspace)
            shutil.rmtree(path)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        db.execute('DELETE FROM scratch WHERE id=?', (row['id'],))
        db.execute('DELETE FROM reservations WHERE token=?', (row['reservation'],))
        return True

    @contextmanager
    def scratch(self, owner: str, reserved_bytes: int = 0,
                minimum_free: int = 2 * GIB):
        token = self.reserve(owner, reserved_bytes, minimum_free)
        path = None
        created_identity = None
        registered = False
        try:
            root = self._root()
            ident = uuid.uuid4().hex
            path = root / ident
            path.mkdir(exist_ok=False)
            created_identity = (path.stat().st_dev, path.stat().st_ino)
            birth = _process_birth(os.getpid())
            ownership = {'id': ident, 'workspace': str(self.workspace), 'owner': owner,
                         'pid': os.getpid(), 'birth': birth}
            (path / '.ownership.json').write_text(json.dumps(ownership), encoding='utf-8')
            with self._db() as db:
                db.execute('INSERT INTO scratch VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (ident, str(self.workspace), str(path), owner, os.getpid(),
                            birth, 'running', token, '[]', '[]'))
            registered = True
            producer_error = None
            try:
                yield path
            except BaseException as error:
                producer_error = error
                raise
            finally:
                cleanup_pending = False
                with self._db() as db:
                    row = db.execute('SELECT * FROM scratch WHERE id=?', (ident,)).fetchone()
                    if row:
                        db.execute('UPDATE scratch SET status=? WHERE id=?',
                                   ('finalizing', ident))
                        if not self._clean(db, row):
                            db.execute('UPDATE scratch SET status=? WHERE id=?',
                                       ('cleanup_pending', ident))
                            cleanup_pending = not bool(json.loads(row['refs']))
                if cleanup_pending:
                    message = 'Owned scratch cleanup pending: ' + str(path)
                    if producer_error is not None:
                        producer_error.add_note(message)
                    else:
                        raise OSError(message)
        except BaseException:
            if path is not None and path.exists() and not registered:
                # Only a directory this call just created may be removed here.
                _safe_path(path, self.workspace)
                if (path.stat().st_dev, path.stat().st_ino) == created_identity:
                    shutil.rmtree(path)
            if path is None or not path.exists():
                self.release(token)
            raise

    def pending_cleanup(self, *, limit: int = 8) -> dict:
        """Bounded metadata queue; hooks never walk or delete a scratch tree."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError('Cleanup queue limit must be between 1 and 256')
        with self._db() as db:
            deadline = time.monotonic() + .05
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            try:
                rows = db.execute("SELECT id,status,refs!='[]' AS referenced FROM scratch "
                                  'WHERE workspace=? LIMIT ?',
                                  (str(self.workspace), limit + 1)).fetchall()
            finally:
                db.set_progress_handler(None, 0)
        return {'status': 'queued' if rows else 'cleaned', 'scope': 'registered scratch',
                'entries': [dict(row) for row in rows[:limit]], 'more': len(rows) > limit,
                'execution': 'next owned command rechecks ownership and liveness'}

    def sweep(self) -> dict:
        """Recover only registered, unreferenced scratch with dead owners/children."""
        removed, protected = [], []
        with self._db() as db:
            rows = db.execute('SELECT * FROM scratch WHERE workspace=?',
                              (str(self.workspace),)).fetchall()
            for row in rows:
                if (_process_state(row['pid'], row['birth']) != 'dead'
                        and row['status'] == 'running'):
                    protected.append(row['id'])
                elif self._clean(db, row):
                    removed.append(row['id'])
                else:
                    protected.append(row['id'])
                    db.execute('UPDATE scratch SET status=? WHERE id=?',
                               ('cleanup_pending', row['id']))
            # A crashed standalone reservation has no file to inspect. Unknown
            # process identity remains reserved instead of being guessed stale.
            attached = {row['reservation'] for row in db.execute('SELECT reservation FROM scratch')}
            for row in db.execute('SELECT token,pid,birth FROM reservations WHERE workspace=?',
                                  (str(self.workspace),)):
                if row['token'] not in attached and _process_state(row['pid'], row['birth']) == 'dead':
                    db.execute('DELETE FROM reservations WHERE token=?', (row['token'],))
        return {'removed': removed, 'protected': protected}

    def extract_zip_selected(self, archive: Path, members: list[str], *, namespace: str,
                             tool_version: str, options: dict | None = None,
                             max_bytes: int = 512 * MIB, max_cache_bytes: int = 2 * GIB,
                             minimum_free: int = 2 * GIB, _reference: str = 'retained-path') -> Path:
        """Return a checked, content-keyed selected extraction in a bounded cache."""
        archive = _safe_path(Path(archive), self.workspace)
        if not archive.is_file() or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', namespace):
            raise ValueError('Invalid ZIP source or namespace')
        if not tool_version or not members or len(members) > 20_000:
            raise ValueError('ZIP selection and tool version are required')
        names = sorted(set(members))
        for name in names:
            _relative(name)
            if name == '.cache.json':
                raise ValueError('ZIP member conflicts with cache manifest')
        key_data = {'sha256': _file_digest(archive), 'members': names, 'namespace': namespace,
                    'tool_version': tool_version, 'options': options or {}}
        key = hashlib.sha256(json.dumps(key_data, sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()
        cache_root = self.workspace / '.adhd' / 'cache' / 'zip'
        _safe_path(cache_root, self.workspace, missing=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.catalog) + '.cache.lock', timeout=30):
            target = cache_root / key
            if target.exists():
                if self._valid_cache(target, key):
                    with self._db() as db:
                        recorded = db.execute('SELECT 1 FROM zip_cache WHERE key=? AND workspace=?',
                                              (key, str(self.workspace))).fetchone()
                        if not recorded:
                            data = json.loads((target / '.cache.json').read_text(encoding='utf-8'))
                            size = sum(row['bytes'] for row in data['files'])
                            db.execute('INSERT INTO zip_cache VALUES (?,?,?,?,?)',
                                       (key, str(self.workspace), self.volume, str(target), size))
                        db.execute('INSERT INTO zip_cache_refs(key,workspace,reference) VALUES(?,?,?) '
                                   'ON CONFLICT(key,workspace,reference) DO UPDATE SET consumers=consumers+1',
                                   (key,str(self.workspace),_reference))
                    return target
                raise ValueError('Existing ZIP cache entry changed; preserve for inspection')
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                if len(entries) > 100_000:
                    raise ValueError('ZIP entry count exceeds limit')
                info = {entry.filename: entry for entry in entries}
                counts = Counter(entry.filename for entry in entries)
                if any(counts[name] != 1 for name in names):
                    raise ValueError('Selected ZIP member is repeated')
                if any(name not in info or info[name].is_dir() for name in names):
                    raise ValueError('Selected ZIP member is missing or a directory')
                if any((entry.file_size < 0 or entry.file_size > max_bytes
                        or stat.S_ISLNK(entry.external_attr >> 16)) for entry in
                       (info[name] for name in names)):
                    raise ValueError('Unsafe ZIP member')
                expected = sum(info[name].file_size for name in names)
                if expected > max_bytes:
                    raise ValueError('Selected ZIP exceeds byte limit')
                with self._db() as db:
                    used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM zip_cache WHERE volume=?',
                                      (self.volume,)).fetchone()[0]
                    if expected+used>max_cache_bytes:
                        eligible=db.execute('''SELECT c.* FROM zip_cache c WHERE c.workspace=?
                          AND NOT EXISTS(SELECT 1 FROM zip_cache_refs r WHERE r.key=c.key AND r.workspace=c.workspace)
                          ORDER BY c.rowid''',(str(self.workspace),)).fetchall()
                        for entry in eligible:
                            candidate=Path(entry['path'])
                            if (candidate.parent!=cache_root or candidate.name!=entry['key']
                                    or not self._valid_cache(candidate,entry['key'])):
                                continue
                            _safe_path(candidate,self.workspace)
                            shutil.rmtree(candidate)
                            db.execute('DELETE FROM zip_cache WHERE key=? AND workspace=?',
                                       (entry['key'],str(self.workspace)))
                            used-=entry['bytes']
                            if expected+used<=max_cache_bytes:break
                if expected + used > max_cache_bytes:
                    raise ValueError('ZIP cache byte limit exceeded')
                with self.scratch('zip-cache:' + namespace, reserved_bytes=expected,
                                  minimum_free=minimum_free) as scratch:
                    build = scratch / 'entry'
                    build.mkdir()
                    files = []
                    total = 0
                    for name in names:
                        dest = build / _relative(name)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        with source.open(info[name]) as reader, dest.open('xb') as output:
                            for chunk in iter(lambda: reader.read(CHUNK), b''):
                                total += len(chunk)
                                if total > expected or total > max_bytes:
                                    raise ValueError('ZIP expanded beyond reserved bytes')
                                output.write(chunk)
                                digest.update(chunk)
                        files.append({'path': name, 'bytes': dest.stat().st_size,
                                      'sha256': digest.hexdigest()})
                    (build / '.cache.json').write_text(json.dumps({'key': key, 'files': files}),
                                                       encoding='utf-8')
                    os.replace(build, target)
                with self._db() as db:
                    db.execute('INSERT INTO zip_cache VALUES (?,?,?,?,?)',
                               (key, str(self.workspace), self.volume, str(target), expected))
                    db.execute('INSERT INTO zip_cache_refs(key,workspace,reference) VALUES(?,?,?)',
                               (key,str(self.workspace),_reference))
            return target

    @contextmanager
    def selected_zip(self,archive:Path,members:list[str],**options):
        reference=uuid.uuid4().hex
        path=self.extract_zip_selected(archive,members,_reference=reference,**options)
        try:
            yield path
        finally:
            self.release_cache(path,reference)

    def release_cache(self,path:Path,reference:str='retained-path') -> dict:
        path=_safe_path(Path(path),self.workspace)
        expected=self.workspace/'.adhd'/'cache'/'zip'
        if path.parent!=expected or not re.fullmatch('[0-9a-f]{64}',path.name):
            raise ValueError('Expected a registered cache entry')
        with FileLock(str(self.catalog)+'.cache.lock',timeout=30),self._db() as db:
            if not db.execute('SELECT 1 FROM zip_cache WHERE key=? AND workspace=?',
                              (path.name,str(self.workspace))).fetchone():
                raise ValueError('Unknown cache entry')
            values=(path.name,str(self.workspace),reference)
            changed=db.execute('UPDATE zip_cache_refs SET consumers=consumers-1 '
                               'WHERE key=? AND workspace=? AND reference=? AND consumers>1',values).rowcount
            if not changed:
                changed=db.execute('DELETE FROM zip_cache_refs WHERE key=? AND workspace=? AND reference=?',values).rowcount
        return {'released':bool(changed)}

    def _valid_cache(self, path: Path, key: str) -> bool:
        try:
            _safe_path(path, self.workspace)
            manifest = path / '.cache.json'
            if _link(manifest) or manifest.stat().st_size > MIB:
                return False
            data = json.loads(manifest.read_text(encoding='utf-8'))
            if data.get('key') != key or not isinstance(data.get('files'), list):
                return False
            expected = {row['path'] for row in data['files']}
            actual = set()
            for folder, dirs, files in os.walk(path, followlinks=False):
                for name in (*dirs, *files):
                    item = Path(folder) / name
                    if _link(item):
                        return False
                    if item.is_file() and name != '.cache.json':
                        actual.add(item.relative_to(path).as_posix())
            if expected != actual:
                return False
            return all((path / _relative(row['path'])).stat().st_size == row['bytes']
                       and _file_digest(path / _relative(row['path'])) == row['sha256']
                       for row in data['files'])
        except (OSError, ValueError, KeyError, TypeError):
            return False


def bounded_copy(sources: dict[str, Path], destination: Path, *, workspace: Path,
                 max_bytes: int = 512 * MIB, minimum_free: int = 2 * GIB) -> dict:
    """Copy a resolved final-input mapping once, preserving source metadata."""
    manager = WorkspaceStorage(workspace)
    destination = _safe_path(Path(destination), manager.workspace)
    if not destination.is_dir() or type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('Invalid copy destination or byte limit')
    selected = []
    total = 0
    for name, raw in sources.items():
        source = _safe_path(Path(raw), manager.workspace)
        target = _safe_path(destination / _relative(name), manager.workspace, missing=True)
        if not source.is_file():
            raise ValueError('Copy source is not a regular file')
        if target.exists():
            if not target.is_file() or not _same_file_bytes(source, target):
                raise ValueError('Existing destination differs: ' + str(target))
            continue
        size = source.stat().st_size
        total += size
        if total > max_bytes:
            raise ValueError('Copy input exceeds byte limit')
        selected.append((source, target, size))
    token = manager.reserve('bounded-copy', total, minimum_free)
    created, temporary = [], []
    try:
        copied = 0
        for source, target, size in selected:
            _safe_path(source, manager.workspace)
            _safe_path(target, manager.workspace, missing=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name('.' + target.name + '.adhd-copy-' + uuid.uuid4().hex)
            before = source.stat()
            digest = hashlib.sha256()
            with source.open('rb') as reader:
                _safe_path(source, manager.workspace)
                with temp.open('xb') as output:
                    temporary.append(temp)
                    while True:
                        chunk = reader.read(CHUNK)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > total or copied > max_bytes:
                            raise ValueError('Copy source grew beyond reserved byte limit')
                        output.write(chunk)
                        digest.update(chunk)
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
                    after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) or copied > total:
                raise ValueError('Copy source changed during materialization')
            shutil.copystat(source, temp)
            _safe_path(target, manager.workspace, missing=True)
            os.link(temp, target)  # Exclusive publication; temp is immediately unlinked.
            published = target.stat()
            created.append((target, (published.st_dev, published.st_ino), digest.hexdigest()))
            temp.unlink()
            temporary.remove(temp)
        return {'bytes': copied, 'files': len(created)}
    except BaseException as error:
        for path, identity, digest in reversed(created):
            try:
                _safe_path(path, manager.workspace)
                current = path.stat()
                if ((current.st_dev, current.st_ino) == identity
                        and _file_digest(path) == digest):
                    path.unlink()
                else:
                    error.add_note('Changed copy output preserved: ' + str(path))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                error.add_note('Copy output could not be safely removed: ' + str(path))
        raise
    finally:
        cleanup_errors = []
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_errors.append(str(error))
        try:
            manager.release(token)
        except (OSError, sqlite3.Error) as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            active = sys.exc_info()[1]
            message = 'Copy cleanup pending: ' + '; '.join(cleanup_errors)
            if active is not None:
                active.add_note(message)
            else:
                raise OSError(message)
