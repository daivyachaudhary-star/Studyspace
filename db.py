"""Database layer for StudySpace.

The app was written for SQLite (a single file, studyspace.db). Streamlit Community
Cloud wipes files that are not in the GitHub repo every time the app restarts, so
accounts and chats were lost. This module keeps the same code working but stores
the data in a hosted PostgreSQL database (Supabase) when a DATABASE_URL secret is
set. Without DATABASE_URL it falls back to the local SQLite file, so the app still
runs on your own computer exactly as before.

main.py only needs two things from here:
    db.connect()         -> a connection with .cursor(), .commit(), .close()
    db.IntegrityError    -> the error raised when a UNIQUE value already exists
"""
import os
import re
import sqlite3
import threading

SQLITE_FILE = "studyspace.db"


def _database_url():
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    try:
        import streamlit as st
        return st.secrets.get("DATABASE_URL", "") or ""
    except Exception:
        return ""


DATABASE_URL = _database_url()
USING_POSTGRES = bool(DATABASE_URL)

if USING_POSTGRES:
    import psycopg
    from psycopg_pool import ConnectionPool

    IntegrityError = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)
else:
    IntegrityError = sqlite3.IntegrityError


# ---------------------------------------------------------------------------
# SQLite -> PostgreSQL translation (only used when DATABASE_URL is set)
# ---------------------------------------------------------------------------
# SQLite's CURRENT_TIMESTAMP is a UTC text like "2026-10-06 16:12:12". The app
# compares and parses these as text, so Postgres stores the same text format.
_NOW_TEXT = "to_char(timezone('utc', now()), 'YYYY-MM-DD HH24:MI:SS')"


def _translate(sql, has_params):
    s = sql
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bDATETIME\s+DEFAULT\s+CURRENT_TIMESTAMP\b",
               "TEXT DEFAULT (" + _NOW_TEXT + ")", s, flags=re.I)
    s = re.sub(r"\bDATETIME\b", "TEXT", s, flags=re.I)
    s = re.sub(r"\bBLOB\b", "BYTEA", s, flags=re.I)
    s = re.sub(r"=\s*CURRENT_TIMESTAMP\b", "= " + _NOW_TEXT, s, flags=re.I)

    # INSERT OR IGNORE INTO ...  ->  INSERT INTO ... ON CONFLICT DO NOTHING
    if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", s, flags=re.I):
        s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, count=1, flags=re.I)
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # PRAGMA table_info(t) -> same shape of answer: (cid, name, ...)
    m = re.match(r"\s*PRAGMA\s+table_info\(\s*(\w+)\s*\)\s*$", s, flags=re.I)
    if m:
        return ("SELECT ordinal_position - 1, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = '%s' "
                "ORDER BY ordinal_position" % m.group(1).lower())

    if has_params:
        s = s.replace("%", "%%").replace("?", "%s")
    return s


def _clean_params(params):
    # SQLite stores True/False as 1/0; Postgres INTEGER columns need real ints.
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


def _clean_row(row):
    # Postgres returns BYTEA as memoryview; the app expects plain bytes.
    return tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)


_pool = None
_pool_lock = threading.Lock()
_write_count = 0          # bumped on every commit, so main.py can tell when cached reads may be stale


def write_count():
    return _write_count


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            # Speed: the database is far from the app, so every extra round trip costs ~0.1 s.
            #  - no connection check on every checkout (a stale connection is replaced and retried instead)
            #  - autocommit for plain reads, so a SELECT needs no BEGIN / ROLLBACK round trips
            _pool = ConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=4,
                # Close connections that sit idle, so old app copies (after a restart or redeploy) do not
                # keep taking the database's limited number of connection slots for minutes.
                max_idle=20.0,
                max_lifetime=900.0,
                timeout=20.0,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": None,
                    "connect_timeout": 10,
                    "keepalives": 1,
                    "keepalives_idle": 20,
                    "keepalives_interval": 5,
                    "keepalives_count": 3,
                },
                open=True,
            )
        return _pool


_STALE_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError) if USING_POSTGRES else ()


class _PgCursor:
    def __init__(self, parent):
        self._p = parent
        self._cur = parent._conn.cursor()

    def _run(self, sql, params):
        p = self._p
        # Reads run in autocommit (no transaction). Anything else opens a real transaction first,
        # which commit() / rollback() then ends, like SQLite.
        is_read = sql.lstrip()[:6].upper() == "SELECT"
        if not is_read and not p._in_tx:
            self._cur.execute("BEGIN")
            p._in_tx = True
        if params is None:
            self._cur.execute(sql)
        else:
            self._cur.execute(sql, params)

    def execute(self, sql, params=None):
        p = self._p
        tsql = _translate(sql, params is not None)
        tparams = _clean_params(params) if params is not None else None
        try:
            attempts = 0
            while True:
                try:
                    self._run(tsql, tparams)
                    break
                except _STALE_ERRORS:
                    # The pooled connection went stale (idle too long, or the database restarted).
                    # If nothing is pending, swap it for a fresh one and try again (a few times).
                    attempts += 1
                    if p._in_tx or attempts > 3:
                        raise               # mid-transaction: never replay half a transaction
                    p._in_tx = False
                    p._replace_conn()
                    self._cur = p._conn.cursor()
        except Exception:
            # A failed statement inside a transaction leaves Postgres "aborted". Roll back so the
            # next statement (e.g. a retry after a duplicate join code) works, like SQLite.
            p.rollback()
            raise
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return _clean_row(row) if row is not None else None

    def fetchall(self):
        return [_clean_row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        self._cur.execute("SELECT lastval()")
        return self._cur.fetchone()[0]

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConnection:
    def __init__(self):
        self._pool = _get_pool()
        self._closed = False
        self._conn = self._pool.getconn()
        self._in_tx = False

    def __del__(self):
        # Safety net: if some code path failed before close(), hand the connection back when this object
        # is garbage-collected, instead of leaking one pool slot forever.
        try:
            if not self._closed and getattr(self, "_conn", None) is not None:
                self.close()
        except Exception:
            pass

    def _replace_conn(self):
        old = self._conn
        try:
            self._pool.putconn(old)         # a broken connection is discarded by the pool
        except Exception:
            pass
        self._conn = self._pool.getconn()

    def cursor(self):
        return _PgCursor(self)

    def commit(self):
        global _write_count
        if self._in_tx:
            self._conn.commit()
            self._in_tx = False
        _write_count += 1

    def rollback(self):
        try:
            if self._in_tx or self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                self._conn.rollback()
        except Exception:
            pass
        self._in_tx = False

    def close(self):
        # Give the connection back to the pool. Anything not committed is rolled back, the same
        # as closing an SQLite connection.
        if self._closed:
            return
        self._closed = True
        try:
            self.rollback()
        finally:
            self._pool.putconn(self._conn)


if not USING_POSTGRES:
    def write_count():
        return 0


def connect():
    if USING_POSTGRES:
        return _PgConnection()
    return sqlite3.connect(SQLITE_FILE)
