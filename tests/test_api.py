import main
from services import luhn_check_digit


def signup(client, email="jane@example.com", password="correct-horse"):
    return client.post(
        "/api/signup", json={"owner_name": "Jane Doe", "email": email, "password": password}
    )


def funded_card(client, cents=100_00):
    signup(client)
    client.post("/api/me/deposits", json={"amount_cents": cents})
    return client.post("/api/me/card").json()["card_number"]


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
    assert client.get("/api/me").json()["card"]["card_number"] == new

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
