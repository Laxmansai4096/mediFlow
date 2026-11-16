"""Print the current MFA code for a LOCAL demo staff account (acts as your authenticator app).

    python scripts/totp.py dr.rao
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediflow.common import config  # noqa: E402
from mediflow.common.passwords import totp_code  # noqa: E402

secrets = json.loads((config.data_dir() / ".demo_mfa.json").read_text())
user = sys.argv[1] if len(sys.argv) > 1 else "admin"
if user not in secrets:
    sys.exit(f"unknown demo user; choose one of: {', '.join(secrets)}")
print(totp_code(secrets[user]))
