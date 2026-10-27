"""FastAPI dependencies for external clients. Tests override get_service_client
to point every client at the in-test mock server."""

from collections.abc import Iterator

from fastapi import Depends

from app.integrations.http import ServiceClient
from app.integrations.payment import PaymentClient
from app.integrations.shipping import ShippingClient


def get_service_client() -> Iterator[ServiceClient]:
    with ServiceClient() as client:
        yield client


def get_payment_client(http: ServiceClient = Depends(get_service_client)) -> PaymentClient:
    return PaymentClient(http)


def get_shipping_client(http: ServiceClient = Depends(get_service_client)) -> ShippingClient:
    return ShippingClient(http)
