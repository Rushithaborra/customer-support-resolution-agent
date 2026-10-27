import hashlib
import json


def payload_hash(payload: dict) -> str:
    """SHA-256 of canonical JSON. Key order and whitespace don't change the hash;
    any change to a value does, which invalidates an approval bound to it."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
