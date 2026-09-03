"""Add or update a carrier and its portal login in the project database.

The password is encrypted with CRED_ENCRYPTION_KEY before it is stored, which is
why this exists instead of a SQL snippet: an encrypted value cannot be typed by
hand. The password is read from a prompt (or --password-stdin), never from argv,
so it does not land in shell history.

    uv run python scripts/upsert_carrier_creds.py --slug thimble --name "Thimble" \
        --login-url https://app.thimble.com/login --username agent@aidenrisk.com \
        --mfa --mfa-domain thimble.com

Omit --username/--password for a portal with no login; pass --mfa with no
password for an email-OTP-only portal.
"""

import argparse
import getpass
import json
import sys

from trailblazer.shared.config import get_settings
from trailblazer.shared.crypto import encrypt_secret, parse_key
from trailblazer.shared.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True, help="canonical carrier slug; also the MFA inbox key")
    parser.add_argument("--name", help="display name (defaults to the slug)")
    parser.add_argument("--login-url", required=True)
    parser.add_argument("--username", default="")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    parser.add_argument("--no-password", action="store_true", help="portal has no password (OTP-only or open)")
    parser.add_argument("--mfa", action="store_true", help="portal challenges with a one-time code")
    parser.add_argument(
        "--mfa-channel",
        choices=["email", "totp", "manual"],
        default="email",
        help="where the code comes from: the shared inbox (email), an enrolled authenticator seed (totp), or an operator's file drop (manual)",
    )
    parser.add_argument("--mfa-domain", action="append", default=[], help="sender domain the inbox routes by; repeatable")
    parser.add_argument("--totp-seed-stdin", action="store_true", help="read the base32 authenticator seed from stdin (totp only)")
    args = parser.parse_args()

    if args.no_password:
        password = ""
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("portal password (empty for none): ")

    settings = get_settings()
    key = parse_key(settings.cred_encryption_key)
    stored = encrypt_secret(password, key) or ""
    mfa = {"enabled": args.mfa, "channel": args.mfa_channel, "domains": args.mfa_domain}
    if args.mfa_channel == "totp":
        seed = sys.stdin.readline().strip() if args.totp_seed_stdin else getpass.getpass("authenticator seed (base32): ")
        if not seed:
            parser.error("--mfa-channel totp needs the enrolled seed")
        mfa["totp_secret"] = encrypt_secret(seed, key)  # a secret like the password, stored the same way

    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO carriers (name, slug) VALUES (%(name)s, %(slug)s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            {"name": args.name or args.slug, "slug": args.slug},
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
                "username": args.username,
                "password": stored,
                "login_url": args.login_url.strip(),
                "mfa": json.dumps(mfa),
            },
        )
        conn.commit()

    print(f"stored credentials for {args.slug} (carrier id {carrier_id}, mfa={'on' if args.mfa else 'off'})")


if __name__ == "__main__":
    main()
