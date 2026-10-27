"""Carrier tracking data matching the seed scenarios in scripts/seed_data.py."""

from datetime import datetime, timedelta, timezone


def _days(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _in_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _return(tracking: str, status: str, delivered_at: str | None) -> dict:
    return {"tracking_number": tracking, "status": status, "delivered_at": delivered_at}


SHIPMENTS: dict[str, dict] = {
    "ord_1001": {"carrier": "SwiftShip", "tracking_number": "SS100100", "status": "delivered",
                 "eta": None, "delivered_at": _days(12),
                 "return_shipment": _return("SSR100100", "delivered", _days(3))},
    "ord_1002": {"carrier": "SwiftShip", "tracking_number": "SS100200", "status": "delivered",
                 "eta": None, "delivered_at": _days(6),
                 "return_shipment": _return("SSR100200", "in_transit", None)},
    "ord_1003": {"carrier": "SwiftShip", "tracking_number": "SS100300", "status": "in_transit",
                 "eta": _in_days(2), "delivered_at": None, "return_shipment": None},
    "ord_1004": {"carrier": "SwiftShip", "tracking_number": "SS100400", "status": "delivered",
                 "eta": None, "delivered_at": _days(2), "return_shipment": None},
    "ord_2001": {"carrier": "SwiftShip", "tracking_number": "SS200100", "status": "delivered",
                 "eta": None, "delivered_at": _days(40),
                 "return_shipment": _return("SSR200100", "delivered", _days(5))},
}
