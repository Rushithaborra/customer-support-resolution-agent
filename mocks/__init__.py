"""Mock external services: payment provider, shipping carrier, helpdesk.

These stand in for *other companies'* systems, so they live outside app/ and
never import from it. Run:  uv run uvicorn mocks.main:app --port 8001
"""
