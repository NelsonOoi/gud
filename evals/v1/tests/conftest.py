import sys
from pathlib import Path

V1 = Path(__file__).resolve().parents[1]
REPO = V1.parents[1]
for path in (V1, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
