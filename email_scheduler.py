import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, date
import os
import time
import json
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
import httpx
import jwt

from zoneinfo import ZoneInfo

# Constants for email notifications
CONTACT_EMAILS = ["jyotigupta_mail@yahoo.com"]
CHECK_HOUR = 22  # 10 PM - When to run the daily check
CHECK_MINUTE = 0
PUSH_CHECK_HOUR = 5  # 5 AM - When to send push notification to lock phone
PUSH_CHECK_MINUTE = 0

# APNs Configuration (set via environment variables)
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.aayush.god")
# Path to .p8 key file, or the key content directly
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")
APNS_KEY_CONTENT = os.environ.get("APNS_KEY_CONTENT", "")  # Alternative: key as env var
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "false").lower() == "true"

# Database path - use DB_PATH env var if set, otherwise default to script directory
# IMPORTANT: On servers with expiring home directory permissions (Kerberos/LDAP),
# set DB_PATH to a persistent location like /var/lib/habit-tracker/foodlog.db
_THIS_DIR = Path(__file__).resolve().parent
DB_NAME = os.environ.get("DB_PATH", str(_THIS_DIR / "foodlog.db"))
STATE_KEY_NEXT_DUE_DATE = "next_due_date"

print(f"[email_scheduler] Using database: {DB_NAME}")

# Day boundary hour - can be changed here and will propagate to iOS via /check-status
DAY_START_HOUR = 5
# Timezone identifier - will be sent to iOS to ensure consistency
TIMEZONE_NAME = "America/Los_Angeles"
SF_TZ = ZoneInfo(TIMEZONE_NAME)

def get_logical_date(dt: datetime = None) -> str:
    """
    Get the 'logical date' for food logging purposes.
    Days start at 5am, so 4am on Dec 19 is still 'Dec 18'.
    """
    if dt is None:
        dt = datetime.now(SF_TZ)
    
    # If before 5am, it's still "yesterday" for logging purposes
    if dt.hour < DAY_START_HOUR:
        dt = dt - timedelta(days=1)
    
    return dt.strftime("%Y-%m-%d")

def get_logical_yesterday(dt: datetime = None) -> str:
    """Get yesterday's logical date."""
    if dt is None:
        dt = datetime.now(SF_TZ)
    return get_logical_date(dt - timedelta(days=1))

def get_due_logical_date(dt: datetime = None) -> str:
    """
    Get the single date the server will currently accept for submission.

    Rule (matches "deadline 5am"):
    - Before 5:00am local time, you're still submitting for the current logical date.
    - At/after 5:00am local time, you're submitting for the previous logical date (i.e., the day that ended at 5am).
    """
    if dt is None:
        dt = datetime.now(SF_TZ)

    if dt.hour < DAY_START_HOUR:
        return get_logical_date(dt)

    return get_logical_yesterday(dt)

# Database helpers
def _get_db_connection():
    """Get a database connection with better error handling."""
    try:
        # Verify the parent directory exists
        db_path = Path(DB_NAME)
        if not db_path.parent.exists():
            raise RuntimeError(f"Database directory does not exist: {db_path.parent}")
        return sqlite3.connect(DB_NAME)
    except sqlite3.OperationalError as e:
        # Log detailed info for debugging
        print(f"ERROR: Failed to open database at {DB_NAME}")
        print(f"  - Path exists: {Path(DB_NAME).exists()}")
        print(f"  - Parent dir exists: {Path(DB_NAME).parent.exists()}")
        print(f"  - Current working directory: {os.getcwd()}")
        print(f"  - __file__ resolved to: {Path(__file__).resolve()}")
        raise

def init_db():
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS uploads
                     (date text, timestamp text)''')
        # Enforce at most one successful upload per date. Keeps behavior idempotent.
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_date ON uploads(date)''')
        c.execute('''CREATE TABLE IF NOT EXISTS image_hashes
                     (hash text PRIMARY KEY, date text, timestamp text)''')
        c.execute('''CREATE TABLE IF NOT EXISTS state
                     (key text PRIMARY KEY, value text)''')
        # Device tokens for push notifications
        c.execute('''CREATE TABLE IF NOT EXISTS device_tokens
                     (token text PRIMARY KEY, created_at text, last_used text)''')
        conn.commit()

# ============================================================================
# PUSH NOTIFICATION FUNCTIONS
# ============================================================================

def register_device_token(token: str) -> bool:
    """Register or update a device token for push notifications."""
    if not token:
        return False
    with _get_db_connection() as conn:
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO device_tokens (token, created_at, last_used)
            VALUES (?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET last_used = ?
        """, (token, now, now, now))
        conn.commit()
    print(f"[push] Registered device token: {token[:20]}...")
    return True

def get_all_device_tokens() -> list[str]:
    """Get all registered device tokens."""
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT token FROM device_tokens")
        rows = c.fetchall()
    return [row[0] for row in rows]

def remove_device_token(token: str):
    """Remove an invalid device token."""
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM device_tokens WHERE token = ?", (token,))
        conn.commit()
    print(f"[push] Removed invalid token: {token[:20]}...")

def _get_apns_auth_token() -> str | None:
    """Generate a JWT for APNs authentication."""
    if not APNS_KEY_ID or not APNS_TEAM_ID:
        print("[push] APNs not configured (missing KEY_ID or TEAM_ID)")
        return None
    
    # Load the private key
    private_key = None
    if APNS_KEY_CONTENT:
        private_key = APNS_KEY_CONTENT
    elif APNS_KEY_PATH and os.path.exists(APNS_KEY_PATH):
        with open(APNS_KEY_PATH, 'r') as f:
            private_key = f.read()
    
    if not private_key:
        print("[push] APNs not configured (missing private key)")
        return None
    
    # Create JWT token
    token = jwt.encode(
        {
            "iss": APNS_TEAM_ID,
            "iat": int(time.time())
        },
        private_key,
        algorithm="ES256",
        headers={
            "alg": "ES256",
            "kid": APNS_KEY_ID
        }
    )
    return token

def send_push_notification(device_token: str, title: str, body: str, data: dict = None) -> bool:
    """Send a push notification to a single device."""
    auth_token = _get_apns_auth_token()
    if not auth_token:
        return False
    
    # APNs endpoint
    if APNS_USE_SANDBOX:
        url = f"https://api.sandbox.push.apple.com/3/device/{device_token}"
    else:
        url = f"https://api.push.apple.com/3/device/{device_token}"
    
    # Build payload
    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body
            },
            "sound": "default",
            "content-available": 1  # Enable background processing
        }
    }
    if data:
        payload["data"] = data
    
    headers = {
        "authorization": f"bearer {auth_token}",
        "apns-topic": APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10"
    }
    
    try:
        # Use HTTP/2 client
        with httpx.Client(http2=True) as client:
            response = client.post(url, json=payload, headers=headers)
            
            if response.status_code == 200:
                print(f"[push] Sent notification to {device_token[:20]}...")
                return True
            elif response.status_code == 410:
                # Token is no longer valid
                print(f"[push] Token expired/invalid: {device_token[:20]}...")
                remove_device_token(device_token)
                return False
            else:
                print(f"[push] Failed to send: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        print(f"[push] Error sending notification: {e}")
        return False

def send_push_to_all(title: str, body: str, data: dict = None) -> int:
    """Send a push notification to all registered devices. Returns count of successful sends."""
    tokens = get_all_device_tokens()
    if not tokens:
        print("[push] No device tokens registered")
        return 0
    
    success_count = 0
    for token in tokens:
        if send_push_notification(token, title, body, data):
            success_count += 1
    
    print(f"[push] Sent to {success_count}/{len(tokens)} devices")
    return success_count

def record_upload(date_str: str):
    with _get_db_connection() as conn:
        c = conn.cursor()
        # Ignore duplicates (unique date index) to keep retries safe.
        c.execute("INSERT OR IGNORE INTO uploads VALUES (?, ?)",
                  (date_str, datetime.now().isoformat()))
        conn.commit()

def check_image_hash(image_hash: str, current_date: str) -> tuple[bool, str | None]:
    """
    Check if an image hash has been used before.
    Returns (is_duplicate, previous_date)
    - If image is new: (False, None)
    - If image was used on same date: (False, None) - allow resubmission same day
    - If image was used on different date: (True, previous_date)
    """
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT date FROM image_hashes WHERE hash = ?", (image_hash,))
        result = c.fetchone()
    
    if result is None:
        return (False, None)  # New image
    
    previous_date = result[0]
    if previous_date == current_date:
        return (False, None)  # Same day resubmission is OK
    
    return (True, previous_date)  # Duplicate from different day

def record_image_hash(image_hash: str, date_str: str):
    """Store an image hash with its date."""
    with _get_db_connection() as conn:
        c = conn.cursor()
        # Use INSERT OR REPLACE to handle resubmissions on the same day
        c.execute("INSERT OR REPLACE INTO image_hashes VALUES (?, ?, ?)", 
                  (image_hash, date_str, datetime.now().isoformat()))
        conn.commit()

def has_valid_upload_recently():
    # Deprecated/Unused with new logic
    return False

def has_submission_for_date(date_str: str) -> bool:
    """Check if there's a valid submission for a specific date."""
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT count(*) FROM uploads WHERE date = ?", (date_str,))
        count = c.fetchone()[0]
    return count > 0

def _get_state_value(key: str) -> str | None:
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = c.fetchone()
    if row is None:
        return None
    return row[0]

def _set_state_value(key: str, value: str) -> None:
    with _get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
        conn.commit()

def _parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _format_yyyy_mm_dd(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def get_next_due_date(target_due_date: str) -> str:
    """
    Return the oldest missing logical date that must be submitted next.

    We store a pointer in the DB so missed days become a backlog. The user can
    backfill multiple days in one real day, but only sequentially.
    """
    next_due = _get_state_value(STATE_KEY_NEXT_DUE_DATE)
    if not next_due:
        # First run: start at whatever is currently due.
        next_due = target_due_date
        _set_state_value(STATE_KEY_NEXT_DUE_DATE, next_due)
    return next_due

def advance_next_due_date(target_due_date: str) -> str:
    """
    Advance next-due pointer forward while there are already submissions for the
    currently required date AND that date is within the currently-required window
    (<= target_due_date).
    """
    next_due = get_next_due_date(target_due_date)
    next_due_d = _parse_yyyy_mm_dd(next_due)
    target_d = _parse_yyyy_mm_dd(target_due_date)

    while next_due_d <= target_d and has_submission_for_date(_format_yyyy_mm_dd(next_due_d)):
        next_due_d = next_due_d + timedelta(days=1)

    next_due = _format_yyyy_mm_dd(next_due_d)
    _set_state_value(STATE_KEY_NEXT_DUE_DATE, next_due)
    return next_due

def send_email():
    sender_email = os.getenv("EMAIL_USER")
    sender_password = os.getenv("EMAIL_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    if not sender_email or not sender_password:
        print("Warning: EMAIL_USER or EMAIL_PASSWORD not set. Cannot send alert email.")
        return

    msg = EmailMessage()
    msg.set_content(f"Aayush hasn't logged his food for more than 24 hours past the deadline. He is currently behind on logging for {get_next_due_date(get_due_logical_date())}.")
    msg['Subject'] = "⚠️ Aayush Is Behind on Food Logs"
    msg['From'] = sender_email
    msg['To'] = ", ".join(CONTACT_EMAILS)

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"Alert email sent to {CONTACT_EMAILS}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def scheduled_check():
    print("Running scheduled check for food log...")
    
    # Check if we are "caught up".
    # Caught up means next_due_date > target_due_date.
    # If next_due_date <= target_due_date, we have a missing log.
    # If next_due_date < target_due_date, we are MORE than 24 hours behind (missed >1 deadline).
    
    target_due_date = get_due_logical_date()
    # Ensure pointer is initialized/advanced if needed (idempotent)
    next_due_date = advance_next_due_date(target_due_date)
    
    print(f"Scheduled Check: Target={target_due_date}, Next={next_due_date}")
    
    if next_due_date < target_due_date:
        print(f"User is behind by more than 24 hours (Next: {next_due_date} < Target: {target_due_date}). Sending email.")
        send_email()
    elif next_due_date == target_due_date:
        print(f"User is due for today ({next_due_date}), but not >24h behind yet. No email.")
    else:
        print(f"User is caught up (Next: {next_due_date} > Target: {target_due_date}).")

def scheduled_push_check():
    """
    5 AM push check - sends a push notification to wake up the iOS app.
    The app will then sync with the server and lock/unlock accordingly.
    """
    print("Running 5 AM push check...")
    
    target_due_date = get_due_logical_date()
    next_due_date = advance_next_due_date(target_due_date)
    
    # Determine if phone should be locked
    should_lock = next_due_date <= target_due_date
    
    print(f"5AM Push Check: Target={target_due_date}, Next={next_due_date}, ShouldLock={should_lock}")
    
    # Send push notification to wake the app
    # The notification includes lock status so app can update immediately
    if should_lock:
        title = "⚠️ Phone Locked"
        body = f"Log your food for {next_due_date} to unlock"
    else:
        title = "✅ Good morning!"
        body = "You're all caught up on food logs"
    
    data = {
        "action": "sync",
        "should_lock": should_lock,
        "next_due_date": next_due_date,
        "target_due_date": target_due_date
    }
    
    count = send_push_to_all(title, body, data)
    print(f"5AM Push: Sent to {count} device(s)")

def create_scheduler():
    scheduler = BackgroundScheduler()
    # 10 PM - Email check for users who are >24h behind
    scheduler.add_job(scheduled_check, 'cron', hour=CHECK_HOUR, minute=CHECK_MINUTE)
    # 5 AM - Push notification to wake iOS app and apply lock state
    scheduler.add_job(scheduled_push_check, 'cron', hour=PUSH_CHECK_HOUR, minute=PUSH_CHECK_MINUTE)
    return scheduler

