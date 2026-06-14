from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import Response
from models.config_model import AppConfig
from services.lpr_monitor import lpr_monitor
from services.fr_monitor import fr_monitor
from services.vaidio_client import VaidioClient
from services.db import db_manager
from config import load_config, save_config
import httpx
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

router = APIRouter()

# ------------------------------------------------------------------ #
#  Camera name cache (refreshed every 5 minutes)
# ------------------------------------------------------------------ #
_camera_cache: dict[int, str] = {}
_camera_cache_ts: float = 0.0
_CAMERA_CACHE_TTL = 300  # seconds

async def perform_camera_sync(cfg: AppConfig):
    try:
        client = VaidioClient(cfg)
        cameras = await client.get_cameras_with_status()
        await db_manager.upsert_cameras(cameras)
        logger.info(f"Successfully synced {len(cameras)} cameras to database cache.")
    except Exception as e:
        logger.error(f"Background camera cache sync failed: {e}")

@router.get("/cameras")
async def get_cameras():
    """Return a cameraId→name map from the database cache."""
    cached_cams = await db_manager.get_cached_cameras()
    if not cached_cams:
        cfg = await load_config()
        if cfg and cfg.vaidio.base_url and "localhost" not in cfg.vaidio.base_url:
            try:
                client = VaidioClient(cfg)
                cameras = await client.get_cameras_with_status()
                await db_manager.upsert_cameras(cameras)
                cached_cams = await db_manager.get_cached_cameras()
            except Exception as e:
                logger.warning(f"Failed to populate camera cache synchronously: {e}")
    
    return {cam["camera_id"]: cam["name"] for cam in cached_cams}

@router.get("/cameras/by-engine")
async def get_cameras_by_engine(background_tasks: BackgroundTasks):
    cached_cams = await db_manager.get_cached_cameras()
    cfg = await load_config()
    
    if not cfg:
        return format_cached_cameras(cached_cams)
        
    last_sync = await db_manager.get_last_camera_sync_time()
    now = datetime.now(timezone.utc)
    
    if not cached_cams:
        if cfg.vaidio.base_url and "localhost" not in cfg.vaidio.base_url:
            try:
                logger.info("Camera cache empty. Fetching synchronously from Vaidio...")
                client = VaidioClient(cfg)
                cameras = await client.get_cameras_with_status()
                await db_manager.upsert_cameras(cameras)
                cached_cams = await db_manager.get_cached_cameras()
            except Exception as e:
                logger.error(f"Failed synchronous camera fetch: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to load cameras: {e}")
    else:
        # If cache is older than 5 minutes, trigger background refresh
        if not last_sync or (now - last_sync).total_seconds() > 300:
            if cfg.vaidio.base_url and "localhost" not in cfg.vaidio.base_url:
                logger.info("Camera cache is stale. Triggering background refresh...")
                background_tasks.add_task(perform_camera_sync, cfg)
                
    return format_cached_cameras(cached_cams)

def format_cached_cameras(cameras: list[dict]) -> dict:
    lpr_cameras = []
    fr_cameras = []
    
    def is_engine_enabled(plugins_str: str, engine_models_str: str, engine_name: str) -> bool:
        if plugins_str and engine_name.lower() in plugins_str.lower():
            return True
        if engine_models_str and engine_name.lower() in engine_models_str.lower():
            return True
        return False

    for cam in cameras:
        cam_id = cam["camera_id"]
        cam_name = cam["name"]
        is_activate = cam["is_activate"]
        plugins_str = cam["plugins"] or ""
        engine_models_str = cam["engine_models"] or ""
        
        # Check LPREngine
        if is_engine_enabled(plugins_str, engine_models_str, "LPREngine"):
            lpr_cameras.append({
                "id": cam_id,
                "name": cam_name,
                "is_activate": is_activate
            })
            
        # Check FaceRecognitionEngine
        if is_engine_enabled(plugins_str, engine_models_str, "FaceRecognitionEngine"):
            fr_cameras.append({
                "id": cam_id,
                "name": cam_name,
                "is_activate": is_activate
            })
            
    return {"lpr": lpr_cameras, "fr": fr_cameras}

# ------------------------------------------------------------------ #
#  Image proxy — serves Vaidio images to avoid browser SSL errors
# ------------------------------------------------------------------ #

@router.get("/image")
async def proxy_image(url: str = Query(...)):
    try:
        cfg = await load_config()
        cache_enabled = cfg and cfg.image_cache_hours > 0

        # Try fetching from DB cache first
        if cache_enabled:
            try:
                cached = await db_manager.get_cached_image(url)
                if cached:
                    logger.info(f"Image cache HIT for {url}")
                    return Response(content=cached["content"], media_type=cached["content_type"])
            except Exception as cache_err:
                logger.error(f"Error querying image cache for {url}: {cache_err}")

        # Cache miss or caching disabled: fetch from Vaidio
        logger.info(f"Image cache MISS/DISABLED (enabled={cache_enabled}). Fetching from {url}...")
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "image/jpeg")
            content = r.content

        # Cache the fetched image if cache is enabled
        if cache_enabled:
            try:
                await db_manager.insert_cached_image(url, content, content_type)
                logger.info(f"Cached image in DB for {url}")
            except Exception as cache_err:
                logger.error(f"Failed to insert image cache for {url}: {cache_err}")

        return Response(content=content, media_type=content_type)
    except Exception as e:
        logger.error(f"Image fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Image fetch failed: {e}")

# ------------------------------------------------------------------ #
#  Config
# ------------------------------------------------------------------ #

@router.get("/config")
async def get_config():
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")
    d = cfg.dict()
    key = d["vaidio"]["api_key"]
    d["vaidio"]["api_key"] = key[:6] + "*" * (len(key) - 6)
    return d

@router.post("/config")
async def set_config(cfg: AppConfig):
    # If the API key is empty or contains asterisks, retain the existing one
    if not cfg.vaidio.api_key or "*" in cfg.vaidio.api_key:
        existing_cfg = await load_config()
        if existing_cfg:
            cfg.vaidio.api_key = existing_cfg.vaidio.api_key

    await save_config(cfg)
    
    # Trigger image cache cleanup immediately if cache is disabled or altered
    try:
        await db_manager.delete_expired_cached_images(cfg.image_cache_hours)
    except Exception as cleanup_err:
        logger.error(f"Failed immediate image cache cleanup: {cleanup_err}")

    # Start or stop LPR monitor based on enabled flag
    if cfg.job.enabled:
        await lpr_monitor.start(cfg)
    else:
        lpr_monitor.stop()
    # Start or stop FR monitor based on enabled flag
    if cfg.fr.enabled:
        await fr_monitor.start(cfg)
    else:
        fr_monitor.stop()
    return {"message": "Config saved"}

@router.post("/config/test")
async def test_config(cfg: AppConfig):
    # If the API key is empty or contains asterisks, retain the existing one
    if not cfg.vaidio.api_key or "*" in cfg.vaidio.api_key:
        existing_cfg = await load_config()
        if existing_cfg:
            cfg.vaidio.api_key = existing_cfg.vaidio.api_key

    client = VaidioClient(cfg)
    ok = await client.test_connection()
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot connect to Vaidio server")
    return {"message": "Connection successful"}

# ------------------------------------------------------------------ #
#  LPR Monitor
# ------------------------------------------------------------------ #

@router.get("/monitor/status")
async def get_lpr_status():
    status = lpr_monitor.get_status()
    cfg = await load_config()
    lookback = cfg.job.lookback_hours if cfg else 24
    status["stats"]["unique_lpr_count"] = await db_manager.get_unique_lpr_count(lookback)
    return status

@router.get("/monitor/logs")
async def get_lpr_logs(limit: int = Query(default=50, ge=1, le=200)):
    return await db_manager.get_lpr_logs(limit=limit)

@router.get("/monitor/target/history")
async def get_lpr_target_history(
    characters: str = Query(...),
):
    """Return all Vaidio-searched detections for a given license plate within the configured lookback window."""
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")

    try:
        client = VaidioClient(cfg)
        records = await client.search_lpr_history(characters)
        return records
    except Exception as e:
        logger.error(f"Error fetching target history for plate {characters}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ------------------------------------------------------------------ #
#  FR Monitor
# ------------------------------------------------------------------ #

@router.get("/fr/status")
async def get_fr_status():
    status = fr_monitor.get_status()
    cfg = await load_config()
    lookback = cfg.fr.lookback_hours if cfg else 24
    status["stats"]["unique_fr_count"] = await db_manager.get_unique_fr_count(lookback)
    return status

@router.get("/fr/logs")
async def get_fr_logs(limit: int = Query(default=50, ge=1, le=200)):
    return await db_manager.get_fr_logs(limit=limit)

@router.get("/fr/target/history")
async def get_fr_target_history(
    face_target_id: str = Query(...),
    face_file: str = Query(default=None),
    detected_at: str = Query(default=None),
):
    """Return detections for a face target within the configured Check Period.
    When detected_at is provided (from the clicked log row), the window is
    detected_at - lookback_hours → detected_at — exactly the window the monitor
    used when it computed history_count for that row. So a row showing 1 hit
    will expand to show exactly 1 detection, and 2 hits will show 2, etc.
    """
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")

    # Parse the anchor timestamp (from the clicked row), fall back to now
    anchor_dt = None
    if detected_at:
        try:
            # Use fromisoformat — handles "2026-06-13T09:34:00.366000+00:00" style strings
            anchor_dt = datetime.fromisoformat(detected_at)
        except Exception:
            pass

    if face_file:
        try:
            client = VaidioClient(cfg)
            
            # Check local DB descriptor cache first
            descriptor = await db_manager.get_fr_descriptor_by_file(face_file)
            if not descriptor:
                logger.info(f"Descriptor cache MISS for {face_file}. Extracting from Vaidio...")
                descriptor = await client.get_face_descriptor(face_file)
                if descriptor:
                    # Save to DB cache for future loads
                    await db_manager.update_fr_descriptor(face_file, descriptor)
            else:
                logger.info(f"Descriptor cache HIT for {face_file}")

            if descriptor:
                records = await client.search_face_history(
                    descriptor,
                    anchor_dt=anchor_dt,
                    lookback_hours=cfg.fr.lookback_hours,
                )
                if records:
                    return records
                # Vaidio returned empty (e.g. stranger with no other matches in the window).
                # Fall through to DB to return at least the clicked record itself.
                logger.info(f"Vaidio search returned 0 results for {face_target_id}, fetching from DB")
        except Exception as e:
            logger.warning(f"Failed to fetch face history from Vaidio for {face_target_id}: {e}, falling back to DB")

    try:
        # For strangers (faceTargetId="unknown"), filter by face_file to get only the specific
        # detection row. For named targets, use faceTargetId + the anchor window.
        if face_target_id == "unknown" and face_file and anchor_dt:
            records = await db_manager.get_fr_logs_by_face_file(face_file=face_file)
        else:
            records = await db_manager.get_fr_logs_by_target(
                face_target_id=face_target_id,
                lookback_hours=cfg.fr.lookback_hours,
            )
        return records
    except Exception as e:
        logger.error(f"Error fetching target history for {face_target_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
