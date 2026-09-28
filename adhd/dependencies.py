"""Load the vendored, unmodified pure-Python dependencies, offline."""
from pathlib import Path
import sys
path = str(Path(__file__).resolve().parent.parent / 'third_party')
if path not in sys.path:
    sys.path.insert(0, path)
