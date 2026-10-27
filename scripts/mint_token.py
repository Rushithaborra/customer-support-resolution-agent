"""Print a dev JWT for a seeded customer, for curl or the /docs "Authorize" button.

Run:  uv run python -m scripts.mint_token "auth|asha"
      uv run python -m scripts.mint_token "auth|vikram"
"""

import sys

from app.auth import issue_token

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m scripts.mint_token "auth|asha"')
    print(issue_token(sys.argv[1]))
