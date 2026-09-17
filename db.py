"""Schema, connection settings, and transaction helpers."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "hsa.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id            INTEGER PRIMARY KEY,
  owner_name    TEXT    NOT NULL,
  email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT    NOT NULL,
  balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0)
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash    TEXT    PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  expires_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  card_number   TEXT    NOT NULL UNIQUE,
  expiry_month  INTEGER NOT NULL,
  expiry_year   INTEGER NOT NULL,
  cvv           TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'replaced')),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_card_per_account
  ON cards(account_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS deposits (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS purchases (
  id                INTEGER PRIMARY KEY,
  account_id        INTEGER NOT NULL REFERENCES accounts(id),
  card_id           INTEGER NOT NULL REFERENCES cards(id),
  merchant_name     TEXT    NOT NULL,
  merchant_category TEXT    NOT NULL,
  amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
  status            TEXT    NOT NULL,
  decline_reason    TEXT,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  CHECK (
    (status = 'approved' AND decline_reason IS NULL) OR
    (status = 'declined' AND decline_reason IN ('card_inactive', 'not_qualified', 'insufficient_funds'))
  )
);

CREATE INDEX IF NOT EXISTS idx_deposits_account  ON deposits(account_id);
CREATE INDEX IF NOT EXISTS idx_purchases_account ON purchases(account_id);

CREATE VIEW IF NOT EXISTS account_activity AS
  SELECT id, 'D' || id AS ref, account_id, 'deposit' AS type,
         NULL AS merchant_name, NULL AS merchant_category,
         amount_cents, 'approved' AS status, NULL AS decline_reason, created_at
    FROM deposits
  UNION ALL
  SELECT id, 'P' || id, account_id, 'purchase',
         merchant_name, merchant_category,
         amount_cents, status, decline_reason, created_at
    FROM purchases;
"""


def db_path() -> str:
    return os.environ.get("HSA_DB_PATH", str(DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path(),
        isolation_level=None,  # no hidden BEGIN; transaction() writes BEGIN IMMEDIATE itself
        timeout=5.0,  # busy timeout: a second writer waits up to 5 s for the lock instead of failing
        # FastAPI may open and close the connection on different threads. One request
        # still uses it one call at a time, so this is safe.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # stored in the file, so set once here
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection, begin: str):
    conn.execute(begin)
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def transaction(conn: sqlite3.Connection):
    """Write transaction. Takes the database write lock at BEGIN, so writers run one at a time."""
    return _transaction(conn, "BEGIN IMMEDIATE")


def read_transaction(conn: sqlite3.Connection):
    """Read transaction. Takes no write lock, but every SELECT inside sees the same snapshot."""
    return _transaction(conn, "BEGIN")
