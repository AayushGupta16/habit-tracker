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
from email_scheduler import init_db, record_upload, create_scheduler, check_image_hash, record_image_hash, has_submission_for_date

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

# Hard-coded "user local time" for now.
# If you later support multiple users/timezones, push this to a per-user setting.
SF_TZ = ZoneInfo("America/Los_Angeles")

class UploadResponse(BaseModel):
    success: bool
    reason: str | None = None

@app.post("/validate-log", response_model=UploadResponse)
async def validate_log(
    date: str = Form(...),
    screenshots: List[UploadFile] = File(None),
    video: UploadFile = File(None)
):
    logger.info(f"Processing validation request for date: {date}")
    
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
            prompt = f"""
            CONTEXT:
            You are an AI validator for a habit-tracking app. 
            The user has uploaded a screen recording of their food log app (e.g. MyFitnessPal, LoseIt).
            
            CURRENT DATE (LOGICAL DATE): {date}
            IMPORTANT TIME RULE:
            - The user's "day" runs from 5:00am to 5:00am in their local time (America/Los_Angeles).
            - If the recording is taken before 5:00am, MyFitnessPal may label the log as "Yesterday".
              In that case, "Yesterday" is VALID for CURRENT DATE as long as the on-screen time is before 5:00am.
            
            YOUR TASK:
            Analyze the video to verify:
            1. The user is in a food tracking app (showing food/calories).
            2. They scroll/navigate to show the DATE, and it matches CURRENT DATE: {date} (using the time rule above).
            3. The total calories for the day seem to be > 1200.
            
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

            prompt = f"""
            CONTEXT:
            You are an AI validator for a habit-tracking app. 
            Users must upload screenshot(s) of their daily food log to prove they are tracking their food intake.
            
            CURRENT DATE (LOGICAL DATE): {date}
            IMPORTANT TIME RULE:
            - The user's "day" runs from 5:00am to 5:00am in their local time (America/Los_Angeles).
            - If the screenshot is taken before 5:00am, MyFitnessPal may label the log as "Yesterday".
              In that case, "Yesterday" is VALID for CURRENT DATE as long as the on-screen time is before 5:00am.
            INPUT: {len(image_parts)} image(s).
            
            YOUR TASK:
            Analyze the provided image(s) to verify it is a legitimate, unique food log for CURRENT DATE (using the time rule above).
            
            VERIFICATION STEPS:
            1. **Relevance**: Is this a food log? If random photo, return 'FALSE'.
            2. **Calorie Check**: Sum total calories. 
               - If < 1200, return 'FALSE'.
               - If visibly incomplete (e.g. only breakfast) and low calories, return 'FALSE'.
            3. **Date Check**:
               - Prefer explicit dates shown in the app UI.
               - If the app shows a relative label like "Yesterday", treat it as VALID for CURRENT DATE only when the device/status-bar time visible in the screenshot is before 5:00am.
            
            OUTPUT:
            Return ONLY the word 'TRUE' if it passes.
            Return ONLY the word 'FALSE' if it fails.
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

        return UploadResponse(
            success=is_accurate,
            reason=f"Gemini evaluation ({gemini_reason}): {text_response}"
        )

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return UploadResponse(success=False, reason=str(e))

class StatusResponse(BaseModel):
    submitted_today: bool
    submitted_yesterday: bool

# Day boundary hour (5am) - days run from 5am to 5am
DAY_START_HOUR = 5

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

@app.get("/check-status", response_model=StatusResponse)
def check_status():
    """Check if submissions exist for today and yesterday (using 5am day boundary)."""
    today = get_logical_date()
    yesterday = get_logical_yesterday()
    
    logger.info(f"Checking status - logical today: {today}, logical yesterday: {yesterday}")
    
    return StatusResponse(
        submitted_today=has_submission_for_date(today),
        submitted_yesterday=has_submission_for_date(yesterday)
    )

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Food Log Validator (Gemini 3 Flash) is running"}
