# HSA Account Simulation

A local web app that simulates a Health Savings Account: sign up, deposit funds, get a virtual debit card, and make purchases that are approved only for qualified medical expenses and only when the balance covers them, including when many purchases arrive at the same time.

Stack: Python (FastAPI + uvicorn), SQLite, plain HTML/CSS/JavaScript. Design: [design.md](design.md).

## Setup

Needs Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app
```

Open http://localhost:8000. The database file `hsa.db` is created on first start, with a demo account:

- Email: `demo@example.com`
- Password: `demo-password`
- Balance $100.00 and an active card

Delete `hsa.db*` to start fresh. API docs are at http://localhost:8000/docs.

## Test

```bash
pytest
```

`tests/test_concurrency.py` races purchases from parallel threads against one account and checks that exactly the right number are approved, the balance never goes below $0, and the balance equals deposits minus approved purchases.

## Try the core flows

1. **Create an account:** Sign up tab, or log in with the demo account.
2. **Deposit:** the **Deposit** page.
3. **Issue a card:** **Home** → Issue card. The number is hidden until you click Show details. Replace card makes the old number stop working.
4. **Purchase:** **Home** → Make a purchase → Single purchase. It charges your active card, the way a store terminal would. Pharmacy, hospital, doctor, dental, vision, medical equipment, and lab are approved; restaurant, grocery, electronics, and the rest are declined. "Use a different card number" lets you try an old, replaced card.
5. **Concurrency:** **Home** → Make a purchase → Concurrency test. With a $100 balance, click **$80 + $50** then **Send all at once**. One is approved, one is declined, and $20 is left.
6. **History:** **Activity** lists every deposit and purchase attempt, including declines and why.

## Layout

```
main.py         HTTP routes, startup, demo seed
auth.py         password hashing, session cookies
services.py     business rules (deposits, cards, purchases)
db.py           schema, connections, transactions
models.py       request validation
categories.py   qualified / not-qualified merchant categories
static/         index.html, app.js, style.css
tests/          API and concurrency tests
```
