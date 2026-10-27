class NotFound(Exception):
    """The resource doesn't exist *or* isn't the caller's. Deliberately one
    error for both, so responses never reveal that someone else's record exists."""

    def __init__(self, resource: str):
        super().__init__(f"{resource} not found")
        self.resource = resource


class Conflict(Exception):
    """The request is valid but the current state forbids it (409)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class NotEligible(Exception):
    """A policy rule in code says no (422). Carries the rule's reason code."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class ProviderUnavailable(Exception):
    """The provider refused or couldn't be reached and nothing changed (503). Safe to retry."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
