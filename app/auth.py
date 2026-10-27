"""Customer authentication.

Identity comes from a verified token's 'sub' claim, matched to
customers.external_auth_subject. Nothing in the request body or message
text can change who the caller is.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Customer
from app.db.session import get_db

# auto_error=False so a missing header gets the same 401 as a bad token,
# instead of FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def issue_token(subject: str, ttl_minutes: int | None = None) -> str:
    """Mint a token the way a dev identity provider would. Used by
    scripts/mint_token.py and tests; a real deployment gets tokens from its IdP."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes or settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_current_customer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Customer:
    """FastAPI dependency: the authenticated customer, or 401.

    Every failure returns the same response, so a caller can't tell
    'bad signature' from 'unknown user'."""
    if creds is None:
        raise _UNAUTHORIZED
    try:
        claims = jwt.decode(
            creds.credentials,
            settings.jwt_secret,
            # Pinned list: rejects alg=none and algorithm-switching tokens.
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        raise _UNAUTHORIZED

    customer = db.scalar(select(Customer).where(Customer.external_auth_subject == claims["sub"]))
    if customer is None:
        # Never auto-create: a valid signature for an unknown subject is still not a customer.
        raise _UNAUTHORIZED
    return customer
