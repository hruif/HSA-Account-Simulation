import pytest
from fastapi.testclient import TestClient

import auth
import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HSA_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)  # fast hashes; the rounds are not under test
    db.init_db()


@pytest.fixture
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client
