from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from typing import List, Optional
from pydantic import BaseModel
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
from PIL import Image
import io
import logging
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager
from email_scheduler import (
    init_db, 
    record_upload, 
    create_scheduler, 
    check_image_hash, 
    record_image_hash, 
    has_submission_for_date, 
    advance_next_due_date,
    get_logical_date,
    get_logical_yesterday,
    get_due_logical_date,
    SF_TZ,
    DAY_START_HOUR,
    TIMEZONE_NAME
)

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    scheduler = create_scheduler()
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

# Initialize Gemini client
# Expects GEMINI_API_KEY in environment variables
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("Warning: GEMINI_API_KEY not found in environment variables.")

client = genai.Client(api_key=api_key)

class UploadResponse(BaseModel):
    success: bool
    reason: str | None = None

@app.post("/validate-log", response_model=UploadResponse)
async def validate_log(
    date: str = Form(...),
    screenshots: List[UploadFile] = File(None),
    video: UploadFile = File(None)
):
    # Enforce server-side "debt queue" logic:
    # - target_due_date is the latest logical date that must be satisfied as of now (yesterday after 5am).
    # - next_due_date is the oldest missing date; users must submit sequentially to catch up.
    # - today_logical is the current logical day (allows proactive logging before deadline).
    now = datetime.now(SF_TZ)
    target_due_date = get_due_logical_date(now)
    today_logical = get_logical_date(now)
    next_due_date = advance_next_due_date(target_due_date)

    # Determine which date to accept:
    # 1. If behind (next_due_date <= target_due_date): must submit for next_due_date
    # 2. If caught up (next_due_date > target_due_date): can submit for today_logical (proactive)
    is_caught_up = next_due_date > target_due_date
    
    if is_caught_up:
        # User is caught up on past dues. Allow proactive submission for TODAY.
        acceptable_date = today_logical
        
        if has_submission_for_date(today_logical):
            msg = f"You've already logged for today ({today_logical}). Nothing more to submit."
            logger.info(msg)
            return UploadResponse(success=False, reason=msg)
        
        if date != acceptable_date:
            msg = f"You're caught up! Submit for today: {acceptable_date}"
            logger.warning(f"{msg} (client_sent={date})")
            return UploadResponse(success=False, reason=msg)
    else:
        # User has backlog. Must submit for next_due_date.
        acceptable_date = next_due_date
        
        if date != acceptable_date:
            msg = f"Invalid date. You have a backlog. Submit for: {next_due_date}"
            logger.warning(f"{msg} (client_sent={date}, target_due_date={target_due_date}, now={now.isoformat()})")
            return UploadResponse(success=False, reason=msg)

        if has_submission_for_date(next_due_date):
            msg = f"A valid log for {next_due_date} has already been submitted."
            logger.info(msg)
            return UploadResponse(success=False, reason=msg)

    logger.info(f"Processing validation request for next due date: {date} (target_due_date={target_due_date})")
    
    try:
        if not screenshots and not video:
            logger.warning("No media provided in request")
            return UploadResponse(success=False, reason="No images or video provided")
        
        contents = []
        image_hashes = []
        gemini_reason = ""
        
        # ---------------------------------------------------------------------
        # VIDEO FLOW
        # ---------------------------------------------------------------------
        if video:
            logger.info(f"Processing video upload: {video.filename}")
            video_content = await video.read()
            video_hash = hashlib.sha256(video_content).hexdigest()
            
            # Deduplication check
            is_duplicate, previous_date = check_image_hash(video_hash, date)
            if is_duplicate:
                msg = f"Video {video.filename} was already used on {previous_date}. Please upload a fresh video."
                logger.warning(msg)
                return UploadResponse(success=False, reason=msg)
                
            image_hashes.append(video_hash)
            
            # Prepare Gemini content for video
            # Get human-readable date and today for context
            required_date_obj = datetime.strptime(date, "%Y-%m-%d")
            required_date_formatted = required_date_obj.strftime("%B %d, %Y")  # e.g. "December 18, 2024"
            required_date_short = required_date_obj.strftime("%b %d")  # e.g. "Dec 18"
            today_formatted = datetime.now(SF_TZ).strftime("%B %d, %Y")
            
            prompt = f"""
            CONTEXT:
            You are an AI validator for a habit-tracking app. 
            The user has uploaded a screen recording of their food log app (e.g. MyFitnessPal, LoseIt).
            
            REQUIRED DATE TO VERIFY: {date} ({required_date_formatted})
            TODAY'S ACTUAL DATE: {today_formatted} ({TIMEZONE_NAME})
            
            The food log app must show entries for **{required_date_formatted}** (also written as "{required_date_short}" or "{date}").
            
            SPECIAL CASE - "Yesterday" label:
            - If the device clock visible in the recording shows a time BEFORE {DAY_START_HOUR}:00am, AND
            - The food log app displays "Yesterday" instead of an explicit date, AND  
            - "Yesterday" relative to that device time equals {required_date_formatted},
            - THEN treat "Yesterday" as valid for the required date.
            - Otherwise, the app MUST show the explicit date {required_date_formatted}.
            
            YOUR TASK:
            Analyze the video to verify:
            1. The user is in a food tracking app (showing food/calories).
            2. They scroll/navigate to show the DATE, and it matches the REQUIRED DATE: {required_date_formatted}.
            3. The total calories for the day are > 1200.
            
            OUTPUT:
            Return ONLY the word 'TRUE' if all checks pass.
            Return ONLY the word 'FALSE' if any check fails.
            """
            
            contents = [
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=video_content, mime_type=video.content_type or "video/mp4")
            ]
            gemini_reason = "Video analysis"
            
        # ---------------------------------------------------------------------
        # IMAGE FLOW
        # ---------------------------------------------------------------------
        elif screenshots:
            logger.info(f"Processing {len(screenshots)} screenshots")
            
            image_parts = []
            for screenshot in screenshots:
                content = await screenshot.read()
                
                # Compute hash
                img_hash = hashlib.sha256(content).hexdigest()
                is_duplicate, previous_date = check_image_hash(img_hash, date)
                if is_duplicate:
                    msg = f"Image {screenshot.filename} was already used on {previous_date}. Please upload fresh screenshots."
                    logger.warning(msg)
                    return UploadResponse(success=False, reason=msg)
                
                # Basic validation
                try:
                    img = Image.open(io.BytesIO(content))
                    img.verify()
                except Exception as e:
                    return UploadResponse(success=False, reason=f"Invalid image: {screenshot.filename} ({str(e)})")

                image_hashes.append(img_hash)
                image_parts.append(types.Part.from_bytes(data=content, mime_type=screenshot.content_type or "image/jpeg"))

            # Get human-readable date and today for context
            required_date_obj = datetime.strptime(date, "%Y-%m-%d")
            required_date_formatted = required_date_obj.strftime("%B %d, %Y")  # e.g. "December 18, 2024"
            required_date_short = required_date_obj.strftime("%b %d")  # e.g. "Dec 18"
            today_formatted = datetime.now(SF_TZ).strftime("%B %d, %Y")
            
            prompt = f"""
            CONTEXT:
            You are an AI validator for a habit-tracking app. 
            Users must upload screenshot(s) of their daily food log to prove they are tracking their food intake.
            
            REQUIRED DATE TO VERIFY: {date} ({required_date_formatted})
            TODAY'S ACTUAL DATE: {today_formatted} ({TIMEZONE_NAME})
            INPUT: {len(image_parts)} image(s).
            
            The food log app must show entries for **{required_date_formatted}** (also written as "{required_date_short}" or "{date}").
            
            SPECIAL CASE - "Yesterday" label:
            - If the device clock/status bar visible in the screenshot shows a time BEFORE {DAY_START_HOUR}:00am, AND
            - The food log app displays "Yesterday" instead of an explicit date, AND  
            - "Yesterday" relative to that device time equals {required_date_formatted},
            - THEN treat "Yesterday" as valid for the required date.
            - Otherwise, the app MUST show the explicit date {required_date_formatted}.
            
            VERIFICATION STEPS:
            1. **Relevance**: Is this a food log? If random photo, return 'FALSE'.
            2. **Date Check**: The recording must display {required_date_formatted} (or valid "Yesterday" per above).
               - If the date shown does NOT match {required_date_formatted}, return 'FALSE'.
            3. **Calorie Check**: Sum total calories visible. 
               - If < 1200, return 'FALSE'.
               - If visibly incomplete (e.g. only breakfast) and low calories, return 'FALSE'.
            
            OUTPUT:
            Return ONLY the word 'TRUE' if all checks pass.
            Return ONLY the word 'FALSE' if any check fails.
            """
            
            contents = [types.Part.from_text(text=prompt)] + image_parts
            gemini_reason = f"Image analysis ({len(image_parts)} images)"

        # ---------------------------------------------------------------------
        # GEMINI CALL
        # ---------------------------------------------------------------------
        logger.info("Sending request to Gemini model...")
        response = client.models.generate_content(
            model="gemini-3-flash-preview", 
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=1,
                thinking_config=types.ThinkingConfig(
                    thinking_level="LOW", 
                    include_thoughts=False 
                )
            )
        )
        
        raw_text = response.text or ""
        logger.info(f"Gemini raw response: {raw_text}")
        
        text_response = raw_text.strip().lower()
        is_accurate = text_response == "true"
        
        logger.info(f"Validation result: {is_accurate} (Reason: {text_response})")

        if is_accurate:
            # Record the successful upload
            record_upload(date)
            # Store hashes
            for h in image_hashes:
                record_image_hash(h, date)
            logger.info(f"Stored {len(image_hashes)} hashes for {date}")
            # Advance pointer after successful submission (may still be locked if backlog remains)
            advance_next_due_date(target_due_date)

        return UploadResponse(
            success=is_accurate,
            reason=f"Gemini evaluation ({gemini_reason}): {text_response}"
        )

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return UploadResponse(success=False, reason=str(e))

class AppConfig(BaseModel):
    day_start_hour: int
    timezone: str

class StatusResponse(BaseModel):
    submitted_today: bool
    submitted_yesterday: bool
    target_due_date: str
    next_due_date: str
    is_caught_up: bool  # True = today is logged (nothing to submit)
    should_lock: bool   # True = has overdue backlog (past deadline missed)
    config: AppConfig

@app.get("/check-status", response_model=StatusResponse)
def check_status():
    """Check if submissions exist for today and yesterday (using 5am day boundary)."""
    today = get_logical_date()
    yesterday = get_logical_yesterday()
    target_due_date = get_due_logical_date()
    next_due_date_from_backlog = advance_next_due_date(target_due_date)
    
    # Determine what the user should submit next:
    # - If backlog exists (next_due <= target_due): submit for next_due_date_from_backlog
    # - If no backlog: submit for today (proactive logging)
    has_backlog = next_due_date_from_backlog <= target_due_date
    
    if has_backlog:
        effective_next_due = next_due_date_from_backlog
    else:
        effective_next_due = today
    
    # should_lock: True if there's a backlog (past deadline missed)
    # This controls whether the phone should be locked
    should_lock = has_backlog
    
    # is_caught_up: True if today's log is already submitted (nothing to submit)
    # This controls whether upload buttons are shown
    is_caught_up = has_submission_for_date(today) and not has_backlog
    
    logger.info(f"Checking status - logical today: {today}, yesterday: {yesterday}, "
                f"target_due: {target_due_date}, backlog_next: {next_due_date_from_backlog}, "
                f"effective_next: {effective_next_due}, should_lock: {should_lock}, is_caught_up: {is_caught_up}")
    
    return StatusResponse(
        submitted_today=has_submission_for_date(today),
        submitted_yesterday=has_submission_for_date(yesterday),
        target_due_date=target_due_date,
        next_due_date=effective_next_due,
        is_caught_up=is_caught_up,
        should_lock=should_lock,
        config=AppConfig(
            day_start_hour=DAY_START_HOUR,
            timezone=TIMEZONE_NAME
        )
    )

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Food Log Validator (Gemini 3 Flash) is running"}
