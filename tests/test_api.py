import re

import pytest

import auth
import db
import main
from models import MAX_AMOUNT_CENTS
from services import luhn_check_digit


def signup(client, email="jane@example.com", password="correct-horse"):
    return client.post(
        "/api/signup", json={"owner_name": "Jane Doe", "email": email, "password": password}
    )


def funded_card(client, cents=100_00):
    signup(client)
    client.post("/api/me/deposits", json={"amount_cents": cents})
    return client.post("/api/me/card").json()["card_number"]  # issue is the one full-number reply


def purchase(client, card_number, cents, category="pharmacy", merchant="CVS"):
    return client.post(
        "/api/purchases",
        json={
            "card_number": card_number,
            "merchant_name": merchant,
            "merchant_category": category,
            "amount_cents": cents,
        },
    )


def balance(client):
    return client.get("/api/me").json()["account"]["balance_cents"]


# ---------- accounts and login ----------


def test_signup_logs_in_with_empty_account(client):
    assert signup(client).status_code == 201
    me = client.get("/api/me").json()
    assert me["account"]["email"] == "jane@example.com"
    assert me["account"]["balance_cents"] == 0
    assert me["card"] is None
    assert me["activity"] == []


def test_duplicate_email_is_rejected_ignoring_case(client):
    signup(client)
    assert signup(client, email="JANE@example.com").status_code == 409


def test_short_password_is_rejected(client):
    assert signup(client, password="short").status_code == 422


def test_login_failure_does_not_reveal_which_part_was_wrong(client):
    signup(client)
    client.post("/api/logout")
    wrong_password = client.post("/api/login", json={"email": "jane@example.com", "password": "nope-nope"})
    unknown_email = client.post("/api/login", json={"email": "who@example.com", "password": "nope-nope"})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_me_requires_login_and_logout_ends_session(client):
    assert client.get("/api/me").status_code == 401
    signup(client)
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/me").status_code == 401


def test_demo_account_is_seeded(client):
    r = client.post("/api/login", json={"email": main.DEMO_EMAIL, "password": main.DEMO_PASSWORD})
    assert r.status_code == 200
    assert r.json()["account"]["balance_cents"] == 100_00
    assert r.json()["card"]["status"] == "active"


def test_cross_site_post_is_rejected(client):
    r = client.post("/api/me/card", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---------- deposits ----------


def test_deposit_updates_balance_and_activity(client):
    signup(client)
    r = client.post("/api/me/deposits", json={"amount_cents": 123_45})
    assert r.status_code == 200
    assert r.json()["balance_cents"] == 123_45
    activity = client.get("/api/me").json()["activity"]
    assert [(a["type"], a["amount_cents"]) for a in activity] == [("deposit", 123_45)]


def test_deposit_rejects_bad_amounts(client):
    signup(client)
    for bad in (0, -500, 10.5, "100"):
        assert client.post("/api/me/deposits", json={"amount_cents": bad}).status_code == 422
    assert balance(client) == 0


# ---------- cards ----------


def test_issued_card_number_is_16_digits_and_luhn_valid(client):
    number = funded_card(client)
    assert len(number) == 16 and number.isdigit()
    assert luhn_check_digit(number[:-1]) == number[-1]


def test_replacing_a_card_deactivates_the_old_one(client):
    old = funded_card(client)
    new = client.post("/api/me/card")
    assert new.status_code == 201
    new = new.json()["card_number"]
    assert new != old
    assert client.get("/api/me").json()["card"]["last4"] == new[-4:]
    assert client.get("/api/me/card/details").json()["card_number"] == new

    declined = purchase(client, old, 10_00).json()
    assert (declined["status"], declined["decline_reason"]) == ("declined", "card_inactive")
    assert purchase(client, new, 10_00).json()["status"] == "approved"


# ---------- purchases ----------


def test_qualified_purchase_is_approved(client):
    card = funded_card(client)
    r = purchase(client, card, 25_00, category="pharmacy")
    assert r.json()["status"] == "approved"
    assert balance(client) == 75_00


def test_non_medical_purchase_is_declined_without_touching_balance(client):
    card = funded_card(client)
    r = purchase(client, card, 10_00, category="restaurant").json()
    assert (r["status"], r["decline_reason"]) == ("declined", "not_qualified")
    assert balance(client) == 100_00


def test_not_qualified_is_checked_before_funds(client):
    card = funded_card(client)
    r = purchase(client, card, 999_00, category="electronics").json()
    assert r["decline_reason"] == "not_qualified"


def test_purchase_over_balance_is_declined(client):
    card = funded_card(client)
    r = purchase(client, card, 100_01, category="hospital").json()
    assert (r["status"], r["decline_reason"]) == ("declined", "insufficient_funds")
    assert balance(client) == 100_00


def test_declined_purchases_appear_in_activity(client):
    card = funded_card(client)
    purchase(client, card, 5_00, category="restaurant")
    statuses = [(a["type"], a["status"]) for a in client.get("/api/me").json()["activity"]]
    assert ("purchase", "declined") in statuses


def test_unknown_card_is_404(client):
    signup(client)
    assert purchase(client, "4000000000000002", 5_00).status_code == 404


def test_unknown_category_is_rejected(client):
    card = funded_card(client)
    assert purchase(client, card, 5_00, category="casino").status_code == 422


# ---------- what the dashboard reveals ----------


def test_dashboard_never_carries_the_card_number_or_cvv(client):
    number = funded_card(client)
    body = client.get("/api/me").text
    assert number not in body
    assert not re.search(r"\d{16}", body)
    assert "cvv" not in body.lower()
    card = client.get("/api/me").json()["card"]
    assert card["last4"] == number[-4:]
    assert "card_number" not in card and "cvv" not in card


def test_card_details_are_owner_only(client):
    number = funded_card(client)
    details = client.get("/api/me/card/details").json()
    assert details["card_number"] == number
    assert len(details["cvv"]) == 3
    client.post("/api/logout")
    assert client.get("/api/me/card/details").status_code == 401


def test_card_details_are_404_without_a_card(client):
    signup(client)
    assert client.get("/api/me/card/details").status_code == 404


# ---------- activity ----------


def test_activity_is_newest_first_within_the_same_second(client):
    """Deposit, purchase, deposit back to back. Deposit and purchase ids are separate
    sequences, so only the timestamp can put these three in the right order."""
    signup(client)
    d1 = client.post("/api/me/deposits", json={"amount_cents": 100_00}).json()["deposit"]["id"]
    card = client.post("/api/me/card").json()["card_number"]
    p1 = purchase(client, card, 5_00).json()["purchase"]["id"]
    d2 = client.post("/api/me/deposits", json={"amount_cents": 1_00}).json()["deposit"]["id"]

    refs = [a["ref"] for a in client.get("/api/me").json()["activity"]]
    assert refs == [f"D{d2}", f"P{p1}", f"D{d1}"]


# ---------- validation ----------


def test_unknown_body_fields_are_rejected(client):
    signup(client)
    r = client.post("/api/me/deposits", json={"amount_cents": 100, "note": "hi"})
    assert r.status_code == 422


def test_amount_cap_boundary(client):
    signup(client)
    assert client.post("/api/me/deposits", json={"amount_cents": MAX_AMOUNT_CENTS}).status_code == 200
    assert client.post("/api/me/deposits", json={"amount_cents": MAX_AMOUNT_CENTS + 1}).status_code == 422
    assert balance(client) == MAX_AMOUNT_CENTS


def test_signup_validates_email_and_name(client):
    for email in ("not-an-email", "no@dot", "two@@example.com", ""):
        assert signup(client, email=email).status_code == 422
    r = client.post(
        "/api/signup", json={"owner_name": "x" * 101, "email": "a@b.co", "password": "correct-horse"}
    )
    assert r.status_code == 422
    r = client.post(
        "/api/signup", json={"owner_name": "  ", "email": "a@b.co", "password": "correct-horse"}
    )
    assert r.status_code == 422


def test_over_long_password_fails_login_but_is_not_a_422(client):
    signup(client)
    client.post("/api/logout")
    r = client.post("/api/login", json={"email": "jane@example.com", "password": "x" * 100})
    assert r.status_code == 401


def test_password_at_the_bcrypt_boundary_signs_up_and_logs_in(client):
    password = "y" * 72
    assert signup(client, email="max@example.com", password=password).status_code == 201
    client.post("/api/logout")
    r = client.post("/api/login", json={"email": "max@example.com", "password": password})
    assert r.status_code == 200
    assert signup(client, email="toolong@example.com", password="y" * 73).status_code == 422


def test_categories_are_two_disjoint_lists_with_the_amount_cap(client):
    body = client.get("/api/categories").json()
    assert body["qualified"] and body["not_qualified"]
    assert set(body["qualified"]).isdisjoint(body["not_qualified"])
    assert body["max_amount_cents"] == MAX_AMOUNT_CENTS


# ---------- sessions ----------


def test_expired_session_is_rejected(client):
    signup(client)
    conn = db.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 day')")
    finally:
        conn.close()
    assert client.get("/api/me").status_code == 401


def test_logout_is_204_with_no_cookie_and_when_repeated(client):
    assert client.post("/api/logout").status_code == 204
    signup(client)
    assert client.post("/api/logout").status_code == 204
    assert client.post("/api/logout").status_code == 204


def test_signup_leaves_no_account_when_the_session_cannot_be_stored(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("session store is down")

    monkeypatch.setattr(auth, "start_session", boom)
    before = account_rows()
    with pytest.raises(RuntimeError):
        signup(client, email="ghost@example.com")
    assert account_rows() == before


def account_rows() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()


# ---------- purchases ----------


def test_one_account_card_cannot_spend_another_accounts_balance(client):
    rich_card = funded_card(client, 100_00)
    client.post("/api/logout")
    signup(client, email="poor@example.com")
    poor_card = client.post("/api/me/card").json()["card_number"]

    r = purchase(client, poor_card, 10_00).json()
    assert (r["status"], r["decline_reason"]) == ("declined", "insufficient_funds")
    assert balance(client) == 0
    assert purchase(client, rich_card, 10_00).json()["status"] == "approved"
    assert balance(client) == 0  # the other account paid, not this one


def test_card_inactive_outranks_the_other_decline_reasons(client):
    old = funded_card(client)
    client.post("/api/me/card")  # replaces it
    for cents, category in ((10_00, "restaurant"), (999_00, "pharmacy"), (999_00, "restaurant")):
        r = purchase(client, old, cents, category=category).json()
        assert r["decline_reason"] == "card_inactive"


def test_same_request_id_is_charged_only_once(client):
    card = funded_card(client)
    body = {
        "card_number": card,
        "merchant_name": "CVS",
        "merchant_category": "pharmacy",
        "amount_cents": 25_00,
        "request_id": "retry-me",
    }
    first = client.post("/api/purchases", json=body).json()
    second = client.post("/api/purchases", json=body).json()
    assert first["status"] == second["status"] == "approved"
    assert first["purchase"]["id"] == second["purchase"]["id"]
    assert balance(client) == 75_00
    rows = [a for a in client.get("/api/me").json()["activity"] if a["type"] == "purchase"]
    assert len(rows) == 1


def test_a_declined_purchase_is_also_replayed_rather_than_retried(client):
    card = funded_card(client)
    body = {
        "card_number": card,
        "merchant_name": "Burger Place",
        "merchant_category": "restaurant",
        "amount_cents": 10_00,
        "request_id": "declined-retry",
    }
    first = client.post("/api/purchases", json=body).json()
    second = client.post("/api/purchases", json=body).json()
    assert first["decline_reason"] == second["decline_reason"] == "not_qualified"
    assert first["purchase"]["id"] == second["purchase"]["id"]
