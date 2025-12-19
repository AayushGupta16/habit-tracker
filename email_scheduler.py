import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, date
import os
from apscheduler.schedulers.background import BackgroundScheduler

from zoneinfo import ZoneInfo

# Constants for email notifications
CONTACT_EMAILS = ["jyotigupta_mail@yahoo.com"]
CHECK_HOUR = 22  # 10 PM - When to run the daily check
CHECK_MINUTE = 0
DB_NAME = "foodlog.db"
STATE_KEY_NEXT_DUE_DATE = "next_due_date"

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
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS uploads
                 (date text, timestamp text)''')
    # Enforce at most one successful upload per date. Keeps behavior idempotent.
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_date ON uploads(date)''')
    c.execute('''CREATE TABLE IF NOT EXISTS image_hashes
                 (hash text PRIMARY KEY, date text, timestamp text)''')
    c.execute('''CREATE TABLE IF NOT EXISTS state
                 (key text PRIMARY KEY, value text)''')
    conn.commit()
    conn.close()

def record_upload(date_str: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Ignore duplicates (unique date index) to keep retries safe.
    c.execute("INSERT OR IGNORE INTO uploads VALUES (?, ?)",
              (date_str, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def check_image_hash(image_hash: str, current_date: str) -> tuple[bool, str | None]:
    """
    Check if an image hash has been used before.
    Returns (is_duplicate, previous_date)
    - If image is new: (False, None)
    - If image was used on same date: (False, None) - allow resubmission same day
    - If image was used on different date: (True, previous_date)
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT date FROM image_hashes WHERE hash = ?", (image_hash,))
    result = c.fetchone()
    conn.close()
    
    if result is None:
        return (False, None)  # New image
    
    previous_date = result[0]
    if previous_date == current_date:
        return (False, None)  # Same day resubmission is OK
    
    return (True, previous_date)  # Duplicate from different day

def record_image_hash(image_hash: str, date_str: str):
    """Store an image hash with its date."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Use INSERT OR REPLACE to handle resubmissions on the same day
    c.execute("INSERT OR REPLACE INTO image_hashes VALUES (?, ?, ?)", 
              (image_hash, date_str, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def has_valid_upload_recently():
    # Deprecated/Unused with new logic
    return False

def has_submission_for_date(date_str: str) -> bool:
    """Check if there's a valid submission for a specific date."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM uploads WHERE date = ?", (date_str,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def _get_state_value(key: str) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return row[0]

def _set_state_value(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

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

def create_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_check, 'cron', hour=CHECK_HOUR, minute=CHECK_MINUTE)
    return scheduler

