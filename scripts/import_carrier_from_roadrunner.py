"""Copy one carrier's portal login from Roadrunner's database into this project's.

Roadrunner keeps the canonical record: a `carriers` row plus a `portal` row in
`carrier_integrations` whose `config` holds url, username and the MFA toggle and
whose `secrets_encrypted` holds the password (AES-256-GCM under
RR_CRED_ENCRYPTION_KEY, or plaintext when that key is unset, as in dev). This
reads that, re-encrypts the password under our CRED_ENCRYPTION_KEY, and upserts
`carriers` + `carrier_creds` here. Secrets are never printed.

    ROADRUNNER_DATABASE_URL=postgresql://... uv run python scripts/import_carrier_from_roadrunner.py --slug thimble

Set RR_CRED_ENCRYPTION_KEY too when Roadrunner encrypts at rest. The slug written
here is Roadrunner's external_id, which is the id the shared inbox routes codes by.
"""

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from trailblazer.shared.config import get_settings
from trailblazer.shared.crypto import decrypt_secret, encrypt_secret, parse_key
from trailblazer.shared.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True, help="Roadrunner external_id, mfa_carrier_id, or carrier name")
    parser.add_argument("--from-url", default=os.environ.get("ROADRUNNER_DATABASE_URL"), help="Roadrunner's DATABASE_URL")
    args = parser.parse_args()
    if not args.from_url:
        parser.error("set ROADRUNNER_DATABASE_URL or pass --from-url")

    rr_key = parse_key(os.environ.get("RR_CRED_ENCRYPTION_KEY"))
    with psycopg.connect(args.from_url, row_factory=dict_row) as rr:
        row = rr.execute(
            """
            SELECT id, name, external_id, mfa_carrier_id, login_url, username, password
              FROM carriers
             WHERE external_id = %(s)s OR mfa_carrier_id = %(s)s OR lower(name) = lower(%(s)s)
             LIMIT 1
            """,
            {"s": args.slug},
        ).fetchone()
        if row is None:
            sys.exit(f"no carrier {args.slug!r} in Roadrunner")
        portal = rr.execute(
            """
            SELECT config, secrets_encrypted FROM carrier_integrations
             WHERE carrier_id = %s AND kind = 'portal' AND is_active LIMIT 1
            """,
            (row["id"],),
        ).fetchone()

    login_url = row["login_url"]
    username = row["username"]
    password = decrypt_secret(row["password"], rr_key) if row["password"] else None
    mfa_enabled = bool(row["mfa_carrier_id"])
    domains: list[str] = []
    if portal is not None:
        cfg = portal["config"] or {}
        login_url = cfg.get("url") or login_url
        username = cfg.get("username") or username
        mfa = cfg.get("mfa") or {}
        if isinstance(mfa, dict):
            mfa_enabled = bool(mfa.get("enabled", mfa_enabled))
            domains = [d for d in (mfa.get("domains") or []) if d]
        if portal["secrets_encrypted"]:
            raw = decrypt_secret(portal["secrets_encrypted"], rr_key) or ""
            try:
                secrets = json.loads(raw) if raw else {}
            except ValueError:
                secrets = {}
            password = secrets.get("password") or password
    if not login_url:
        sys.exit(f"{row['name']} has no login URL in Roadrunner")

    slug = row["external_id"] or row["mfa_carrier_id"] or args.slug
    settings = get_settings()
    stored = encrypt_secret(password, parse_key(settings.cred_encryption_key)) or ""
    mfa_cfg = {"enabled": mfa_enabled, "channel": "email", "domains": domains}

    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO carriers (name, slug) VALUES (%(name)s, %(slug)s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            {"name": row["name"], "slug": slug},
        )
        carrier_id = cur.fetchone()["id"]
        cur.execute("DELETE FROM carrier_creds WHERE carrier_id = %(id)s", {"id": carrier_id})
        cur.execute(
            """
            INSERT INTO carrier_creds (carrier_id, username, password, login_url, mfa)
            VALUES (%(id)s, %(username)s, %(password)s, %(login_url)s, %(mfa)s::jsonb)
            """,
            {
                "id": carrier_id,
                "username": (username or "").strip(),
                "password": stored,
                "login_url": login_url.strip(),
                "mfa": json.dumps(mfa_cfg),
            },
        )
        conn.commit()

    print(
        f"imported {row['name']} as {slug}: login_url={login_url} username={username} "
        f"password={'stored' if password else 'none'} mfa={'on' if mfa_enabled else 'off'} "
        f"encrypted={'yes' if settings.cred_encryption_key else 'no (CRED_ENCRYPTION_KEY unset)'}"
    )


if __name__ == "__main__":
    main()
