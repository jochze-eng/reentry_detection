"""Vendor-side license signing CLI (offline).

Takes a machine fingerprint (received out-of-band from the customer) and signs a
license token bound to it. Requires the private key produced by gen_keypair.py.
NOT shipped in the Docker image (see .dockerignore).

Examples:
    # 1-year license
    python tools/license_sign.py --fingerprint mid:ab12... --customer "Acme Corp" --days 365
    # perpetual license
    python tools/license_sign.py --fingerprint dmi:cd34... --customer "Acme Corp"
"""
import argparse
import base64
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # allow `python tools/license_sign.py` from anywhere

from services.licensing import SCHEMA_VERSION, TOKEN_PREFIX


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def build_token(payload: dict, private_key: Ed25519PrivateKey) -> str:
    payload_segment = _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = private_key.sign(payload_segment.encode("ascii"))
    return f"{TOKEN_PREFIX}{payload_segment}.{_b64u_encode(signature)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign an offline node-locked license.")
    parser.add_argument("--fingerprint", required=True, help="Machine fingerprint from the customer (e.g. mid:ab12...).")
    parser.add_argument("--customer", required=True, help="Customer name.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="License valid for N days from now.")
    group.add_argument("--expiry", help="Explicit expiry (ISO 8601, e.g. 2027-07-17T00:00:00Z).")
    parser.add_argument("--limits", default="{}", help="JSON object of feature/seat limits (default: {}).")
    parser.add_argument("--private-key", default=str(REPO_ROOT / "license_priv.pem"),
                        help="Path to the signing private key.")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.days is not None:
        expires_at = (now + timedelta(days=args.days)).isoformat()
    elif args.expiry:
        expires_at = args.expiry
    else:
        expires_at = None  # perpetual

    try:
        limits = json.loads(args.limits)
        if not isinstance(limits, dict):
            raise ValueError("limits must be a JSON object")
    except ValueError as exc:
        parser.error(f"invalid --limits: {exc}")

    private_key = load_pem_private_key(Path(args.private_key).read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        parser.error("private key is not an Ed25519 key")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "license_id": str(uuid.uuid4()),
        "customer": args.customer,
        "fingerprint": args.fingerprint,
        "issued_at": now.isoformat(),
        "expires_at": expires_at,
        "limits": limits,
    }
    print(build_token(payload, private_key))


if __name__ == "__main__":
    main()
