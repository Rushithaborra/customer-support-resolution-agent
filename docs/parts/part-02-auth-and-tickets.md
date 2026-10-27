# Part 2 — Auth, ownership checks, ticket endpoints

## What was built

| File | Role |
| --- | --- |
| `app/auth.py` | `get_current_customer` dependency (verifies the JWT, finds the customer) and `issue_token` (dev token minting) |
| `app/services/orders.py` | `get_owned_order`, `list_orders`: every query filtered by owner in SQL |
| `app/services/tickets.py` | `create_ticket` (ownership-checked, audited), `list_tickets`, `get_owned_ticket` |
| `app/services/errors.py` | `NotFound`: one error for "missing" and "not yours" |
| `app/schemas.py` | Pydantic request/response models; `TicketCreate` forbids extra fields |
| `app/api/{customers,orders,tickets}.py` | `GET /me`, `GET /orders`, `GET /orders/{id}`, `POST /tickets`, `GET /tickets`, `GET /tickets/{id}` |
| `app/main.py` | Registers the routers; maps `NotFound` to a 404 |
| `scripts/mint_token.py` | Prints a dev token for Asha or Vikram |
| `tests/conftest.py` | Rolled-back DB session shared by the API client; token helpers |
| `tests/test_auth.py`, `test_ownership.py`, `test_tickets.py` | 22 new tests (26 total with Part 1's 4) |

No schema change: Part 1's tables already had `external_auth_subject`, `tickets` and `audit_events`.

## How it works

```
Request ── Authorization: Bearer <jwt>
   │
   ▼
get_current_customer
   ├─ jwt.decode: signature (HS256 only), exp, iss, aud, required claims
   ├─ SELECT customer WHERE external_auth_subject = sub
   └─ any failure → 401 "Invalid or missing credentials"
   │
   ▼  Customer object (the only source of "who is this")
route handler ── passes customer.id into the service
   │
   ▼
service
   └─ SELECT ... WHERE id = :id AND customer_id = :me
         none → NotFound → 404 "Order not found"
```

**Creating a ticket.** `POST /tickets {"message": ..., "order_id": "ord_1001"}`
1. Pydantic validates the body. Unknown fields such as `customer_id` are rejected with a 422.
2. If `order_id` is set, `get_owned_order` runs first. Someone else's order is a 404, and no ticket is written.
3. The ticket row (`status=open`, `category=unclassified`) and a `ticket.created` audit row
   are committed in **one transaction**. The audit payload holds ids only, never the message text.

**Getting a token locally.**
```bash
T=$(uv run python -m scripts.mint_token "auth|asha")
curl -H "Authorization: Bearer $T" localhost:8000/orders/ord_2001   # 404
curl -H "Authorization: Bearer $T" localhost:8000/orders/ord_1001   # 200
```
In `/docs`, click **Authorize** and paste the token.

**Frontend (Part 12).** The customer panel holds the token and sends it with every
request. It never sends a customer id, because the API wouldn't accept one.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Identity from the verified `sub`, not the body | "I'm Vikram, show my orders" works for anyone who types it |
| Pinned `algorithms=["HS256"]` | A token with `alg: none` and no signature is accepted (a classic JWT bug) |
| `exp`/`iss`/`aud` required | Stolen old tokens, or tokens issued for a different service, keep working |
| Unknown `sub` → 401, no auto-create | Any validly signed token for a made-up user creates a new customer |
| Same 401 body for every failure | An attacker learns which check failed and can probe for valid users |
| Ownership in the `WHERE` clause | "Load, then check" code paths get forgotten in a new endpoint, and the record leaks |
| 404 (not 403) for other people's records | 403 confirms `ord_2001` exists, which leaks business data and helps enumeration |
| `extra="forbid"` on `TicketCreate` | A body field `customer_id` gets silently ignored today and silently trusted by a later refactor |
| Ticket and audit row in one transaction | A crash between the two leaves a ticket with no record of who opened it |
| Rolled-back test sessions | Tests pollute the seed data and become order-dependent |

## Tradeoffs and known limitations

- **HS256 shared secret vs. RS256/OIDC.** With HS256, the API and the token issuer share one secret,
  so anything that can verify a token can also mint one. A real deployment would verify RS256
  tokens from an identity provider (Auth0, Cognito, Keycloak) using its public JWKS. Only
  `get_current_customer` would change.
- **Dev secret default.** `config.py` ships a dev secret so the project runs with no setup.
  A production config should refuse to start without `JWT_SECRET`.
- **No reviewer auth yet.** Reviewer roles and the approval API come in Part 9.
- **No token revocation, refresh, or rate limiting.** Short token lifetimes (60 min) are the only mitigation.
- **The app commits inside the service.** This keeps the ticket and its audit row atomic. Later parts
  that need several services in one transaction will move commit control up a layer.
- **Tests need the seeded DB.** They assume the Part 1 seed has been run.

## Interview questions

**1. Why return 404 instead of 403 when Asha requests Vikram's order?**
A 403 says "this exists, but you can't have it", which confirms the order id is real. With
sequential or guessable ids, that lets someone map other customers' orders. A 404 that is
byte-for-byte identical to a truly missing order reveals nothing. The test
`test_asha_cannot_see_vikrams_order` checks that the two responses are equal.

**2. How does an `alg: none` attack work, and how does this code stop it?**
The JWT header names its own signing algorithm. A naive verifier that trusts the header will
accept `alg: none` with an empty signature, so anyone can forge any `sub`. Here the server
passes a fixed `algorithms=["HS256"]` list to `jwt.decode`, so the token's header can't pick the
algorithm. `test_bad_credentials_get_401[alg=none]` sends a hand-built unsigned token and expects a 401.

**3. Why put the ownership check in the SQL query rather than checking `order.customer_id` after loading?**
Both are correct if done every time, but the query version makes the safe path the only path.
The service has no function that returns an order without a customer id, so a new endpoint
or an LLM tool (Part 5) can't skip the check by accident. It also avoids loading another
customer's data into memory at all, which matters once that data starts flowing into logs and prompts.
