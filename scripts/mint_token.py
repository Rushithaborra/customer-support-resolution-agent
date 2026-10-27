"""Print a dev JWT, for curl or the /docs "Authorize" button.

Run:  uv run python -m scripts.mint_token "auth|asha"
      uv run python -m scripts.mint_token "auth|vikram"
      uv run python -m scripts.mint_token --staff "staff|priya"
"""

import sys

from app.auth import STAFF_ROLE, issue_token

if __name__ == "__main__":
    args = sys.argv[1:]
    role = None
    if args[:1] == ["--staff"]:
        role, args = STAFF_ROLE, args[1:]
    if len(args) != 1:
        sys.exit('usage: python -m scripts.mint_token [--staff] "auth|asha"')
    print(issue_token(args[0], role=role))
