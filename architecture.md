# HSA Account Simulation — Architecture

How the app is built, how a request travels through it, what is stored, how
simultaneous purchases are kept safe, and what was traded away. Setup and usage
are in [README.md](README.md).

## System architecture

### Stack

| Layer | Choice | Why |
|---|---|---|
| Browser UI | `index.html` + `app.js` + `style.css` | No build step. The reviewer opens one URL. `style.css` is looks only and can be inlined. |
| HTTP server | uvicorn | Listens on `localhost:8000`, hands requests to FastAPI. |
| Web framework | FastAPI | Routes, input validation (Pydantic), free `/docs` page. |
| Business rules | `services.py` | Plain Python functions. All money rules live here, testable without HTTP. |
| Auth | `auth.py` + `bcrypt` | Password hashing, session cookies, "who is logged in" dependency. |
| Database access | stdlib `sqlite3` | No ORM. The atomic SQL is visible in the code. |
| Storage | SQLite in WAL mode | Real persistence, survives restart, zero setup. |
| Tests | pytest | API tests + a threaded concurrency test. |

Money is always **integer cents**. `$80.10` is stored and passed as `8010`. No floats anywhere.

### How the parts connect

```
 Browser                          Python process (uvicorn)                     Disk
 ───────                          ─────────────────────────                    ────
 index.html ─ GET / ────────────▶ FastAPI StaticFiles ─────────────────────▶ static/
 app.js ── fetch('/api/…') JSON ▶ FastAPI route (main.py)
   + session cookie                 │ Pydantic validates body
                                    │ Depends(get_db) opens ONE sqlite3
                                    │   connection for this request
                                    │ Depends(current_account) reads cookie,
                                    │   looks up session → account_id (or 401)
                                    ▼
                                  services.py function
                                    │ BEGIN IMMEDIATE … COMMIT
                                    ▼
                                  sqlite3 ──────────────────────────────────▶ hsa.db
                                    │                                          hsa.db-wal
 app.js updates DOM ◀── JSON ◀─────┘ connection closed                        hsa.db-shm
```

Rules of the connections:

- `app.js` never talks to the database. It only calls `/api/*` and redraws the page.
- `main.py` never runs business logic. It validates input, resolves the connection and the logged-in account, calls one service function, returns its result.
- `services.py` never knows about HTTP or cookies. It takes a connection plus plain values and returns a dict. The concurrency test calls these functions directly from threads.
- `auth.py` owns password hashing and sessions. Services receive an `account_id`, never a token.
- `db.py` owns the schema, the connection settings, and a `transaction(conn)` context manager that issues `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`.
- Each request gets its **own** connection, and this is a correctness rule, not a performance one. A SQLite connection holds exactly one transaction state. If two requests shared one, the second `BEGIN IMMEDIATE` would fail outright, and statements sent without a `BEGIN` would quietly join the transaction already open: the first request's `ROLLBACK` would then discard the second's work, and its `COMMIT` would commit the second's half-finished work. Separate connections give each request its own transaction, which is what every rule in [Concurrency handling](#concurrency-handling) depends on. `sqlite3` connections are also not safe to use from two threads at once, and FastAPI runs each request in its own thread.

Connection settings, applied on every open (`db.connect`):

```python
conn = sqlite3.connect(
    "hsa.db",
    isolation_level=None,     # no hidden BEGIN; we write BEGIN IMMEDIATE ourselves
    timeout=5.0,              # busy timeout: a 2nd writer waits up to 5 s instead of erroring
    check_same_thread=False,  # FastAPI may open and close it on different threads; one request uses it one call at a time
)
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; trades the last commits on a power cut for speed
conn.row_factory = sqlite3.Row
```

`PRAGMA journal_mode=WAL` is stored in the database file, so `db.init_db` sets it once at startup.

**WAL files.** WAL mode uses three files: `hsa.db` (the data), `hsa.db-wal` (new writes are appended here first), and `hsa.db-shm` (a small index into the WAL). Every ~1000 pages SQLite runs a checkpoint: it copies the WAL pages into `hsa.db` and reuses the WAL from the start. The WAL is a crash-safety buffer, not a history. The permanent history is the `deposits` and `purchases` tables. All three files are git-ignored.

### File layout

```
main.py                  FastAPI app, routes, static mount, schema + demo seed on startup
db.py                    connect(), schema SQL, transaction() context manager
auth.py                  hash/verify password, create/lookup/delete session, current_account dependency
services.py              signup, deposit, issue_card, process_purchase, get_dashboard
models.py                Pydantic request/response models
categories.py            QUALIFIED / NOT_QUALIFIED merchant category sets
static/index.html
static/app.js            pages, panels, state, everything with a side effect
static/format.js         pure helpers: parseDollars, formatCents, formatTime, labelFor
static/style.css
tests/test_api.py        one happy-path + one failure test per endpoint, plus auth checks
tests/test_unit.py       Luhn vectors, the error handler, transaction rollback behaviour
tests/test_concurrency.py
hsa.db*                  created at first run, git-ignored
requirements.txt         runtime dependencies
requirements-dev.txt     the above plus pytest and httpx
README.md  architecture.md  ai-usage.md
```

### Pages

Logged out: one panel with Log in / Sign up tabs, and the demo credentials under it.

Logged in, the page is split the way a bank site is. Pages live behind the URL hash, so Back, Forward, and reload keep your place:

| Page | Hash | Shows |
|---|---|---|
| Home | `#/` | Balance with Make a purchase and Deposit funds buttons, the debit card, last 5 activity rows |
| Deposit | `#/deposit` | Current balance and the deposit form |
| Make a purchase | `#/purchase` | Single purchase and concurrency test. Not in the nav (a nav item called "Purchases" reads like a history list); reached from the Home button |
| Activity | `#/activity` | Full table: every deposit and purchase attempt, with decline reasons |

- **Card details are not in the page** unless the owner asks. The dashboard is sent only `last4`; **Show details** fetches the number, expiry and CVV from `GET /api/me/card/details`, and Hide, logging out, or changing page drops them from memory again. Masking them in the browser would have been decoration — anyone can read a response.
- **Purchases have their own page** because on a real card they come from a store's terminal, not from the bank's site. The single-purchase form charges the active card automatically; "Use a different card" opens a field for testing a replaced card.
- **Concurrency test** starts every listed amount as a pharmacy purchase with `Promise.all`, and shows how many were approved and the balance left. Presets: $80 + $50, 10 × $30, 20 × $5. The browser opens about six connections per origin, so a 20-amount burst arrives in waves of about six rather than in one instant; `tests/test_concurrency.py` is what races them from real threads, both through `services` and through the HTTP path.

Every action re-fetches `GET /api/me` and redraws from the response. The cookie is the only thing that says who you are.

## Request flow

### API

All bodies and responses are JSON. Amounts are `amount_cents` integers.

| Method | Path | Auth | Body | Result |
|---|---|---|---|---|
| POST | `/api/signup` | — | `{owner_name, email, password}` | 201, sets cookie; 409 email taken |
| POST | `/api/login` | — | `{email, password}` | 200, sets cookie; 401 |
| POST | `/api/logout` | cookie | — | 204, clears cookie |
| GET | `/api/me` | cookie | — | account + active card (`last4`, expiry, status — **no number, no CVV**) + last 50 rows of `account_activity` (approved **and** declined) |
| POST | `/api/me/deposits` | cookie | `{amount_cents}` | 200 `{balance_cents, deposit}` |
| POST | `/api/me/card` | cookie | — | 201 new card, **including the full number** — the one moment the owner is shown it; replaces the active one if it exists |
| GET | `/api/me/card/details` | cookie | — | 200 full number, expiry and CVV of the active card; 404 if there is none |
| POST | `/api/purchases` | — | `{card_number, merchant_name, merchant_category, amount_cents, request_id?}` | 200 `{status, decline_reason, balance_cents, purchase}`; 404 unknown card |
| GET | `/api/categories` | — | — | `{qualified: [...], not_qualified: [...], max_amount_cents}` |

- **`/api/purchases` takes no session.** It plays the role of the card network: a merchant sends the card number, not the cardholder's login. The card number is the credential, as with a real card. The UI pre-fills the logged-in user's card number.
- **The card number is never sent back on a read.** `GET /api/me` runs on every page load, so it carries only `last4`. The full number and CVV come from `GET /api/me/card/details`, which the page calls only when the owner clicks **Show details**, and from the 201 that issues the card. Hiding the number in the browser would not have hidden it from anyone reading the response.
- **`request_id` is optional and makes a retry safe.** The same id sent twice returns the first result and charges nothing more. Without it, a retried POST is a second purchase, which is the correct reading of a second unlabelled request.
- **A declined purchase is HTTP 200** with `status: "declined"`. It is a valid business outcome, not an error. 4xx is only for bad input, missing auth, or an unknown card.

### Auth

- **Sign up:** `owner_name`, `email`, `password` (min 8 characters). The password is hashed with `bcrypt` and the account row is inserted. A duplicate email hits the `UNIQUE` constraint → 409. Signing up also logs you in.
- **Log in:** look up by email, `bcrypt.checkpw`. Wrong email and wrong password return the same 401 message, so the response does not reveal which emails exist. Login does not validate the password's length: bcrypt only reads the first 72 bytes, so the input is cut there and any wrong credential comes back as 401 rather than 422. Sign-up still refuses a password over 72 bytes outright, where the message is useful.
- **Sign-up and log in are each one transaction:** the account row and the session row are written together, so a failure cannot leave an account that nobody can log into.
- **Session:** `secrets.token_urlsafe(32)` goes to the browser in a cookie (`HttpOnly`, `SameSite=Lax`, 7-day expiry). The database stores only its sha256, so a leaked database cannot be used to log in.
- **Every `/api/me/*` route** depends on `current_account`: read cookie → hash → look up an unexpired session → `account_id`, else 401. Services only ever see that `account_id`, so one user cannot act on another's account.
- **Log out** deletes the session row and clears the cookie.
- **CSRF:** `SameSite=Lax` stops other sites sending the cookie on POST, and a middleware rejects any non-GET request whose `Origin` header names a different host (403).
- **Demo user:** on startup, if there are no accounts, seed `demo@example.com` / `demo-password` with $100.00 and an active card. The login page shows these credentials.

### The four requirements

#### 1. Create an HSA account

Information the system needs: `owner_name` (shown on the dashboard and the card), `email` + `password` (login), `balance_cents` (starts at 0). Nothing else — no SSN, address, or employer, because nothing in the simulation uses them.

Flow: sign-up form → `POST /api/signup` → `services.signup(conn, owner_name, email, password_hash)` → `INSERT INTO accounts` → `auth.create_session` → cookie set → dashboard loads from `GET /api/me`.

#### 2. Deposit funds

Flow: deposit form → `POST /api/me/deposits {amount_cents}` → `services.deposit(conn, account_id, amount_cents)`.

Inside one `BEGIN IMMEDIATE`:

```sql
UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?;
INSERT INTO deposits (account_id, amount_cents) VALUES (?, ?);
```

Both happen or neither. Pydantic rejects `amount_cents <= 0` before the service runs. IRS annual contribution limits are out of scope.

#### 3. Issue or replace a card

Flow: "Issue card" / "Replace card" button → `POST /api/me/card` → `services.issue_card(conn, account_id)`.

Inside one `BEGIN IMMEDIATE`:

```sql
UPDATE cards SET status = 'replaced' WHERE account_id = ? AND status = 'active';  -- 0 or 1 rows
INSERT INTO cards (account_id, card_number, expiry_month, expiry_year, cvv) VALUES (…);
```

- Number: `4` + 14 random digits + Luhn check digit = 16 digits. Passes a checksum, is not a real card.
- Expiry: 3 years from today. CVV: 3 random digits.
- The partial unique index guarantees at most one active card, even if two replace requests race.
- Old cards stay in the table, so old purchases still point to the card that made them. A purchase on a replaced card is declined `card_inactive`.
- Card details are stored in plain text. This is a simulation; a real issuer never stores the CVV and keeps card numbers in a tokenization vault.

#### 4. Process purchases

Inputs: `card_number`, `merchant_name`, `merchant_category`, `amount_cents`. Category comes from a fixed dropdown.

Qualified: `pharmacy, hospital, doctor, dental, vision, medical_equipment, lab`.
Not qualified: `restaurant, grocery, electronics, gas, entertainment, retail, other`.

`services.process_purchase(conn, card_number, merchant_name, merchant_category, amount_cents)`, all inside one `BEGIN IMMEDIATE`:

```
1. SELECT card by card_number
     none              → 404, nothing stored
     status=replaced   → declined card_inactive
2. category not in QUALIFIED
                       → declined not_qualified   (balance untouched)
3. UPDATE accounts SET balance_cents = balance_cents - :amt
     WHERE id = :account_id AND balance_cents >= :amt
     rowcount == 0     → declined insufficient_funds
     rowcount == 1     → approved
4. INSERT INTO purchases (… status, decline_reason …)
5. COMMIT
```

Order matters: a restaurant purchase with too little money is declined `not_qualified`, because that rule does not depend on the balance.

## Data model

```sql
CREATE TABLE accounts (
  id            INTEGER PRIMARY KEY,
  owner_name    TEXT    NOT NULL,
  email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,   -- login username
  password_hash TEXT    NOT NULL,                         -- bcrypt, never the plain password
  balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0)
);

CREATE TABLE sessions (
  token_hash    TEXT    PRIMARY KEY,                      -- sha256 of the cookie value
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  expires_at    TEXT    NOT NULL
);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);   -- expired rows are swept on login

CREATE TABLE cards (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  card_number   TEXT    NOT NULL UNIQUE,                  -- 16 digits, Luhn-valid, fake
  expiry_month  INTEGER NOT NULL CHECK (expiry_month BETWEEN 1 AND 12),
  expiry_year   INTEGER NOT NULL CHECK (expiry_year >= 2000),
  cvv           TEXT    NOT NULL CHECK (length(cvv) = 3),
  status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','replaced')),
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);
-- at most one active card per account; any number of replaced ones
CREATE UNIQUE INDEX one_active_card_per_account ON cards(account_id) WHERE status = 'active';

CREATE TABLE deposits (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

CREATE TABLE purchases (
  id                INTEGER PRIMARY KEY,
  account_id        INTEGER NOT NULL REFERENCES accounts(id),
  card_id           INTEGER NOT NULL REFERENCES cards(id),
  merchant_name     TEXT    NOT NULL,
  merchant_category TEXT    NOT NULL,
  amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
  status            TEXT    NOT NULL,
  decline_reason    TEXT,
  request_id        TEXT    UNIQUE,   -- optional; a retry with the same id is not charged twice
  created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
  CHECK (
    (status = 'approved' AND decline_reason IS NULL) OR
    (status = 'declined' AND decline_reason IN ('card_inactive','not_qualified','insufficient_funds'))
  )
);

-- created_at leads the sort in the activity query, so it belongs in the index
CREATE INDEX idx_deposits_account  ON deposits(account_id, created_at);
CREATE INDEX idx_purchases_account ON purchases(account_id, created_at);

-- one place to read all money activity; nothing to keep in step
CREATE VIEW account_activity AS
  SELECT id, 'D' || id AS ref, account_id, 'deposit' AS type,
         NULL AS merchant_name, NULL AS merchant_category,
         amount_cents, 'approved' AS status, NULL AS decline_reason, created_at
    FROM deposits
  UNION ALL
  SELECT id, 'P' || id, account_id, 'purchase',
         merchant_name, merchant_category,
         amount_cents, status, decline_reason, created_at
    FROM purchases;
```

Decisions:

- **Deposits and purchases are separate tables.** They have different shapes: a deposit has no card, no merchant, and cannot be declined. In one table, half the columns would be NULL for each type and the schema could not stop bad rows. Split, every column always has a meaning and the `CHECK` ties `status` to `decline_reason`.
- **A view, not a parent table,** gives "one list of all transactions". A parent `transactions` table would mean two inserts per write that must stay in step, and a join to show any row. It starts to pay off only when other records must point at "any transaction" (refunds, disputes), which is out of scope. The two tables have separate id sequences, so the view exposes `ref` (`D7`, `P7`) as the display id.
- **`accounts.balance_cents` is the source of truth for approvals,** because the atomic `UPDATE … WHERE balance_cents >= ?` needs a column to guard. The tables are the audit trail. A test asserts they agree: `balance == Σ deposits − Σ approved purchases`.
- **`CHECK (balance_cents >= 0)`** is the last line of defense. Even with an application bug, the database refuses to store a negative balance.
- **Every purchase attempt is stored,** approved or declined, so the user can see why something failed. The one exception is an unknown card number: there is no account to attach it to, so it returns 404 and stores nothing.
- **`card_number` is unique** because purchases find the account by card number. A duplicate would make the paying account ambiguous. On a random collision, generate again.
- **One card belongs to one account.** A purchase must debit exactly one account, and real HSAs are individually owned. Family cards (many cards on one account) are a future improvement.
- **`active` / `replaced`,** not `active` / `inactive`. `replaced` says the card can never come back. A reversible `frozen` state is added only if a freeze feature is built.
- **`created_at`** exists only where it is shown or used for order: cards, deposits, purchases. It is written with millisecond precision (`strftime('%Y-%m-%d %H:%M:%f','now')`), because **order comes from the timestamp, not from the id**: deposits and purchases have separate id sequences, so `id` cannot compare a deposit with a purchase. With one-second precision a deposit and a purchase made in the same second came back in the wrong order. Two rows written inside the same millisecond can still tie, and then the ordering between the two tables is again arbitrary; a single `ledger` table with one id sequence is the real fix and is the change to make if this grows.
- **The view is dropped and recreated on every start,** so it always matches the code. `CREATE VIEW IF NOT EXISTS` would have kept an older definition alive. See "no migrations" under Design tradeoffs for the tables, which are not recreated.
- **Merchant categories are a Python constant,** not a table. The assignment does not ask for editing them.

## Concurrency handling

The assignment's test: balance $100, purchase A $80 and purchase B $50 arrive at the same time. Exactly one must be approved and the balance must never go negative.

**The bug being tested for** is read-then-write:

```python
bal = SELECT balance …      # thread A reads 100, thread B reads 100
if bal >= amount:           # both pass
    UPDATE balance = ?      # A writes 20, B writes 50 → last writer wins, A's $80 was never deducted
```

Writing `balance = balance - amount` instead does not fix it: both updates apply and the balance becomes −30.

**Three independent guards.** Each one alone prevents the bug:

1. **`BEGIN IMMEDIATE`** takes SQLite's write lock at the start of the transaction. Thread B waits at `BEGIN` (up to `busy_timeout`) until A commits, then sees the new balance. All writers run one at a time.
2. **Conditional `UPDATE … WHERE balance_cents >= :amt`** makes check-and-debit one atomic statement. Two such updates cannot both pass on a $100 balance, even without guard 1.
3. **`CHECK (balance_cents >= 0)`.** If guards 1 and 2 were both removed, the bad write raises `IntegrityError` and rolls back instead of storing a negative number.

**Why not a Python `threading.Lock`?** A `Lock` is an object in one Python process's memory that only one thread can hold at a time. Wrapping read-check-write in `with lock:` fixes the race, and with a single uvicorn process it would work. It is still the wrong layer:

- It exists only inside one process. `uvicorn --workers 4` runs 4 processes with 4 separate locks, so purchases in different workers both get in. The same goes for a second server, a script, or the `sqlite3` shell. The database lock lives with the data, so every writer obeys it.
- Nothing forces code to use it. One new code path without `with lock:` brings the bug back. Guards 2 and 3 protect the data no matter which code writes.
- One global lock makes every account wait on every other account.

The same three guards work unchanged on Postgres (with row locks instead of a whole-database lock).

**Proof, automated** — `tests/test_concurrency.py`:

- Assignment case: $100 balance, `ThreadPoolExecutor(2)` submits $80 and $50 at once → exactly one `approved`, one `insufficient_funds`, balance ∈ {$20, $50}.
- Stress case: $100 balance, 20 threads each attempt $30 → exactly 3 approved, 17 declined, balance == $10.
- Ledger integrity after each case: `balance_cents == Σ deposits − Σ approved purchases`.
- Each thread opens its own connection and calls `services.process_purchase` directly. No HTTP in the loop, so the test isolates the database guarantee.

**Proof, visible** — the Make a purchase page has a "Concurrency test" panel: pick a count and an amount, click once, and `app.js` fires them all with `Promise.all(fetch…)`. The activity table fills with the approved/declined mix and the final balance.

## Design tradeoffs

- **SQLite, not Postgres.** Zero setup for the reviewer. The write lock covers the whole database, which limits throughput but is correct for a single-node demo. The same SQL works on Postgres.
- **A new connection per request, not a pool.** A pool would be safe — it lends each request a connection of its own and takes it back, which is the part that matters; what is never safe is two requests using one connection at the same time, for the transaction-state reason above. The pool is skipped because it buys nothing: `sqlite3.connect` on a local file costs microseconds, next to a `BEGIN IMMEDIATE` that waits for the write lock. A server where opening a connection means a network handshake and authentication, as with Postgres, needs one.
- **Stored balance column, not computed from the tables.** Computing it is O(history) per read, and only this shape lets the database enforce "never negative" at all: a `CHECK` cannot sum other tables, so `CHECK (balance_cents >= 0)` would have to become a trigger or application code. The cost is derived data with no database-level tie to the ledger: every writer must keep it in step, and here only a test checks that it did.
- **Separate `deposits` and `purchases` tables with a view,** not one wide table or a parent table. Each table then holds only columns that are always meaningful, so `merchant_name`, `merchant_category` and the `status`/`decline_reason` CHECK can be `NOT NULL` and actually enforced; in one wide table they would all be nullable and that CHECK would have to exempt deposits. Four costs come with it. The two tables have separate id sequences, so only `created_at` can order a deposit against a purchase — the timestamp bullet below is that cost. `account_activity` is a `UNION ALL`, so the activity query reads every row of both tables for the account and sorts them before the `LIMIT 50`; the per-table indexes cannot serve the combined sort. A third kind of money movement — a refund, a fee, interest — means a new table and an edit to the view, not just an insert. And a view has no primary key, so nothing can foreign-key to "an activity row"; a disputes table would have to store a type tag plus an id. One `ledger` table fixes all four and pays for it with the nullable columns.
- **Minimal auth.** No email verification, password reset, login rate limiting, or session rotation.
- **Purchase endpoint trusts the card number.** No merchant authentication, CVV/expiry check, or fraud rules.
- **Card data in plain text.** Simulation only.
- **Categories in code.** Real systems use merchant category codes (MCC) from the card network.
- **No IRS contribution limit on deposits.** The only cap is technical: `MAX_AMOUNT_CENTS`, $1,000,000 per request, which is there to bound the input, not to model the law. The real rule for 2026 is $4,400 a year for self-only coverage and $8,750 for family, plus $1,000 more from age 55 (Rev. Proc. 2025-19). Enforcing it is a data model change, not a rule layer: `accounts` would need coverage type and date of birth, every deposit would need the tax year it counts against, and contributions made through payroll or a second custodian would have to be counted too, because the limit is per person per year and not per account.
- **One `CREATE TABLE ... IF NOT EXISTS` script, no migrations.** `init_db()` runs the whole schema on every start, so the reviewer needs no extra tool and the current shape is one readable block that the tests rebuild in a single call. The cost is that it only adds missing tables: change a column and an existing `hsa.db` keeps the old shape until it is deleted. Numbered migration files with an applied-migrations table are what a deployed system needs; here there is no old data anywhere to upgrade: `hsa.db*` is git-ignored and never committed, so a clone has no database at all and the first run builds the current schema. Only a checkout that has already been run needs the file deleted after a schema change.
- **Order comes from a millisecond timestamp, not from one id sequence.** `deposits` and `purchases` have separate ids, so nothing but `created_at` can put a deposit and a purchase in order. At one-second precision they came back in the wrong order, which is why the default is now `strftime('%Y-%m-%d %H:%M:%f','now')`. Two rows written inside the same millisecond can still tie, and then their order is arbitrary again: measured at about 33 writes per millisecond when a script drives the service layer flat out, and never in a browser, where a round trip is far longer. The effect is confined to how two rows are listed — balances and approvals are decided by the `UPDATE`, not by this sort — so it did not justify the fix that removes it, which is one `ledger` table with a single id sequence. That is the change to make if this grows.
- **`PRAGMA synchronous = NORMAL` with WAL.** Every committed transaction stays consistent and the database cannot be corrupted, but the WAL is not fsynced on each commit, so a host crash or power cut can lose the last few commits. For a local simulation that trade buys a large write speed-up; a real ledger would use `FULL`.
- **No automated browser tests.** jsdom or Playwright would cover the page, and anything with real users needs them, because hand-checking a UI does not scale. Here the server already refuses whatever a broken page could send, and those paths are tested, so the risk left over did not justify a second toolchain in a Python project. What that misses is anything the server cannot see, such as whether the card number leaves the page's memory.
- **Native ES modules, no bundler.** `index.html` loads `app.js` with `type="module"`, so the split above needs no build step, no `node_modules` and no source maps. The same mechanism is what would split `app.js` further, one file per page, if the app grew.

### Out of scope

Refunds and disputes, pending → settled states, card freeze, family cards on one account, receipt substantiation, contribution limits, password reset, real MCC codes.

- **No rate limiting.** `/api/login` accepts unlimited password attempts, and `/api/purchases` is unauthenticated by design — it plays the card network — so it accepts unlimited card-number guessing. A real system would apply per-IP and per-account limits on login, and velocity and fraud checks on the card, plus an alert on repeated unknown-card 404s. A limiter is not built here because it would need shared state that SQLite and a single process make uninteresting to demonstrate.
- **Pagination.** The activity list is the newest 50 rows, with no way to page back further.

## Run it

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt    # fastapi, uvicorn, bcrypt + pytest, httpx
uvicorn main:app --reload              # http://localhost:8000
pytest
```
