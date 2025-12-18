from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from typing import List
from pydantic import BaseModel
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
from PIL import Image, ExifTags
import io
import logging
import hashlib
from datetime import datetime, timedelta

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

def get_image_date(image: Image.Image) -> str | None:
    try:
        exif = image.getexif()
        if not exif:
            return None
        # 36867 is DateTimeOriginal, 306 is DateTime
        # We prefer DateTimeOriginal (creation time) over DateTime (modification time)
        date_str = exif.get(36867) or exif.get(306)
        if date_str:
            # Format is usually "YYYY:MM:DD HH:MM:SS"
            # We want to return "YYYY-MM-DD"
            return date_str.split(' ')[0].replace(':', '-')
    except Exception:
        pass
    return None

class UploadResponse(BaseModel):
    success: bool
    reason: str | None = None

@app.post("/validate-log", response_model=UploadResponse)
async def validate_log(
    date: str = Form(...),
    screenshots: List[UploadFile] = File(...)
):
    logger.info(f"Processing validation request for date: {date} with {len(screenshots)} screenshots")
    try:
        if not screenshots:
            logger.warning("No images provided in request")
            return UploadResponse(success=False, reason="No images provided")
        
        # Read and validate all images
        image_contents = []
        image_hashes = []
        
        for screenshot in screenshots:
            contents = await screenshot.read()
            
            # Compute hash for deduplication
            image_hash = hashlib.sha256(contents).hexdigest()
            logger.info(f"Computed hash for {screenshot.filename}: {image_hash[:16]}...")
            
            # Check if this image was used before on a different date
            is_duplicate, previous_date = check_image_hash(image_hash, date)
            if is_duplicate:
                msg = f"Image {screenshot.filename} was already used on {previous_date}. Please upload fresh screenshots."
                logger.warning(msg)
                return UploadResponse(success=False, reason=msg)
            
            # Verify it's a valid image using PIL
            try:
                image = Image.open(io.BytesIO(contents))
                image.verify()
                
                # Re-open for metadata extraction (verify() can close/modify the file pointer)
                image = Image.open(io.BytesIO(contents))
                
                # Check metadata date
                img_date = get_image_date(image)
                logger.info(f"Extracted date from {screenshot.filename}: {img_date}")
                
                if img_date and img_date != date:
                    msg = f"Image date {img_date} does not match log date {date}"
                    logger.warning(msg)
                    return UploadResponse(success=False, reason=msg)
                
                if not img_date:
                    logger.warning(f"Could not verify date metadata in {screenshot.filename}. Proceeding with visual inspection.")
                    
            except Exception as e:
                logger.error(f"Image validation failed for {screenshot.filename}: {e}")
                return UploadResponse(success=False, reason=f"Invalid image format or metadata error: {screenshot.filename} ({str(e)})")
            
            image_contents.append((contents, screenshot.content_type or "image/jpeg"))
            image_hashes.append(image_hash)
        
        # Prompt for Gemini 3 Flash
        image_count = len(image_contents)
        prompt = f"""
        CONTEXT:
        You are an AI validator for a habit-tracking app. 
        Users must upload screenshot(s) of their daily food log (e.g., from MyFitnessPal, LoseIt, etc.) to prove they are tracking their food intake.
        If they fail to upload a valid, current log with enough calories, their phone locks them out until they do.
        
        CURRENT DATE: {date}
        INPUT: {image_count} image(s) provided.
        
        YOUR TASK:
        Analyze the provided image(s) to verify it is a legitimate, unique food log for TODAY.
        
        VERIFICATION STEPS:
        1. **Relevance**: Is this an image of a food log or calorie tracker? If it's a random photo (e.g., a selfie, a wall, a meme), return 'FALSE'.
        2. **Calorie Check**: Sum the total calories across all images. 
           - If the total is LESS THAN 1200, return 'FALSE'.
           - If the log seems visibly incomplete (e.g., only shows breakfast) and total is low, return 'FALSE'.
        
        OUTPUT:
        Return ONLY the word 'TRUE' if it passes all checks (valid log, >1200 calories).
        Return ONLY the word 'FALSE' if it fails any check.
        """
        
        # Build content array with prompt + all images
        contents = [types.Part.from_text(text=prompt)]
        for image_data, mime_type in image_contents:
            contents.append(types.Part.from_bytes(data=image_data, mime_type=mime_type))
        
        # Call Gemini 3 Flash Preview with "low thinking" (minimal) as requested
        # Docs: https://ai.google.dev/gemini-api/docs/gemini-3
        logger.info("Sending request to Gemini model...")
        response = client.models.generate_content(
            model="gemini-3-flash-preview", 
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=1,
                thinking_config=types.ThinkingConfig(
                    thinking_level="LOW", # Low thinking for speed as requested
                    include_thoughts=False # We don't need the thought trace in the response
                )
            )
        )
        
        # Gemini is instructed to return ONLY 'TRUE' or 'FALSE'.
        # Be strict here to avoid accidental passes like "untrue".
        raw_text = response.text or ""
        logger.info(f"Gemini raw response: {raw_text}")
        
        text_response = raw_text.strip().lower()
        is_accurate = text_response == "true"
        
        logger.info(f"Validation result: {is_accurate} (Reason: {text_response})")

        if is_accurate:
            # Record the successful upload
            record_upload(date)
            
            # Store image hashes to prevent reuse
            for img_hash in image_hashes:
                record_image_hash(img_hash, date)
            logger.info(f"Stored {len(image_hashes)} image hash(es) for date {date}")

        return UploadResponse(
            success=is_accurate,
            reason=f"Gemini evaluation ({image_count} image{'s' if image_count > 1 else ''}): {text_response}"
        )

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return UploadResponse(success=False, reason=str(e))

class StatusResponse(BaseModel):
    submitted_today: bool
    submitted_yesterday: bool

@app.get("/check-status", response_model=StatusResponse)
def check_status():
    """Check if submissions exist for today and yesterday."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    return StatusResponse(
        submitted_today=has_submission_for_date(today),
        submitted_yesterday=has_submission_for_date(yesterday)
    )

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Food Log Validator (Gemini 3 Flash) is running"}
