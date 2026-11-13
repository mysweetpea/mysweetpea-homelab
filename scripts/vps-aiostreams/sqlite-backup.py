#!/usr/bin/env python3
"""Create a consistent, integrity-checked AIOStreams SQLite backup."""
import os
import sqlite3
from pathlib import Path

ROOT = Path('/opt/aiostreams/data')
SOURCE = ROOT / 'db.sqlite'
DESTINATION = ROOT / 'db-backup.sqlite'
TEMP = ROOT / '.db-backup.sqlite.tmp'

try:
    TEMP.unlink(missing_ok=True)
    with sqlite3.connect(f'file:{SOURCE}?mode=ro', uri=True) as source:
        with sqlite3.connect(TEMP) as target:
            source.backup(target)
            result = target.execute('PRAGMA integrity_check').fetchone()
            if result != ('ok',):
                raise RuntimeError(f'SQLite integrity_check failed: {result}')
    os.chmod(TEMP, 0o600)
    os.replace(TEMP, DESTINATION)
    print(f'AIOStreams SQLite backup OK: {DESTINATION.stat().st_size} bytes; integrity_check=ok')
finally:
    TEMP.unlink(missing_ok=True)
