# HSA Account Simulation — Design

Pre-build design. Once the code exists this becomes the basis of `architecture.md`.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Browser UI | One `index.html` + `app.js` + `style.css` | No build step. The reviewer opens one URL. |
| HTTP server | uvicorn | Listens on `localhost:8000`, hands requests to FastAPI. |
| Web framework | FastAPI | Routes, input validation (Pydantic), free `/docs` page. |
| Business rules | `services.py` | Plain Python functions. All money rules live here, testable without HTTP. |
| Database access | stdlib `sqlite3` | No ORM. The atomic SQL is visible in the code. |
| Storage | `hsa.db` (SQLite file, WAL mode) | Real persistence, survives restart, zero setup. |
| Tests | pytest | API tests + a threaded concurrency test. |

Money is always **integer cents**. `$80.10` is stored and passed as `8010`. No floats anywhere.

## How the parts connect

```
 Browser                          Python process (uvicorn)                     Disk
 ───────                          ─────────────────────────                    ────
 index.html ─ GET / ────────────▶ FastAPI StaticFiles ─────────────────────▶ static/
 app.js ── fetch('/api/…') JSON ▶ FastAPI route (main.py)
                                    │ Pydantic validates body
                                    │ Depends(get_db) opens ONE sqlite3
                                    │   connection for this request
                                    ▼
                                  services.py function
                                    │ BEGIN IMMEDIATE … COMMIT
                                    ▼
                                  sqlite3 ──────────────────────────────────▶ hsa.db
                                    │
 app.js updates DOM ◀── JSON ◀─────┘ connection closed
```

Rules of the connections:

- `app.js` never talks to the database. It only calls `/api/*` and redraws the page.
- `main.py` never runs business logic. It validates input, opens a connection, calls one service function, returns its result.
- `services.py` never knows about HTTP. It takes a connection plus plain values and returns a dict. The concurrency test calls these functions directly from threads.
- `db.py` owns the schema, the connection settings, and a `transaction(conn)` context manager that issues `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`.
- Each request gets its **own** connection (`sqlite3` connections are not thread-safe; FastAPI runs each request in its own thread; opening a SQLite file takes well under a millisecond).

Connection settings, applied on every open:

```python
conn = sqlite3.connect("hsa.db", isolation_level=None, timeout=5.0)  # autocommit off → we write BEGIN ourselves
conn.execute("PRAGMA journal_mode=WAL")      # readers don't block the writer
conn.execute("PRAGMA busy_timeout=5000")     # a 2nd writer waits up to 5 s instead of erroring
conn.execute("PRAGMA foreign_keys=ON")
conn.row_factory = sqlite3.Row
```

## File layout

```
main.py                  FastAPI app, routes, static mount, create schema on startup
db.py                    connect(), schema SQL, transaction() context manager
services.py              create_account, deposit, issue_card, process_transaction
models.py                Pydantic request/response models
categories.py            QUALIFIED / NOT_QUALIFIED merchant category sets
static/index.html
static/app.js
static/style.css
tests/test_api.py        one happy-path + one failure test per endpoint
tests/test_concurrency.py
hsa.db                   created at first run, git-ignored
README.md  architecture.md  ai-usage.md
```

## Data model

```sql
CREATE TABLE accounts (
  id            INTEGER PRIMARY KEY,
  owner_name    TEXT    NOT NULL,
  email         TEXT    NOT NULL UNIQUE,
  balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE cards (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL UNIQUE REFERENCES accounts(id),  -- one card per account
  card_number   TEXT    NOT NULL UNIQUE,                           -- 16 digits, Luhn-valid, fake
  expiry_month  INTEGER NOT NULL,
  expiry_year   INTEGER NOT NULL,
  cvv           TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','frozen')),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE transactions (
  id                INTEGER PRIMARY KEY,
  account_id        INTEGER NOT NULL REFERENCES accounts(id),
  card_id           INTEGER REFERENCES cards(id),                 -- NULL for deposits
  type              TEXT    NOT NULL CHECK (type IN ('deposit','purchase')),
  merchant_name     TEXT,
  merchant_category TEXT,
  amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
  status            TEXT    NOT NULL CHECK (status IN ('approved','declined')),
  decline_reason    TEXT    CHECK (decline_reason IN
                      (NULL,'not_qualified','insufficient_funds','card_not_found','card_inactive')),
  created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_transactions_account ON transactions(account_id, id);
```

Decisions:

- `accounts.balance_cents` is the **source of truth** for approvals, because the atomic `UPDATE … WHERE balance_cents >= ?` needs a column to guard. `transactions` is the audit trail. A test asserts they agree: `balance == Σ deposits − Σ approved purchases`.
- `CHECK (balance_cents >= 0)` is the last line of defense. Even if application code has a bug, the database refuses to store a negative balance.
- Every attempt — approved or declined — is a row in `transactions`, so the reviewer can see *why* something was declined.
- Merchant categories are a Python constant, not a table. Simpler, and the assignment does not ask for editing them. Tradeoff noted in `architecture.md`.

## API

All bodies and responses are JSON. Amounts are `amount_cents` integers.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/accounts` | `{owner_name, email}` | 201 account |
| GET | `/api/accounts` | — | list of accounts |
| GET | `/api/accounts/{id}` | — | account + card + last 50 transactions |
| POST | `/api/accounts/{id}/deposits` | `{amount_cents}` | 200 `{balance_cents, transaction}` |
| POST | `/api/accounts/{id}/card` | — | 201 card; 409 if one already exists |
| POST | `/api/transactions` | `{card_number, merchant_name, merchant_category, amount_cents}` | 200 `{status, decline_reason, balance_cents, transaction}` |
| GET | `/api/categories` | — | `{qualified: [...], not_qualified: [...]}` for the UI dropdown |

A declined purchase is HTTP **200** with `status: "declined"`. It is a valid business outcome, not an error. 4xx is only for malformed input (negative amount, unknown account, missing card).

## The four requirements

### 1. Create an HSA account

Information the system needs: `owner_name`, `email` (unique, used as the human-readable identifier in the UI), `balance_cents` (starts at 0), `created_at`. Nothing else — no SSN, address, or employer, because nothing in the simulation uses them. `architecture.md` says this explicitly.

Flow: form in `index.html` → `POST /api/accounts` → `services.create_account(conn, owner_name, email)` → `INSERT INTO accounts` → account list redraws, new account becomes the selected one.

### 2. Deposit funds

Flow: deposit form → `POST /api/accounts/{id}/deposits {amount_cents}` → `services.deposit(conn, account_id, amount_cents)`.

Inside one `BEGIN IMMEDIATE`:

```sql
UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?;
INSERT INTO transactions (account_id, type, amount_cents, status) VALUES (?, 'deposit', ?, 'approved');
```

Both happen or neither. Pydantic rejects `amount_cents <= 0` before the service runs. Annual IRS contribution limits are out of scope and listed as a future improvement.

### 3. Issue a card

Flow: "Issue card" button → `POST /api/accounts/{id}/card` → `services.issue_card(conn, account_id)`.

- Number: `4` + 14 random digits + Luhn check digit = 16 digits. Looks real, passes a checksum, is not a real BIN.
- Expiry: 3 years from today. CVV: 3 random digits. Status `active`.
- `UNIQUE(account_id)` enforces one card per account; a second request returns 409.
- Card details are stored in plaintext. This is a simulation; a real issuer never stores PAN/CVV and uses a tokenization vault. Stated as a tradeoff.

The card is not decoration: purchases are submitted **by card number**, the way a card network would send them, and are declined if the account has no card or the card is frozen.

### 4. Process transactions

Inputs: `card_number`, `merchant_name`, `merchant_category`, `amount_cents`. Category comes from a fixed dropdown in the UI.

Qualified categories: `pharmacy, hospital, doctor, dental, vision, medical_equipment, lab`.
Not qualified: `restaurant, grocery, electronics, gas, entertainment, retail, other`.

`services.process_transaction(conn, card_number, merchant_name, merchant_category, amount_cents)` runs every step inside one `BEGIN IMMEDIATE`:

```
1. SELECT card + account by card_number
     none        → decline card_not_found (no transactions row, no account to attach it to → 404)
     frozen      → decline card_inactive
2. category not in QUALIFIED
                 → decline not_qualified   (balance untouched)
3. UPDATE accounts SET balance_cents = balance_cents - :amt
     WHERE id = :account_id AND balance_cents >= :amt
     rowcount == 0 → decline insufficient_funds
     rowcount == 1 → approved
4. INSERT INTO transactions (… status, decline_reason …)
5. COMMIT
```

The order matters: a restaurant purchase with insufficient funds is declined as `not_qualified`, because the medical check is the cheaper and more important rule.

## Concurrency

The assignment's test: balance $100, purchase A $80 and purchase B $50 arrive at the same time. Exactly one must be approved and the balance must never go negative.

**The bug being tested for** is read-then-write:

```python
bal = SELECT balance …      # thread A reads 100, thread B reads 100
if bal >= amount:           # both pass
    UPDATE balance = ?      # A writes 20, B writes 50 → last writer wins, A's $80 was never deducted
```

Writing `balance = balance - amount` instead does not fix it: both updates apply and the balance becomes −30.

**Three independent guards**, any one of which is enough on its own:

1. **`BEGIN IMMEDIATE`** takes SQLite's write lock at the start of the transaction. Thread B blocks at `BEGIN` until A commits (waits up to `busy_timeout`). When B runs, it sees the post-A balance. This serializes all writers.
2. **Conditional `UPDATE … WHERE balance_cents >= :amt`** makes check-and-debit a single atomic statement. Even with no lock at all, the database evaluates the `WHERE` against the current row under its own internal lock, so two updates cannot both pass on a $100 balance.
3. **`CHECK (balance_cents >= 0)`** on the column. If guards 1 and 2 were both deleted, the second write would raise `IntegrityError` and roll back instead of storing a negative number.

Why not a Python `threading.Lock`? It would work for one process, but it proves nothing about the data layer and breaks the moment there are two uvicorn workers. Putting the guarantee in the database is what makes it real, and the same three guards work unchanged on Postgres.

**Proof, automated** — `tests/test_concurrency.py`:

- Assignment case: $100 balance, `ThreadPoolExecutor(2)` submits $80 and $50 at once → exactly one `approved`, one `insufficient_funds`, balance ∈ {20, 50}.
- Stress case: $100 balance, 20 threads each attempt $30 → exactly 3 approved, 17 declined, balance == 10, and Σ approved amounts == 90.
- Ledger integrity: after each case, `balance_cents == Σ deposits − Σ approved purchases`.
- Each thread opens its own connection and calls `services.process_transaction` directly. No HTTP in the loop, so the test isolates the database guarantee.

**Proof, visible** — the UI has a "Simulate concurrent purchases" panel: pick a count and amount, click once, `app.js` fires them all with `Promise.all(fetch…)`. The transactions table fills in with the approved/declined mix and the final balance, so the reviewer can watch the $100 / $80 / $50 case in the browser.

## UI

One page, one selected account at a time.

```
┌──────────────────────────────┬──────────────────────────────────────────────┐
│ Accounts                     │ Selected: Jane Doe        Balance  $100.00   │
│  [+ Create]  name / email    │                                              │
│  • Jane Doe        $100.00   │ Deposit  [ amount ] [Deposit]                │
│  • Sam Li           $0.00    │                                              │
│                              │ Card    4xxx xxxx xxxx 1234  exp 09/29  ●active │
│                              │         (or [Issue card])                    │
│                              │                                              │
│                              │ Purchase [merchant] [category ▾] [amount]    │
│                              │          [Submit]                            │
│                              │ Concurrency demo  [10] × [$30] [Fire all]    │
│                              │                                              │
│                              │ Transactions                                 │
│                              │  time  type  merchant  category  amt  status │
└──────────────────────────────┴──────────────────────────────────────────────┘
```

Every action re-fetches `GET /api/accounts/{id}` and redraws the right panel from the response. No client-side state beyond "which account is selected".

## Run

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # fastapi, uvicorn, pytest, httpx
uvicorn main:app --reload              # http://localhost:8000
pytest                                 # ~1 s
```

## Tradeoffs to state in architecture.md

- **SQLite, not Postgres.** Zero setup for the reviewer; the write lock is coarse (whole database) which limits throughput but is the correct behavior for a single-node demo. Same SQL works on Postgres.
- **Stored balance column, not computed from ledger.** Needed for the atomic guard; the integrity test keeps it honest.
- **No authentication.** The reviewer acts as every account owner. Out of scope.
- **Card data in plaintext.** Simulation only.
- **Categories in code.** Real systems use merchant category codes (MCC) from the network; a table would allow admin edits.
- **Deposits have no limit.** IRS annual contribution limits are a rule layer to add later.

## Out of scope (list as improvements in the video)

Refunds, pending → settled states, card freeze/unfreeze UI, receipt substantiation, contribution limits, multiple cards per account, auth, real MCC codes.
