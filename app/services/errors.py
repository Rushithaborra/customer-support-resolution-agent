class NotFound(Exception):
    """The resource doesn't exist *or* isn't the caller's. Deliberately one
    error for both, so responses never reveal that someone else's record exists."""

    def __init__(self, resource: str):
        super().__init__(f"{resource} not found")
        self.resource = resource
