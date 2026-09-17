"""Purchases racing on one account. Each thread uses its own connection, like a request would."""
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import db
import services


def account_with_card(balance_cents: int) -> tuple[int, str]:
    conn = db.connect()
    try:
        account_id = services.signup(conn, "Racer", f"{uuid.uuid4()}@example.com", "unused-hash")
        services.deposit(conn, account_id, balance_cents)
        card_number = services.issue_card(conn, account_id)["card_number"]
    finally:
        conn.close()
    return account_id, card_number


def fire_at_once(card_number: str, amounts: list[int]) -> list[dict]:
    """Start every purchase at the same moment and wait for all of them."""
    start = threading.Barrier(len(amounts))

    def run(amount_cents: int) -> dict:
        conn = db.connect()
        try:
            start.wait()
            return services.process_purchase(conn, card_number, "Pharmacy", "pharmacy", amount_cents)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=len(amounts)) as pool:
        return list(pool.map(run, amounts))


def assert_ledger_matches_balance(account_id: int) -> int:
    conn = db.connect()
    try:
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
        deposited = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM deposits WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM purchases WHERE account_id = ? AND status = 'approved'",
            (account_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert balance == deposited - spent
    assert balance >= 0
    return balance


def test_assignment_case_80_and_50_against_100():
    account_id, card = account_with_card(100_00)
    results = fire_at_once(card, [80_00, 50_00])

    statuses = sorted(r["status"] for r in results)
    assert statuses == ["approved", "declined"]
    assert [r["decline_reason"] for r in results if r["status"] == "declined"] == ["insufficient_funds"]
    assert assert_ledger_matches_balance(account_id) in (20_00, 50_00)


def test_twenty_purchases_racing_for_one_balance():
    account_id, card = account_with_card(100_00)
    results = fire_at_once(card, [30_00] * 20)

    assert sum(r["status"] == "approved" for r in results) == 3
    assert sum(r["decline_reason"] == "insufficient_funds" for r in results) == 17
    assert assert_ledger_matches_balance(account_id) == 10_00


def test_database_refuses_a_negative_balance_even_without_the_guard():
    account_id, _ = account_with_card(10_00)
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounts SET balance_cents = balance_cents - 2000 WHERE id = ?", (account_id,))
    finally:
        conn.close()


# ---------- through the real HTTP path ----------


def race(calls: list) -> list:
    """Start every call at the same moment and wait for all of them."""
    start = threading.Barrier(len(calls))

    def run(call):
        start.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def http_account_with_card(client, cents: int) -> str:
    client.post(
        "/api/signup",
        json={"owner_name": "Racer", "email": f"{uuid.uuid4()}@example.com", "password": "correct-horse"},
    )
    client.post("/api/me/deposits", json={"amount_cents": cents})
    return client.post("/api/me/card").json()["card_number"]


def test_http_purchases_race_the_same_balance(client):
    """The same race as above, but through TestClient: one connection per request,
    FastAPI's threadpool, and the middleware all take part."""
    card = http_account_with_card(client, 100_00)

    def buy(cents):
        return lambda: client.post(
            "/api/purchases",
            json={
                "card_number": card,
                "merchant_name": "Pharmacy",
                "merchant_category": "pharmacy",
                "amount_cents": cents,
            },
        )

    responses = race([buy(30_00) for _ in range(20)])

    assert all(r.status_code == 200 for r in responses)
    results = [r.json() for r in responses]
    assert sum(r["status"] == "approved" for r in results) == 3
    assert sum(r["decline_reason"] == "insufficient_funds" for r in results) == 17
    assert client.get("/api/me").json()["account"]["balance_cents"] == 10_00


def test_deposits_and_purchases_racing_together():
    """30 deposits against 30 purchases at once. The balance must stay consistent and
    never go negative, whatever order they land in."""
    account_id, card = account_with_card(50_00)

    def deposit():
        conn = db.connect()
        try:
            return services.deposit(conn, account_id, 10_00)
        finally:
            conn.close()

    def buy():
        conn = db.connect()
        try:
            return services.process_purchase(conn, card, "Pharmacy", "pharmacy", 10_00)
        finally:
            conn.close()

    race([deposit] * 30 + [buy] * 30)
    assert_ledger_matches_balance(account_id)


def test_two_card_issues_at_once_leave_one_active_card():
    account_id, _ = account_with_card(10_00)

    def issue():
        conn = db.connect()
        try:
            return services.issue_card(conn, account_id)
        finally:
            conn.close()

    race([issue, issue])

    conn = db.connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE account_id = ? AND status = 'active'", (account_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert active == 1


def test_signing_up_twice_with_one_email_gives_one_201_and_one_409(client):
    email = f"{uuid.uuid4()}@example.com"

    def sign_up():
        return lambda: client.post(
            "/api/signup", json={"owner_name": "Twin", "email": email, "password": "correct-horse"}
        )

    codes = sorted(r.status_code for r in race([sign_up(), sign_up()]))
    assert codes == [201, 409]
