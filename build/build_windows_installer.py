"""Build one offline Windows setup EXE from reviewed public files and Python."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from adhd import __version__
from build.package_adhd import public_files

PYTHON_VERSION = '3.13.16'
PYTHON_ZIP_SHA256 = '97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297'
PYTHON_ZIP_URL = 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip'
CSC = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
OUTPUT = ROOT / '.adhd' / 'deliverables' / f'ADHD-Setup-v{__version__}-windows-x64.exe'
PTH = b'python313.zip\r\n.\r\n..\r\n..\\third_party\r\n'
RUNTIME_CHANGES = (f'Python {PYTHON_VERSION} embeddable Windows x64 runtime\n'
                   f'Original: {PYTHON_ZIP_URL}\n'
                   f'Original ZIP SHA-256: {PYTHON_ZIP_SHA256}\n'
                   'Change: python313._pth adds the installed ADHD release root and its '
                   'vendored third_party directory to the isolated module search path.\n'
                   'The upstream LICENSE.txt is retained in this runtime directory.\n').encode('utf-8')


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def runtime_files(path: Path) -> dict[str, bytes]:
    if hashlib.sha256(path.read_bytes()).hexdigest() != PYTHON_ZIP_SHA256:
        raise ValueError('Embedded Python ZIP digest does not match the pinned official archive')
    result = {}
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('Embedded Python ZIP has a CRC error')
        if len(archive.infolist()) > 100 or sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError('Embedded Python ZIP exceeds its expected bounds')
        for info in archive.infolist():
            name = info.filename
            relative = PurePosixPath(name)
            if (info.is_dir() or name != relative.as_posix() or len(relative.parts) != 1
                    or name in {'', '.', '..'} or '\\' in name or ':' in name or name in result
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe or repeated runtime ZIP path: ' + name)
            result[name] = archive.read(info)
    for name in ('python.exe', 'python313.dll', 'python313.zip', 'python313._pth',
                 '_ssl.pyd', '_sqlite3.pyd', 'sqlite3.dll', 'LICENSE.txt'):
        if name not in result:
            raise ValueError('Embedded Python ZIP is missing ' + name)
    result['python313._pth'] = PTH
    result['RUNTIME-CHANGES.txt'] = RUNTIME_CHANGES
    return result


def payload_files(runtime_zip: Path) -> dict[str, bytes]:
    files = {'source/' + path.relative_to(ROOT).as_posix(): path.read_bytes()
             for path in public_files()}
    for name, data in runtime_files(runtime_zip).items():
        files['source/runtime/' + name] = data
    if len(files) != len(set(files)):
        raise ValueError('Installer payload has repeated paths')
    return files


def build(runtime_zip: Path, output: Path = OUTPUT, compiler: Path = CSC) -> dict:
    if os.name != 'nt' or not compiler.is_file():
        raise RuntimeError('A Windows x64 .NET Framework C# compiler is required to build this EXE')
    entries = payload_files(runtime_zip)
    manifest = ''.join(sha256(data) + '\t' + name + '\n'
                       for name, data in sorted(entries.items())).encode('utf-8')
    source = (ROOT / 'build' / 'windows' / 'launcher.cs').read_text(encoding='utf-8')
    source = source.replace('__MANIFEST_SHA256__', sha256(manifest)).replace('__VERSION__', __version__)
    if '__MANIFEST_SHA256__' in source or '__VERSION__' in source:
        raise ValueError('Launcher template substitution failed')
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='adhd-setup-build-') as temp:
        root = Path(temp)
        cs = root / 'launcher.cs'
        exe = root / 'setup.exe'
        cs.write_text(source, encoding='utf-8')
        command = [str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                   '/out:' + str(exe), '/r:System.Windows.Forms.dll',
                   '/r:System.Drawing.dll', '/r:System.IO.Compression.dll',
                   '/r:System.IO.Compression.FileSystem.dll', str(cs)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('C# compiler failed: ' + (result.stdout + result.stderr)[-4000:])
        with zipfile.ZipFile(exe, 'a', compression=zipfile.ZIP_DEFLATED, compresslevel=9,
                             allowZip64=True) as archive:
            for name, data in sorted(entries.items()):
                archive.writestr(name, data)
            archive.writestr('MANIFEST.sha256', manifest)
        with zipfile.ZipFile(exe) as archive:
            if archive.testzip() is not None or sha256(archive.read('MANIFEST.sha256')) != sha256(manifest):
                raise RuntimeError('Built installer payload failed CRC or manifest validation')
            if set(archive.namelist()) != set(entries) | {'MANIFEST.sha256'}:
                raise RuntimeError('Built installer payload inventory changed')
        os.replace(exe, output)
    return {'path': str(output), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'payload_files': len(entries), 'runtime_version': PYTHON_VERSION,
            'runtime_sha256': PYTHON_ZIP_SHA256}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-zip', required=True, type=Path,
                        help='Already reviewed official Python embeddable ZIP; no download occurs')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.runtime_zip, args.output), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
