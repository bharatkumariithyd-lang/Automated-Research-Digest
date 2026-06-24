"""
smtp_debug.py — Run this before anything else.
It tells you exactly where Outlook SMTP is failing.

Usage:
    python smtp_debug.py
"""

import smtplib
import ssl
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL    = os.getenv("SENDER_EMAIL")
PASSWORD = os.getenv("SENDER_PASSWORD")

print(f"[1] Loaded email: {EMAIL}")
print(f"[2] Password loaded: {'YES' if PASSWORD else 'NO — check .env'}")
print()

# ── Test 1: Basic TCP connection ─────────────────────────────────────────────
print("[TEST 1] Connecting to smtp.office365.com:587 ...")
try:
    with smtplib.SMTP("smtp.office365.com", 587, timeout=15) as s:
        print("         Connected OK")

        # ── Test 2: EHLO ──────────────────────────────────────────────────────
        print("[TEST 2] Sending EHLO ...")
        code, msg = s.ehlo()
        print(f"         EHLO response: {code} — {msg.decode()}")

        # Check if AUTH is listed in capabilities
        # If SMTP AUTH is disabled by IT, AUTH won't appear here
        caps = s.esmtp_features
        print(f"[TEST 3] Server capabilities: {list(caps.keys())}")
        if "auth" in caps:
            print("         AUTH advertised — SMTP AUTH is ENABLED on this account ✓")
        else:
            print("         AUTH not advertised — SMTP AUTH is DISABLED")
            print("         >>> Tell IT: 'Please enable SMTP AUTH for", EMAIL, "'")
            print("         >>> IT portal: Microsoft 365 Admin → Users → Active Users")
            print("         >>>            → select user → Mail → Manage email apps")
            print("         >>>            → tick 'Authenticated SMTP'")

        # ── Test 3: STARTTLS ─────────────────────────────────────────────────
        print("[TEST 4] Starting TLS ...")
        s.starttls(context=ssl.create_default_context())
        print("         TLS started OK")

        # ── Test 4: Login ─────────────────────────────────────────────────────
        print("[TEST 5] Attempting login ...")
        s.login(EMAIL, PASSWORD)
        print("         Login SUCCESSFUL ✓")
        print()
        print("=== Outlook SMTP is fully working. Run pipeline.py ===")

except smtplib.SMTPAuthenticationError as e:
    print(f"\n[FAIL] Authentication error: {e}")
    print()
    print("Most likely causes (in order):")
    print("  1. SMTP AUTH not enabled for this account in Microsoft 365 Admin")
    print("     Fix: Ask IT to enable 'Authenticated SMTP' for your sender account")
    print("  2. MFA is enforced — plain password won't work")
    print("     Fix: Ask IT to exclude sender account from MFA, or use app password")
    print("  3. Wrong password in .env")
    print("     Fix: Re-check SENDER_PASSWORD in .env (no quotes needed)")

except smtplib.SMTPConnectError as e:
    print(f"\n[FAIL] Cannot connect: {e}")
    print("       Corporate firewall is blocking smtp.office365.com:587")
    print("       Fix: Ask IT to whitelist smtp.office365.com on port 587")

except TimeoutError:
    print("\n[FAIL] Connection timed out")
    print("       Port 587 is blocked on your network")
    print("       Fix: Try from home network to isolate. If home works, it's the firewall")

except Exception as e:
    print(f"\n[FAIL] Unexpected error: {type(e).__name__}: {e}")
