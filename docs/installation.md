# Install ADHD

## Windows x64 setup file

Download the reviewed `ADHD-Setup-v0.1.6-windows-x64.exe` artifact and open it on a machine with Codex already installed. The setup window shows the detected Codex home. Select **Install**, wait for the result, then restart Codex. Review and trust the new commands if Codex asks. The executable is unsigned, so Windows may show a publisher warning; verify its published SHA-256 before opening it.

The setup file contains the official Python 3.13.16 Windows x64 embeddable runtime, ADHD source, and existing vendored dependencies. Installation needs no system Python, network download, elevation, or Codex login. It installs the runtime inside the immutable managed ADHD release, so hooks keep working after the setup file and its temporary extraction are removed. The original Python `LICENSE.txt` and runtime change note are included. The source package retains its Python 3.11+ install path on Windows, macOS, and Linux.

For an isolated Codex home or automated verification, use:

```powershell
& .\ADHD-Setup-v0.1.6-windows-x64.exe --codex-home C:\path\to\codex-home --quiet --report C:\path\to\setup-report.json
```

The selected home must already exist. Without `--codex-home`, setup uses `CODEX_HOME` when set, then the standard user `.codex` folder; it checks for an existing Codex home before installing. `--quiet` returns a process exit code and `--report` records the result. A second run validates the managed installation and returns `already_installed` when its source digest matches. To inspect the installation, run the installed release's `runtime\python.exe adhd.py doctor --codex-home PATH`. `rollback-native` uses the same managed backup checks and refuses to overwrite later edits.

To rebuild the executable from reviewed local inputs, supply the already downloaded official ZIP explicitly:

```powershell
python build/build_windows_installer.py --runtime-zip C:\path\to\python-3.13.16-embed-amd64.zip
```

The builder requires SHA-256 `97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297` for [Python's embeddable ZIP](https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip), compiles the launcher with the Windows .NET Framework compiler, and packages only `build/public-files.txt` plus that runtime. It does not fetch dependencies or include `.adhd`, personal configuration, credentials, or verification logs. The launcher checks payload inventory, names, sizes, and hashes before extraction; the managed upgrade then validates the existing release and backups before changing user-facing files.
