#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
exec python3 adhd.py upgrade "$@"
