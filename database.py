"""
database.py — DB connection and setup.
Single place to get a DB connection across all files.
"""
import sqlite3
import os

DB_PATH = os.environ.get("PSR_DB_PATH", "powerapp.db")


def get_db() -> sqlite3.Connection:
    """Connect to SQLite and ensure all tables exist."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row   # lets you access columns by name
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        db.executescript(f.read())
    return db
