"""Unit tests for pieces with no HTTP and no database of their own."""
import asyncio
import sqlite3

import pytest

import db
import main
import services
from services import luhn_check_digit


def test_luhn_check_digit_against_known_card_numbers():
    for number in ("4111111111111111", "4012888888881881", "5555555555554444", "378282246310005"):
        assert luhn_check_digit(number[:-1]) == number[-1]


def test_an_unmapped_domain_error_becomes_400_not_a_crash():
    """DOMAIN_ERROR_STATUS has no entry for DomainError itself, nor for future subclasses."""

    class NewRule(services.DomainError):
        pass

    for exc in (services.DomainError("plain"), NewRule("new")):
        response = asyncio.run(main.domain_error(None, exc))
        assert response.status_code == 400
    assert asyncio.run(main.domain_error(None, services.EmailTaken("taken"))).status_code == 409


class _Wrapper:
    """A connection whose COMMIT or ROLLBACK fails, the way a full disk makes them fail."""

    def __init__(self, conn, fail_on, in_transaction=None):
        self._conn = conn
        self._fail_on = fail_on
        self._in_transaction = in_transaction

    def execute(self, sql, *args):
        if sql == self._fail_on:
            raise sqlite3.OperationalError(f"simulated failure on {sql}")
        return self._conn.execute(sql, *args)

    @property
    def in_transaction(self):
        if self._in_transaction is None:
            return self._conn.in_transaction
        return self._in_transaction


def test_a_failed_commit_rolls_back_instead_of_leaving_the_transaction_open():
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="COMMIT"):
            with db.transaction(_Wrapper(conn, fail_on="COMMIT")):
                conn.execute("INSERT INTO accounts (owner_name, email, password_hash) VALUES (?,?,?)",
                             ("Rolled", "rolled@example.com", "hash"))
        assert conn.in_transaction is False
        assert conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE email = 'rolled@example.com'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_failed_rollback_does_not_replace_the_real_error():
    conn = db.connect()
    try:
        with pytest.raises(ValueError, match="the real problem"):
            with db.transaction(_Wrapper(conn, fail_on="ROLLBACK")):
                raise ValueError("the real problem")
        db._safe_rollback(conn)
    finally:
        conn.close()


def test_rollback_is_skipped_when_sqlite_already_rolled_back():
    conn = db.connect()
    try:
        # in_transaction False: SQLite rolled back by itself, so ROLLBACK would fail.
        wrapper = _Wrapper(conn, fail_on="ROLLBACK", in_transaction=False)
        with pytest.raises(ValueError, match="the real problem"):
            with db._transaction(wrapper, "BEGIN IMMEDIATE"):
                raise ValueError("the real problem")
        db._safe_rollback(conn)
    finally:
        conn.close()
