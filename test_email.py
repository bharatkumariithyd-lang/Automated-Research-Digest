"""
test_email.py — Sends a minimal test digest to your recipient list.
Run this after fixing SMTP to confirm delivery before next Monday.

By default this now sends to everyone in RECIPIENT_EMAILS in your .env file,
exactly like the real pipeline does — so you can confirm all engineers
receive the email correctly before the first live run.

To test only yourself instead, set SEND_TO_SELF_ONLY = True below.

Usage:
    python test_email.py
"""

import os
import logging
from dotenv import load_dotenv
from email_sender import send_digest_email, parse_recipients

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

# -- Toggle --------------------------------------------------------------------
# True  = send test only to yourself (SENDER_EMAIL)
# False = send test to everyone in RECIPIENT_EMAILS (same as real pipeline)
SEND_TO_SELF_ONLY = False
# ------------------------------------------------------------------------------

if SEND_TO_SELF_ONLY:
    recipients = [os.getenv("SENDER_EMAIL")]
else:
    recipients = parse_recipients(os.getenv("RECIPIENT_EMAILS", ""))

if not recipients:
    print("No recipients found.")
    print("Check RECIPIENT_EMAILS in your .env file, or set SEND_TO_SELF_ONLY = True")
    raise SystemExit(1)

print(f"Sending test email to {len(recipients)} recipient(s): {recipients}")

html = """
<html><body style="font-family: Arial, sans-serif; padding: 24px;">
  <h2 style="color: #1a1a2e;">R&D Digest &mdash; Pipeline Test</h2>
  <p>If you're reading this, SMTP delivery is working correctly.</p>
  <hr>
  <p style="color: #666; font-size: 12px;">
    Sent by research-digest pipeline test script.
  </p>
</body></html>
"""

success = send_digest_email(
    subject="[TEST] R&D Digest Pipeline - SMTP Check",
    html_body=html,
    recipients=recipients,
)

if success:
    print(f"\n[OK] Test email sent to {len(recipients)} recipient(s). Check inboxes.")
else:
    print("\n[FAIL] Delivery failed. Check logs above for the specific error.")