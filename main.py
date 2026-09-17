"""HTTP layer: validate input, resolve the connection and logged-in account, call one service."""
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
import services
from categories import NOT_QUALIFIED, QUALIFIED
from models import MAX_AMOUNT_CENTS, DepositIn, LoginIn, PurchaseIn, SignupIn

STATIC_DIR = Path(__file__).parent / "static"
DEMO_EMAIL = "demo@example.com"  # also shown in static/app.js
DEMO_PASSWORD = "demo-password"

Conn = Annotated[sqlite3.Connection, Depends(db.get_db)]
AccountId = Annotated[int, Depends(auth.current_account_id)]


def seed_demo_account(conn: sqlite3.Connection) -> None:
    if services.account_count(conn):
        return
    account_id = services.signup(conn, "Demo User", DEMO_EMAIL, auth.hash_password(DEMO_PASSWORD))
    services.deposit(conn, account_id, 100_00)
    services.issue_card(conn, account_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    conn = db.connect()
    try:
        seed_demo_account(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="HSA Account Simulation", lifespan=lifespan)

DOMAIN_ERROR_STATUS = {services.EmailTaken: 409, services.CardNotFound: 404}


@app.exception_handler(services.DomainError)
async def domain_error(request: Request, exc: services.DomainError) -> JSONResponse:
    # .get, not [...]: DomainError itself and any future subclass must not KeyError here.
    return JSONResponse({"detail": str(exc)}, status_code=DOMAIN_ERROR_STATUS.get(type(exc), 400))


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    # Browsers send Origin on POST. A different origin means another site is trying to use
    # the reviewer's cookie. SameSite=Lax already blocks that; this is a second check.
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
        if urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request rejected."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def revalidate_static_files(request: Request, call_next):
    # Without this, browsers reuse a cached app.js for a while after it changes.
    # no-cache still allows the cache; it just asks the server first (cheap 304 via ETag).
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.post("/api/signup", status_code=201)
def signup(body: SignupIn, response: Response, conn: Conn):
    password_hash = auth.hash_password(body.password)
    # One transaction: either the account and its session both exist, or neither does.
    with db.transaction(conn):
        account_id = services.signup(conn, body.owner_name, body.email, password_hash)
        auth.start_session(conn, response, account_id)
    return services.get_dashboard(conn, account_id)


@app.post("/api/login")
def login(body: LoginIn, response: Response, conn: Conn):
    account_id = auth.authenticate(conn, body.email, body.password)
    if account_id is None:
        return JSONResponse({"detail": "Incorrect email or password."}, status_code=401)
    with db.transaction(conn):
        auth.start_session(conn, response, account_id)
    return services.get_dashboard(conn, account_id)


@app.post("/api/logout", status_code=204)
def logout(
    response: Response,
    conn: Conn,
    token: Annotated[str | None, Cookie(alias=auth.SESSION_COOKIE)] = None,
):
    auth.end_session(conn, response, token)


@app.get("/api/me")
def me(account_id: AccountId, conn: Conn):
    return services.get_dashboard(conn, account_id)


@app.post("/api/me/deposits")
def deposit(body: DepositIn, account_id: AccountId, conn: Conn):
    return services.deposit(conn, account_id, body.amount_cents)


@app.post("/api/me/card", status_code=201)
def issue_card(account_id: AccountId, conn: Conn):
    # The one moment the owner is shown the full card: right after it is issued.
    return services.issue_card(conn, account_id)


@app.get("/api/me/card/details")
def card_details(account_id: AccountId, conn: Conn):
    """Full number, expiry and CVV. Asked for only when the owner clicks Show details."""
    return services.get_card_details(conn, account_id)


@app.post("/api/purchases")
def purchase(body: PurchaseIn, conn: Conn):
    # No login: this plays the card network, where the merchant sends only the card number.
    return services.process_purchase(conn, **body.model_dump())


@app.get("/api/categories")
def categories():
    # max_amount_cents travels with this so the browser checks the same cap as the server.
    return {
        "qualified": QUALIFIED,
        "not_qualified": NOT_QUALIFIED,
        "max_amount_cents": MAX_AMOUNT_CENTS,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
