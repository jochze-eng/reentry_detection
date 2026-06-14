from fastapi import APIRouter, HTTPException, Query
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

logger = logging.getLogger(__name__)

router = APIRouter()

# ------------------------------------------------------------------ #
#  Camera name cache (refreshed every 5 minutes)
# ------------------------------------------------------------------ #
_camera_cache: dict[int, str] = {}
_camera_cache_ts: float = 0.0
_CAMERA_CACHE_TTL = 300  # seconds

@router.get("/cameras")
async def get_cameras():
    """Return a cameraId→name map, refreshed from Vaidio every 5 minutes."""
    global _camera_cache, _camera_cache_ts
    now = time.time()
    if not _camera_cache or (now - _camera_cache_ts) > _CAMERA_CACHE_TTL:
        cfg = await load_config()
        if not cfg:
            return {}
        try:
            client = VaidioClient(cfg)
            _camera_cache = await client.get_cameras()
            _camera_cache_ts = now
        except Exception as e:
            logger.warning(f"Failed to refresh camera list: {e}")
            # Return stale cache if available, else empty
    return _camera_cache

# ------------------------------------------------------------------ #
#  Image proxy — serves Vaidio images to avoid browser SSL errors
# ------------------------------------------------------------------ #

@router.get("/image")
async def proxy_image(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "image/jpeg")
            return Response(content=r.content, media_type=content_type)
    except Exception as e:
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
):
    """Return all Vaidio-searched or DB-logged detections for a given face target within the configured lookback window."""
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")

    if face_file:
        try:
            client = VaidioClient(cfg)
            descriptor = await client.get_face_descriptor(face_file)
            if descriptor:
                records = await client.search_face_history(descriptor)
                return records
        except Exception as e:
            logger.warning(f"Failed to fetch face history from Vaidio for {face_target_id}: {e}, falling back to DB")

    try:
        records = await db_manager.get_fr_logs_by_target(
            face_target_id=face_target_id,
            lookback_hours=cfg.fr.lookback_hours,
        )
        return records
    except Exception as e:
        logger.error(f"Error fetching target history for {face_target_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
