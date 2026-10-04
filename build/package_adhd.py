"""Build a clean, checksum-verified local ADHD source archive."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from adhd import __version__

ARCHIVE_ROOT = f'ADHD-v{__version__}'
OUTPUT = ROOT.parent / f'{ARCHIVE_ROOT}.zip'
PUBLIC_FILE_LIST = ROOT / 'build' / 'public-files.txt'


def public_files() -> list[Path]:
    """Package only reviewed source paths, even when a checkout has private files."""
    names = PUBLIC_FILE_LIST.read_text(encoding='utf-8').splitlines()
    if not names or names != sorted(set(names)) or 'build/public-files.txt' not in names:
        raise ValueError('Public file list is missing, unsorted or duplicated')
    files = []
    for name in names:
        if '\\' in name or name.startswith('/') or ':' in name or any(
            part in {'', '.', '..'} for part in name.split('/')
        ):
            raise ValueError(f'Unsafe public path: {name!r}')
        path = ROOT / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'Public source file missing or unsafe: {name!r}')
        files.append(path)
    return files


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f'Refusing to replace existing archive: {OUTPUT}')
    files = public_files()
    sums = ''.join(f'{sha256(path)}  {path.relative_to(ROOT).as_posix()}\n'
                   for path in files)
    manifest_name = f'{ARCHIVE_ROOT}/SHA256SUMS.txt'
    manifest_bytes = sums.encode('utf-8')
    with zipfile.ZipFile(OUTPUT, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=8) as archive:
        for path in files:
            archive.write(path, f'{ARCHIVE_ROOT}/{path.relative_to(ROOT).as_posix()}')
        archive.writestr(manifest_name, manifest_bytes)
    with zipfile.ZipFile(OUTPUT) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive CRC check failed')
        names = set(archive.namelist())
        for path in files:
            name = f'{ARCHIVE_ROOT}/{path.relative_to(ROOT).as_posix()}'
            if name not in names or hashlib.sha256(archive.read(name)).hexdigest() != sha256(path):
                raise RuntimeError(f'Archive checksum mismatch: {name}')
        if archive.read(manifest_name) != manifest_bytes:
            raise RuntimeError('Archive checksum manifest mismatch')
    print(f'{OUTPUT}\nfiles={len(files) + 1}\nsha256={sha256(OUTPUT)}')


if __name__ == '__main__':
    main()
