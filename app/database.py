"""Local SQLite connections and forward-only initial migration."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

def connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=10000')
    return connection

def initialize(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(db_path)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        migration = Path(__file__).with_name('migrations') / '001_initial.sql'
        connection.executescript('BEGIN IMMEDIATE;\n' + migration.read_text(encoding='utf-8') + '\nCOMMIT;')
        with connection:
            connection.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)', (datetime.now(timezone.utc).isoformat(),))
    finally:
        connection.close()
