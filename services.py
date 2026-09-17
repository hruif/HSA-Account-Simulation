"""Business rules. No HTTP here: every function takes a connection plus plain values."""
import secrets
import sqlite3
from datetime import date

from categories import QUALIFIED
from db import read_transaction, transaction

ACTIVITY_LIMIT = 50
CARD_YEARS_VALID = 3


class DomainError(Exception):
    """A well-formed request that cannot be carried out."""


class EmailTaken(DomainError):
    pass


class CardNotFound(DomainError):
    pass


def signup(conn: sqlite3.Connection, owner_name: str, email: str, password_hash: str) -> int:
    try:
        cur = conn.execute(
            "INSERT INTO accounts (owner_name, email, password_hash) VALUES (?, ?, ?)",
            (owner_name, email, password_hash),
        )
    except sqlite3.IntegrityError as exc:
        raise EmailTaken("An account with this email already exists.") from exc
    return cur.lastrowid


def deposit(conn: sqlite3.Connection, account_id: int, amount_cents: int) -> dict:
    with transaction(conn):
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (amount_cents, account_id),
        )
        row = conn.execute(
            "INSERT INTO deposits (account_id, amount_cents) VALUES (?, ?) RETURNING *",
            (account_id, amount_cents),
        ).fetchall()[0]
        balance = _balance(conn, account_id)
    return {"balance_cents": balance, "deposit": dict(row)}


def issue_card(conn: sqlite3.Connection, account_id: int) -> dict:
    """Issue a card. If the account already has an active card, that card is replaced."""
    today = date.today()
    with transaction(conn):
        conn.execute(
            "UPDATE cards SET status = 'replaced' WHERE account_id = ? AND status = 'active'",
            (account_id,),
        )
        row = conn.execute(
            """INSERT INTO cards (account_id, card_number, expiry_month, expiry_year, cvv)
               VALUES (?, ?, ?, ?, ?)
               RETURNING id, card_number, expiry_month, expiry_year, cvv, status""",
            (
                account_id,
                _unused_card_number(conn),
                today.month,
                today.year + CARD_YEARS_VALID,
                f"{secrets.randbelow(1000):03d}",
            ),
        ).fetchall()[0]
    return dict(row)


def process_purchase(
    conn: sqlite3.Connection,
    card_number: str,
    merchant_name: str,
    merchant_category: str,
    amount_cents: int,
    request_id: str | None = None,
) -> dict:
    with transaction(conn):
        if request_id is not None:
            # A retry of a request that already ran returns what it returned, without
            # charging again. The lookup is inside the write lock, so two identical
            # requests cannot both miss it.
            done = conn.execute(
                "SELECT * FROM purchases WHERE request_id = ?", (request_id,)
            ).fetchone()
            if done is not None:
                return _purchase_result(conn, done)

        card = conn.execute(
            "SELECT id, account_id, status FROM cards WHERE card_number = ?", (card_number,)
        ).fetchone()
        if card is None:
            raise CardNotFound("No card with this number.")

        if card["status"] != "active":
            decline_reason = "card_inactive"
        elif merchant_category not in QUALIFIED:
            decline_reason = "not_qualified"
        else:
            # Check and debit in one statement. Two purchases cannot both pass the WHERE
            # on the same money, and the CHECK constraint backs this up.
            cur = conn.execute(
                """UPDATE accounts SET balance_cents = balance_cents - ?
                   WHERE id = ? AND balance_cents >= ?""",
                (amount_cents, card["account_id"], amount_cents),
            )
            decline_reason = None if cur.rowcount == 1 else "insufficient_funds"

        row = conn.execute(
            """INSERT INTO purchases (account_id, card_id, merchant_name, merchant_category,
                                      amount_cents, status, decline_reason, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (
                card["account_id"],
                card["id"],
                merchant_name,
                merchant_category,
                amount_cents,
                "approved" if decline_reason is None else "declined",
                decline_reason,
                request_id,
            ),
        ).fetchall()[0]
        result = _purchase_result(conn, row)

    return result


def _purchase_result(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "status": row["status"],
        "decline_reason": row["decline_reason"],
        "balance_cents": _balance(conn, row["account_id"]),
        "purchase": dict(row),
    }


def get_dashboard(conn: sqlite3.Connection, account_id: int) -> dict:
    with read_transaction(conn):
        account = conn.execute(
            "SELECT id, owner_name, email, balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        # No card number and no CVV here: this runs on every page load. The owner reads
        # them from get_card_details, one request at a time, when they ask to see them.
        card = conn.execute(
            """SELECT id, substr(card_number, -4) AS last4, expiry_month, expiry_year, status
               FROM cards WHERE account_id = ? AND status = 'active'""",
            (account_id,),
        ).fetchone()
        activity = conn.execute(
            """SELECT * FROM account_activity WHERE account_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (account_id, ACTIVITY_LIMIT),
        ).fetchall()
    return {
        "account": dict(account),
        "card": dict(card) if card else None,
        "activity": [dict(r) for r in activity],
    }


def get_card_details(conn: sqlite3.Connection, account_id: int) -> dict:
    """The full number, expiry and CVV of the account's active card. Owner-only."""
    row = conn.execute(
        """SELECT id, card_number, expiry_month, expiry_year, cvv, status
           FROM cards WHERE account_id = ? AND status = 'active'""",
        (account_id,),
    ).fetchone()
    if row is None:
        raise CardNotFound("No active card.")
    return dict(row)


def account_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def luhn_check_digit(digits: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:  # these positions are doubled once the check digit is appended
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def _unused_card_number(conn: sqlite3.Connection) -> str:
    # Called under the write lock, so a free number cannot be taken before the INSERT.
    while True:
        body = "4" + "".join(str(secrets.randbelow(10)) for _ in range(14))
        number = body + luhn_check_digit(body)
        if conn.execute("SELECT 1 FROM cards WHERE card_number = ?", (number,)).fetchone() is None:
            return number


def _balance(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()[0]
