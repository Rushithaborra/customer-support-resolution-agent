"""Shared fixtures. Tests run against the seeded database and leave it unchanged."""

import base64
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import engine, get_db
from app.integrations.deps import get_service_client
from app.integrations.http import ServiceClient
from app.main import app
from mocks.main import app as mocks_app

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


STAFF = "staff|priya"


def as_staff(sub: str = STAFF) -> dict:
    return auth(token_for(sub, role="staff"))


# ---------------------------------------------------------------- mock services

# Clients give up after CLIENT_TIMEOUT_S; injected timeouts sleep FAULT_DELAY_S.
CLIENT_TIMEOUT_S = 0.3
FAULT_DELAY_S = 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_server_url():
    """The mock services on a real socket, so timeouts are real network timeouts."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mocks_app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def mock_admin(mock_server_url):
    """Raw HTTP access to the mocks (admin controls and contract checks).
    Resets provider state and faults before each test."""
    with httpx.Client(base_url=mock_server_url, timeout=5,
                      headers={"Authorization": f"Bearer {settings.mock_api_key}"}) as c:
        c.post("/_admin/reset").raise_for_status()
        yield c


@pytest.fixture
def service_client(mock_server_url, mock_admin):
    with ServiceClient(base_url=mock_server_url, connect_timeout_s=CLIENT_TIMEOUT_S,
                       read_timeout_s=CLIENT_TIMEOUT_S) as c:
        yield c


@pytest.fixture
def staff_client(client, service_client):
    """API client whose payment/shipping clients talk to the in-test mock server."""
    app.dependency_overrides[get_service_client] = lambda: service_client
    yield client


def inject(admin: httpx.Client, kind: str, operation: str | None = None,
           delay_s: float = FAULT_DELAY_S, count: int = 1) -> None:
    admin.post("/_admin/faults", json={"kind": kind, "operation": operation,
                                       "delay_s": delay_s, "count": count}).raise_for_status()


@pytest.fixture
def agent(db, service_client):
    """Run the agent graph on a ticket with a scripted LLM against the real DB and mocks."""
    from app.agent.runner import run_agent
    from app.integrations.shipping import ShippingClient

    def run(ticket_id: str, llm):
        return run_agent(db, llm, ShippingClient(service_client), ticket_id, actor="staff:staff|priya")
    return run


@pytest.fixture(scope="session", autouse=True)
def policies_ingested():
    """Policies are reference data (like the seed). Ingesting is idempotent, so it's
    committed once per test session rather than rolled back per test."""
    from app.db.session import SessionLocal
    from app.policy.embedder import get_embedder
    from app.policy.ingest import ingest_dir
    with SessionLocal() as s:
        ingest_dir(s, get_embedder())
