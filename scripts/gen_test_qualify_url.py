"""Generate a valid qualification booking URL for manual testing.

Usage:
    python scripts/gen_test_qualify_url.py <contact_id>

Reads CFS_ROUTER_SECRET from .env, signs a URL token for the given contact_id,
and prints the full URL you can paste in a browser. Token TTL is 14 days.

Example:
    python scripts/gen_test_qualify_url.py 9Jec0JOrKXTIINcAgnpn
    → https://reply-router.vercel.app/v1/clients/clear_facility/qualify/9Jec0JOrKXTIINcAgnpn?token=<...>
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

from reply_router.qualifier import url_token

load_dotenv()


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/gen_test_qualify_url.py <contact_id> [base_url]")
    contact_id = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) >= 3 else "https://reply-router.vercel.app"
    secret = os.environ.get("CFS_ROUTER_SECRET")
    if not secret:
        sys.exit("ERROR: CFS_ROUTER_SECRET not set (check .env)")
    tok = url_token(secret, contact_id, int(time.time()))
    url = f"{base}/v1/clients/clear_facility/qualify/{contact_id}?token={tok}"
    print(url)


if __name__ == "__main__":
    main()
