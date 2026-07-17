# License signing keys

The app verifies licenses offline using an Ed25519 **public** key stored here as
`license_pub.pem`. The matching **private** key is a vendor secret and must never
be committed or shipped.

## One-time setup (vendor)

```bash
python tools/gen_keypair.py
```

This writes:
- `keys/license_pub.pem` — **commit this**; it ships in the Docker image.
- `license_priv.pem` (repo root) — **keep secret**; gitignored and dockerignored.

Store `license_priv.pem` somewhere safe (password manager / secrets vault).
Regenerating the keypair invalidates every license already issued.

## Issuing a license

```bash
python tools/license_sign.py --fingerprint <customer-fingerprint> --customer "Acme Corp" --days 365
```

The public key location can be overridden at runtime with `RTD_LICENSE_PUBKEY`
(used for key rotation and tests).
