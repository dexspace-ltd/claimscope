"""Random values the demo stack needs to keep stable between deploys. Generated once, kept out of git."""

import json
import secrets
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / ".demo-secrets.json"


def load_demo_secrets() -> dict:
    values = json.loads(PATH.read_text()) if PATH.exists() else {}
    changed = False
    for name in ("origin_verify", "ip_hash_salt"):
        if not values.get(name):
            values[name] = secrets.token_urlsafe(32)
            changed = True
    if changed:
        PATH.write_text(json.dumps(values, indent=1))
        PATH.chmod(0o600)
    return values
