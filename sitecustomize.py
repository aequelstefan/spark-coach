import os
import sys

# Ensure `src/` is importable without needing PYTHONPATH tweaks
ROOT = os.path.dirname(__file__)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
