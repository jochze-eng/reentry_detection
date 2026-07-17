"""Standalone verification of the offline license crypto core (Phase 1).

Proves sign -> verify roundtrip, tamper rejection, wrong-machine rejection, and
expiry handling without needing the app or a database. Uses an ephemeral keypair
and a fixed fingerprint source so it runs anywhere (no Linux host IDs required).

Run:  python tests/test_licensing.py     (exit 0 = all passed)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the repo root importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Deterministic, host-independent fingerprint source before importing licensing.
os.environ["RTD_FINGERPRINT_SOURCE"] = "mid:test-machine-0001"

from services import licensing
from services.licensing import LicenseError, compute_fingerprint, verify_license
from tools.license_sign import build_token

_tmp = tempfile.mkdtemp(prefix="rtd-lic-test-")
_signing_key = Ed25519PrivateKey.generate()


def _install_public_key(key: Ed25519PrivateKey) -> None:
    pub_path = Path(_tmp) / "pub.pem"
    pub_path.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    os.environ["RTD_LICENSE_PUBKEY"] = str(pub_path)


def _payload(fingerprint: str, expires_at):
    return {
        "schema_version": licensing.SCHEMA_VERSION,
        "license_id": "test-license-id",
        "customer": "Test Corp",
        "fingerprint": fingerprint,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "limits": {},
    }


_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


def main() -> None:
    _install_public_key(_signing_key)
    fp = compute_fingerprint()

    check("fingerprint is deterministic", fp == compute_fingerprint())
    check("fingerprint carries source tag", fp.startswith("mid:"))

    # 1. Valid perpetual license on this machine.
    tok = build_token(_payload(fp, None), _signing_key)
    info = verify_license(tok)
    check("valid perpetual: fingerprint_ok", info.fingerprint_ok)
    check("valid perpetual: not expired", not info.expired)
    check("valid perpetual: no expiry date", info.expires_at is None)

    # 2. Valid future-dated license.
    future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    info = verify_license(build_token(_payload(fp, future), _signing_key))
    check("future-dated: not expired", not info.expired)
    check("future-dated: days_left ~ 364", info.days_left is not None and 360 <= info.days_left <= 365)

    # 3. Expired license.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    info = verify_license(build_token(_payload(fp, past), _signing_key))
    check("expired license: expired flag set", info.expired)
    check("expired license: signature still valid (no raise)", info.fingerprint_ok)

    # 4. License bound to a different machine.
    info = verify_license(build_token(_payload("mid:some-other-box", None), _signing_key))
    check("wrong machine: fingerprint_ok False", not info.fingerprint_ok)

    # 5. Tampered payload -> signature fails.
    seg, _, sig = tok[len(licensing.TOKEN_PREFIX):].partition(".")
    flipped = ("A" if seg[0] != "A" else "B") + seg[1:]
    tampered = f"{licensing.TOKEN_PREFIX}{flipped}.{sig}"
    check("tampered payload rejected", _raises(lambda: verify_license(tampered)))

    # 6. Garbage / unrecognized format.
    check("garbage token rejected", _raises(lambda: verify_license("not-a-license")))

    # 7. Signature from a different (attacker) key rejected.
    attacker_key = Ed25519PrivateKey.generate()
    forged = build_token(_payload(fp, None), attacker_key)
    check("foreign-signed token rejected", _raises(lambda: verify_license(forged)))

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except LicenseError:
        return True


if __name__ == "__main__":
    main()
