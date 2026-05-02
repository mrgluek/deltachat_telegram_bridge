#!/usr/bin/env python3
import sys
import argparse
import database

def main():
    parser = argparse.ArgumentParser(description="Secure Admin Management for Telegram Bridge Bot")
    parser.add_argument("--email", help="Set the administrator's email address")
    parser.add_argument("--fingerprint", help="Set the administrator's cryptographic fingerprint")
    parser.add_argument("--reset", action="store_true", help="Completely clear admin credentials")

    args = parser.parse_args()

    if not any([args.email, args.fingerprint, args.reset]):
        parser.print_help()
        return

    # Initialize database
    database.init_db()

    if args.reset:
        database.set_config("admin_dc_email", "")
        database.set_config("admin_dc_fingerprint", "")
        print("✅ Admin credentials have been completely cleared.")
        return

    if args.email:
        database.set_config("admin_dc_email", args.email)
        print(f"✅ Admin email set to: {args.email}")

    if args.fingerprint:
        fp = args.fingerprint.strip().upper()
        database.set_config("admin_dc_fingerprint", fp)
        print(f"✅ Admin fingerprint set to: {fp}")

if __name__ == "__main__":
    main()
