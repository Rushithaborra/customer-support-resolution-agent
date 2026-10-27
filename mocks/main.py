from fastapi import FastAPI

from mocks import admin, helpdesk, payment, shipping

app = FastAPI(title="Mock external services", version="0.1.0")
app.include_router(payment.router)
app.include_router(shipping.router)
app.include_router(helpdesk.router)
app.include_router(admin.router)
