"""Identity comes only from a valid, signed, unexpired token for a known customer."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ASHA, as_customer, auth, token_for, unsigned_token

PAST = datetime.now(timezone.utc) - timedelta(minutes=1)

BAD_HEADERS = {
    "missing header": {},
    "not a bearer token": {"Authorization": "Basic YXNoYTpwdw=="},
    "malformed token": auth("not-a-jwt"),
    "wrong signing secret": auth(token_for(ASHA, secret="some-other-secret-some-other-secret")),
    "expired": auth(token_for(ASHA, exp=PAST)),
    "wrong audience": auth(token_for(ASHA, aud="another-api")),
    "wrong issuer": auth(token_for(ASHA, iss="evil-idp")),
    "alg=none": auth(unsigned_token(ASHA)),
    "unknown subject": auth(token_for("auth|nobody")),
}


@pytest.mark.parametrize("headers", BAD_HEADERS.values(), ids=BAD_HEADERS.keys())
def test_bad_credentials_get_401(client, headers):
    r = client.get("/me", headers=headers)
    assert r.status_code == 401
    # One generic message for every failure: no hint about which check failed.
    assert r.json() == {"detail": "Invalid or missing credentials"}


def test_valid_token_identifies_customer(client):
    r = client.get("/me", headers=as_customer(ASHA))
    assert r.status_code == 200
    assert r.json()["id"] == "cus_asha"


def test_every_customer_route_requires_auth(client):
    for method, path in [("get", "/orders"), ("get", "/orders/ord_1001"),
                         ("get", "/tickets"), ("get", "/tickets/tkt_x"), ("post", "/tickets")]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, (method, path)
