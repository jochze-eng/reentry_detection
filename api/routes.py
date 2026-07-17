import asyncio
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel
from models.config_model import AppConfig
from services.lpr_monitor import lpr_monitor
from services.fr_monitor import fr_monitor
from services.vaidio_client import VaidioClient
from services.db import db_manager, verify_password, hash_password
from config import load_config, save_config
import httpx
import logging
import time

logger = logging.getLogger(__name__)

router = APIRouter()

# ------------------------------------------------------------------ #
#  Authentication & Authorization Dependencies
# ------------------------------------------------------------------ #

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    session = await db_manager.get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Session invalid or expired")
    
    expires = session["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        await db_manager.delete_session(token)
        raise HTTPException(status_code=401, detail="Session expired")
    return session

async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "Administrator":
        raise HTTPException(status_code=403, detail="Administrator privilege required")
    return user

# ------------------------------------------------------------------ #
#  Authentication Endpoints
# ------------------------------------------------------------------ #

from collections import defaultdict

class LoginRateLimiter:
    def __init__(self):
        self.failures_by_user = defaultdict(list)
        self.failures_by_ip = defaultdict(list)
        self.lock = asyncio.Lock()

    async def check_and_throttle(self, username: str, ip: str):
        now = time.time()
        async with self.lock:
            self.failures_by_user[username] = [t for t in self.failures_by_user[username] if now - t < 60]
            self.failures_by_ip[ip] = [t for t in self.failures_by_ip[ip] if now - t < 60]
            
            user_fails = len(self.failures_by_user[username])
            ip_fails = len(self.failures_by_ip[ip])
            
            max_fails = max(user_fails, ip_fails)
            if max_fails >= 5:
                delay = min(1.0 + (max_fails - 5) * 0.5, 10.0)
                logger.warning(f"Login rate limit reached. Throttling request for {username} from IP {ip} by {delay:.2f} seconds.")
                await asyncio.sleep(delay)

    async def record_failure(self, username: str, ip: str):
        now = time.time()
        async with self.lock:
            self.failures_by_user[username].append(now)
            self.failures_by_ip[ip].append(now)

    async def record_success(self, username: str, ip: str):
        async with self.lock:
            self.failures_by_user.pop(username, None)
            self.failures_by_ip.pop(ip, None)

login_rate_limiter = LoginRateLimiter()

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangeDefaultPasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str

@router.post("/login")
async def api_login(req: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    await login_rate_limiter.check_and_throttle(req.username, client_ip)

    user = await db_manager.get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["password_hash"]):
        await login_rate_limiter.record_failure(req.username, client_ip)
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    # Check if user needs to change default password
    if user.get("must_change_password"):
        await login_rate_limiter.record_success(req.username, client_ip)
        return {"status": "must_change_password", "username": user["username"]}

    await login_rate_limiter.record_success(req.username, client_ip)

    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    
    await db_manager.create_session(token, user["username"], user["role"], expires_at)
    
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=86400,
        path="/"
    )
    return {"status": "success", "username": user["username"], "role": user["role"]}

@router.post("/change-default-password")
async def api_change_default_password(req: ChangeDefaultPasswordRequest):
    user = await db_manager.get_user_by_username(req.username)
    if not user or not verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    if not user.get("must_change_password"):
        raise HTTPException(status_code=400, detail="Password change not required for this user")
        
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters long")
    if req.new_password in ("admin123", "operator123"):
        raise HTTPException(status_code=400, detail="New password cannot be a default password")
        
    pw_hash = hash_password(req.new_password)
    await db_manager.update_user_password_and_clear_must_change(req.username, pw_hash)
    await db_manager.delete_sessions_for_user(req.username)
    
    return {"status": "success", "message": "Password updated successfully. Please log in with your new password."}

@router.post("/logout")
async def api_logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        await db_manager.delete_session(token)
    response.delete_cookie(key="session_token", path="/")
    return {"status": "success"}

@router.get("/user/me")
async def get_user_me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "role": user["role"]}

# ------------------------------------------------------------------ #
#  User Management API Endpoints (Admin Only)
# ------------------------------------------------------------------ #

class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str

class PasswordResetRequest(BaseModel):
    password: str

@router.get("/users")
async def api_get_users(user: dict = Depends(require_admin)):
    return await db_manager.get_all_users()

@router.post("/users")
async def api_create_user(req: UserCreateRequest, user: dict = Depends(require_admin)):
    username_clean = req.username.strip()
    if not username_clean:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if req.role not in ('Administrator', 'Operator'):
        raise HTTPException(status_code=400, detail="Invalid user group / role")
    
    existing = await db_manager.get_user_by_username(username_clean)
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")
    
    pw_hash = hash_password(req.password)
    await db_manager.create_user(username_clean, pw_hash, req.role)
    return {"status": "success", "message": f"User {username_clean} created"}

@router.put("/users/{username}/password")
async def api_reset_password(username: str, req: PasswordResetRequest, user: dict = Depends(require_admin)):
    existing = await db_manager.get_user_by_username(username)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    
    pw_hash = hash_password(req.password)
    await db_manager.update_user_password(username, pw_hash)
    await db_manager.delete_sessions_for_user(username)
    return {"status": "success", "message": f"Password reset for user {username}"}

@router.delete("/users/{username}")
async def api_delete_user(username: str, user: dict = Depends(require_admin)):
    existing = await db_manager.get_user_by_username(username)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Block deleting yourself
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account while logged in")
    
    # Block deleting the last administrator
    if existing["role"] == "Administrator":
        admin_count = await db_manager.count_administrators()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last Administrator account")
            
    await db_manager.delete_user(username)
    return {"status": "success", "message": f"User {username} deleted"}

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
async def get_cameras(user: dict = Depends(get_current_user)):
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
async def get_cameras_by_engine(background_tasks: BackgroundTasks, user: dict = Depends(require_admin)):
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
#  Target Lists / Categories
# ------------------------------------------------------------------ #

@router.get("/categories/lpr")
async def get_lpr_categories(user: dict = Depends(require_admin)):
    cfg = await load_config()
    if not cfg or not cfg.vaidio.base_url or "localhost" in cfg.vaidio.base_url:
        return []
    try:
        client = VaidioClient(cfg)
        return await client.get_lpr_category_names()
    except Exception as e:
        logger.error(f"Failed to fetch LPR categories: {e}")
        return []

@router.get("/categories/fr")
async def get_fr_categories(user: dict = Depends(require_admin)):
    cfg = await load_config()
    if not cfg or not cfg.vaidio.base_url or "localhost" in cfg.vaidio.base_url:
        return []
    try:
        client = VaidioClient(cfg)
        return await client.get_face_category_names()
    except Exception as e:
        logger.error(f"Failed to fetch FR categories: {e}")
        return []

# ------------------------------------------------------------------ #
#  Image proxy — serves Vaidio images to avoid browser SSL errors
# ------------------------------------------------------------------ #

import ipaddress
import socket
from urllib.parse import urlparse

def is_private_ip(hostname: str) -> bool:
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return True
    except Exception:
        pass
    return False

@router.get("/image")
async def proxy_image(url: str = Query(...), user: dict = Depends(get_current_user)):
    try:
        cfg = await load_config()
        if not cfg or not cfg.vaidio.base_url:
            raise HTTPException(status_code=400, detail="Vaidio base URL is not configured")
            
        parsed_url = urlparse(url)
        parsed_base = urlparse(cfg.vaidio.base_url)
        
        hostname = parsed_url.hostname
        base_hostname = parsed_base.hostname
        
        if not hostname or not base_hostname:
            raise HTTPException(status_code=400, detail="Invalid URL or configured base URL")
            
        # Domain allowlist: must match configured vaidio_base_url domain
        if hostname.lower() != base_hostname.lower():
            raise HTTPException(status_code=400, detail="URL does not match configured Vaidio base URL domain")
            
        # Scheme check: must be https (or match configured base URL scheme)
        allowed_schemes = {"https"}
        if parsed_base.scheme:
            allowed_schemes.add(parsed_base.scheme)
        if parsed_url.scheme not in allowed_schemes:
            raise HTTPException(status_code=400, detail="URL scheme is not allowed")
            
        # Private IP check (RFC 1918, loopback, link-local)
        # Block private IPs, except if the hostname matches base_hostname
        if is_private_ip(hostname) and hostname.lower() != base_hostname.lower():
            raise HTTPException(status_code=400, detail="Private IP ranges are blocked")

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Image fetch failed: {e}")

# ------------------------------------------------------------------ #
#  Config
# ------------------------------------------------------------------ #

@router.get("/config")
async def get_config(user: dict = Depends(require_admin)):
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")
    d = cfg.dict()
    key = d["vaidio"]["api_key"]
    d["vaidio"]["api_key"] = key[:6] + "*" * (len(key) - 6)
    return d

@router.post("/config")
async def set_config(cfg: AppConfig, user: dict = Depends(require_admin)):
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
        asyncio.create_task(lpr_monitor.start(cfg))
    else:
        lpr_monitor.stop()
    # Start or stop FR monitor based on enabled flag
    if cfg.fr.enabled:
        asyncio.create_task(fr_monitor.start(cfg))
    else:
        fr_monitor.stop()
    return {"message": "Config saved"}

@router.post("/config/test")
async def test_config(cfg: AppConfig, user: dict = Depends(require_admin)):
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
async def get_lpr_status(user: dict = Depends(get_current_user)):
    status = lpr_monitor.get_status()
    cfg = await load_config()
    lookback = cfg.job.lookback_hours if cfg else 24
    status["stats"]["unique_lpr_count"] = await db_manager.get_unique_lpr_count(lookback)
    return status

@router.get("/monitor/chart")
async def get_lpr_chart(
    interval: str = Query(default="1h", pattern="^(5m|15m|30m|1h)$"),
    lookback_hours: int = Query(default=None),
    user: dict = Depends(get_current_user)
):
    cfg = await load_config()
    if lookback_hours is None:
        lookback_hours = cfg.job.lookback_hours if cfg else 24
    
    mapping = {
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60
    }
    interval_minutes = mapping.get(interval, 60)
    
    try:
        data = await db_manager.get_lpr_chart_data(lookback_hours, interval_minutes)
        return data
    except Exception as e:
        logger.error(f"Error fetching chart data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/monitor/logs")
async def get_lpr_logs(limit: int = Query(default=50, ge=1, le=200), user: dict = Depends(get_current_user)):
    return await db_manager.get_lpr_logs(limit=limit)

@router.get("/monitor/target/history")
async def get_lpr_target_history(
    characters: str = Query(...),
    user: dict = Depends(get_current_user)
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
async def get_fr_status(user: dict = Depends(get_current_user)):
    status = fr_monitor.get_status()
    cfg = await load_config()
    lookback = cfg.fr.lookback_hours if cfg else 24
    status["stats"]["unique_fr_count"] = await db_manager.get_unique_fr_count(lookback)
    return status

@router.get("/fr/chart")
async def get_fr_chart(
    interval: str = Query(default="1h", pattern="^(5m|15m|30m|1h)$"),
    lookback_hours: int = Query(default=None),
    user: dict = Depends(get_current_user)
):
    cfg = await load_config()
    if lookback_hours is None:
        lookback_hours = cfg.fr.lookback_hours if cfg else 24
    
    mapping = {
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60
    }
    interval_minutes = mapping.get(interval, 60)
    
    try:
        data = await db_manager.get_fr_chart_data(lookback_hours, interval_minutes)
        return data
    except Exception as e:
        logger.error(f"Error fetching FR chart data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fr/logs")
async def get_fr_logs(limit: int = Query(default=50, ge=1, le=200), user: dict = Depends(get_current_user)):
    return await db_manager.get_fr_logs(limit=limit)

@router.get("/fr/target/history")
async def get_fr_target_history(
    face_target_id: str = Query(...),
    face_file: str = Query(default=None),
    detected_at: str = Query(default=None),
    face_match_id: int = Query(default=None),
    user: dict = Depends(get_current_user)
):
    """Return detections for a face target.
    First tries to retrieve cached history from the local database. If missed,
    falls back to querying Vaidio and caching the result.
    """
    cfg = await load_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="No config found")

    # 1. Try to fetch from database fr_history_cache first
    if face_match_id:
        try:
            cached_records = await db_manager.get_fr_history_cache(face_match_id)
            if cached_records is not None:
                logger.info(f"FR Target history cache HIT for face_match_id={face_match_id}")
                return cached_records
            logger.info(f"FR Target history cache MISS for face_match_id={face_match_id}")
        except Exception as cache_err:
            logger.warning(f"Error reading fr_history_cache: {cache_err}")

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
                    # Cache the fetched records in database
                    if face_match_id:
                        try:
                            await db_manager.insert_fr_history_cache(face_match_id, records)
                            logger.info(f"Cached history records in DB for face_match_id={face_match_id}")
                        except Exception as cache_err:
                            logger.warning(f"Failed to write fr_history_cache: {cache_err}")
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
