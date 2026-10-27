"""Shared fixtures. Tests run against the seeded database and leave it unchanged."""

import base64
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import engine, get_db
from app.main import app

ASHA = "auth|asha"
VIKRAM = "auth|vikram"


@pytest.fixture
def db():
    """Each test runs inside one outer transaction that is rolled back afterwards.
    create_savepoint mode turns the app's own commit() calls into savepoint
    releases, so even committed API writes disappear after the test."""
    conn = engine.connect()
    outer = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint",
                autoflush=False, expire_on_commit=False)
    yield s
    s.close()
    outer.rollback()
    conn.close()


@pytest.fixture
def client(db):
    """API client whose requests share the test's rolled-back session."""
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def token_for(sub: str, *, secret: str | None = None, algorithm: str | None = None,
              **claim_overrides) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **claim_overrides,
    }
    return jwt.encode(claims, secret or settings.jwt_secret, algorithm=algorithm or settings.jwt_algorithm)


def unsigned_token(sub: str) -> str:
    """A hand-built alg=none token: header.payload. with an empty signature."""
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    payload = {"sub": sub, "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "exp": exp}
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def as_customer(sub: str) -> dict:
    return auth(token_for(sub))
