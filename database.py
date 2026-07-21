"""
database.py — DB connection and setup.
Supports Supabase (PostgreSQL) when SUPABASE_DB_URL or DATABASE_URL is set,
with fallback to SQLite for local standalone development.
"""
import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("PSR_DB_PATH", "powerapp.db")


class DBWrapper:
    """
    Wrapper around DB connection to unify SQLite and PostgreSQL interfaces.
    Automatically translates query placeholders ('?' -> '%s') and polyfills '.lastrowid'.
    """
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres

    def execute(self, sql, params=()):
        if self.is_postgres:
            pg_sql = sql.replace("?", "%s")
            is_insert = pg_sql.strip().upper().startswith("INSERT")
            
            cursor = self.conn.cursor()
            if is_insert and "RETURNING" not in pg_sql.upper():
                pg_sql_returning = pg_sql.rstrip(";") + " RETURNING id;"
                try:
                    cursor.execute(pg_sql_returning, params)
                    row = cursor.fetchone()
                    if row:
                        if isinstance(row, dict) and "id" in row:
                            cursor.lastrowid = row["id"]
                        elif isinstance(row, dict):
                            cursor.lastrowid = list(row.values())[0]
                        else:
                            cursor.lastrowid = row[0]
                    return cursor
                except Exception:
                    self.conn.rollback()
                    cursor = self.conn.cursor()
                    cursor.execute(pg_sql, params)
                    return cursor
            else:
                cursor.execute(pg_sql, params)
                return cursor
        else:
            return self.conn.execute(sql, params)

    def executemany(self, sql, seq_of_params):
        if self.is_postgres:
            pg_sql = sql.replace("?", "%s")
            cursor = self.conn.cursor()
            cursor.executemany(pg_sql, seq_of_params)
            return cursor
        else:
            return self.conn.executemany(sql, seq_of_params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    """Connect to Supabase (PostgreSQL) if configured, else SQLite."""
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if db_url:
        try:
            import psycopg2
            import psycopg2.extras

            url = db_url
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)

            conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Auto-initialize Supabase schema if tables don't exist
            schema_path = os.path.join(os.path.dirname(__file__), "schema_supabase.sql")
            if os.path.exists(schema_path):
                with open(schema_path) as f:
                    with conn.cursor() as cur:
                        cur.execute(f.read())
                conn.commit()

            return DBWrapper(conn, is_postgres=True)
        except Exception as e:
            logger.warning("Supabase connection failed (%s). Falling back to SQLite.", e)

    # Fallback to SQLite
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            db.executescript(f.read())
    return DBWrapper(db, is_postgres=False)
