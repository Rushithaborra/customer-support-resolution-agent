"""Authentication for customers and staff.

Identity comes from a verified token's claims, never from the request body
or message text. Customers: 'sub' matched to customers.external_auth_subject.
Staff: a token carrying role="staff"; its 'sub' becomes the reviewer id.
"""

from dataclasses import dataclass
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
_FORBIDDEN = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required")
_REVIEWER_ONLY = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")

STAFF_ROLE = "staff"        # operates tickets: classify, run the agent, propose, execute
REVIEWER_ROLE = "reviewer"  # decides: approve / reject proposals (and may read staff views)


@dataclass(frozen=True)
class Staff:
    subject: str  # e.g. "staff|priya"; stored as reviewer_id / audit actor
    role: str = STAFF_ROLE

    @property
    def actor(self) -> str:
        return f"{self.role}:{self.subject}"


def issue_token(subject: str, ttl_minutes: int | None = None, role: str | None = None) -> str:
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
    if role:
        claims["role"] = role
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode(creds: HTTPAuthorizationCredentials | None) -> dict:
    if creds is None:
        raise _UNAUTHORIZED
    try:
        return jwt.decode(
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


def get_current_customer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Customer:
    """FastAPI dependency: the authenticated customer, or 401.

    Every failure returns the same response, so a caller can't tell
    'bad signature' from 'unknown user'."""
    claims = _decode(creds)
    customer = db.scalar(select(Customer).where(Customer.external_auth_subject == claims["sub"]))
    if customer is None:
        # Never auto-create: a valid signature for an unknown subject is still not a customer.
        raise _UNAUTHORIZED
    return customer


def get_current_staff(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Staff:
    """FastAPI dependency: 401 if not authenticated, 403 if not staff or reviewer."""
    claims = _decode(creds)
    if claims.get("role") not in (STAFF_ROLE, REVIEWER_ROLE):
        raise _FORBIDDEN
    return Staff(subject=claims["sub"], role=claims["role"])


def get_current_reviewer(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Staff:
    """Only reviewers may approve or reject. Separating the roles means operating a
    ticket and authorising its money are different permissions."""
    reviewer = get_current_staff(creds)
    if reviewer.role != REVIEWER_ROLE:
        raise _REVIEWER_ONLY
    return reviewer
