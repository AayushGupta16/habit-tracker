import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import os
from apscheduler.schedulers.background import BackgroundScheduler

# Constants for email notifications
CONTACT_EMAILS = ["jyotigupta_mail@yahoo.com"]
CHECK_HOUR = 22  # 10 PM - When to run the daily check
CHECK_MINUTE = 0
ALERT_AFTER_HOURS = 48  # Alert if no valid upload in the last X hours
DB_NAME = "foodlog.db"

# Database helpers
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS uploads
                 (date text, timestamp text)''')
    c.execute('''CREATE TABLE IF NOT EXISTS image_hashes
                 (hash text PRIMARY KEY, date text, timestamp text)''')
    conn.commit()
    conn.close()

def record_upload(date_str: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO uploads VALUES (?, ?)", 
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
    cutoff_time = (datetime.now() - timedelta(hours=ALERT_AFTER_HOURS)).isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM uploads WHERE timestamp >= ?", (cutoff_time,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def has_submission_for_date(date_str: str) -> bool:
    """Check if there's a valid submission for a specific date."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM uploads WHERE date = ?", (date_str,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def send_email():
    sender_email = os.getenv("EMAIL_USER")
    sender_password = os.getenv("EMAIL_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    if not sender_email or not sender_password:
        print("Warning: EMAIL_USER or EMAIL_PASSWORD not set. Cannot send alert email.")
        return

    msg = EmailMessage()
    msg.set_content(f"Aayush has not logged his food in {ALERT_AFTER_HOURS} hours. Please call and bug him.")
    msg['Subject'] = "⚠️ Aayush Hasn't Logged His Food"
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
    if not has_valid_upload_recently():
        print(f"No valid upload found in the last {ALERT_AFTER_HOURS} hours. Sending email.")
        send_email()
    else:
        print(f"Valid upload found within the last {ALERT_AFTER_HOURS} hours.")

def create_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_check, 'cron', hour=CHECK_HOUR, minute=CHECK_MINUTE)
    return scheduler

