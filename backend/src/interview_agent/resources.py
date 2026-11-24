"""Read-only bundled resources; user data/configuration stays outside the bundle."""
import sys
from pathlib import Path


def resource_root():
    bundled = getattr(sys, '_MEIPASS', None)
    if bundled:return Path(bundled)
    packaged=Path(__file__).resolve().parent/'_resources'
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[3]
