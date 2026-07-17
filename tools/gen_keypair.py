"""One-time vendor keypair generation for license signing.

Generates an Ed25519 keypair:
  - keys/license_pub.pem  -> committed & shipped in the Docker image (public).
  - license_priv.pem      -> KEEP THIS SECRET. Never commit or ship it.

Run once:  python tools/gen_keypair.py
Re-running refuses to clobber an existing private key unless --force is given.
"""
import argparse
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a license signing keypair.")
    parser.add_argument("--private-out", default=str(REPO_ROOT / "license_priv.pem"),
                        help="Path to write the PRIVATE key (default: repo root, gitignored).")
    parser.add_argument("--public-out", default=str(REPO_ROOT / "keys" / "license_pub.pem"),
                        help="Path to write the public key (default: keys/license_pub.pem).")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing private key.")
    args = parser.parse_args()

    private_out = Path(args.private_out)
    public_out = Path(args.public_out)

    if private_out.exists() and not args.force:
        parser.error(f"{private_out} already exists. Use --force to overwrite (this invalidates all issued licenses).")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    public_out.parent.mkdir(parents=True, exist_ok=True)
    private_out.write_bytes(private_pem)
    public_out.write_bytes(public_pem)

    print(f"Private key written to: {private_out}  (KEEP SECRET — do not commit)")
    print(f"Public key written to:  {public_out}  (commit & ship this)")


if __name__ == "__main__":
    main()
