import os
import logging
import time
import asyncio
from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from datetime import datetime, timezone
from api.routes import router, get_license_state
from services.lpr_monitor import lpr_monitor
from services.fr_monitor import fr_monitor
from config import load_config
from services.db import db_manager

# Ensure log directory exists and is writable
log_dir = "/app/logs"
writable = False
if os.path.exists(log_dir):
    try:
        # Test writability
        test_file = os.path.join(log_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("")
        os.remove(test_file)
        writable = True
    except OSError:
        pass
else:
    try:
        os.makedirs(log_dir, exist_ok=True)
        writable = True
    except OSError:
        pass

if not writable:
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

log_file = os.path.join(log_dir, "app.log")

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Remove existing basicConfig handlers to avoid duplication
for h in list(root_logger.handlers):
    root_logger.removeHandler(h)

# Formatter
log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
)

# Console Handler (stdout)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)
root_logger.addHandler(console_handler)

# Timed Rotating File Handler (7 days rotation)
file_handler = TimedRotatingFileHandler(
    log_file,
    when="D",
    interval=1,
    backupCount=7,
    encoding="utf-8"
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)
root_logger.addHandler(file_handler)

# Ensure uvicorn logs also go to the file
for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    uv_logger = logging.getLogger(logger_name)
    uv_logger.addHandler(file_handler)

logger = logging.getLogger("app.main")


async def image_cache_cleanup_loop():
    logger.info("Starting image cache cleanup loop...")
    while True:
        try:
            cfg = await load_config()
            if cfg:
                await db_manager.delete_expired_cached_images(cfg.image_cache_hours)
        except Exception as e:
            logger.error(f"Error in image cache cleanup loop: {e}")
        await asyncio.sleep(3600)  # Run once every hour


async def license_watch_loop(app: FastAPI):
    """Re-evaluate the license periodically so an expiry (past grace) or a tampered
    token locks the running app down and stops the monitors."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            state = (await get_license_state())["state"]
            if state != app.state.license_state:
                logging.warning(f"License state changed: {app.state.license_state} -> {state}")
                app.state.license_state = state
            if state == "locked":
                lpr_monitor.stop()
                fr_monitor.stop()
        except Exception as e:
            logging.error(f"Error in license watch loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await db_manager.connect()
        await db_manager.initialize_schema()

        # Start periodic image cache cleanup task
        asyncio.create_task(image_cache_cleanup_loop())

        # Evaluate license before starting any monitors (soft-lock enforcement).
        app.state.license_state = (await get_license_state())["state"]
        logging.info(f"License state at startup: {app.state.license_state}")
        asyncio.create_task(license_watch_loop(app))

        cfg = await load_config()
        if app.state.license_state == "locked":
            logging.warning("License not valid — monitors held until a valid license is activated.")
        elif cfg:
            if cfg.job.enabled:
                logging.info("LPR enabled in config, starting LPR monitor...")
                asyncio.create_task(lpr_monitor.start(cfg))
            if cfg.fr.enabled:
                logging.info("FR enabled in config, starting FR monitor...")
                asyncio.create_task(fr_monitor.start(cfg))
            if not cfg.job.enabled and not cfg.fr.enabled:
                logging.info("Config found but both monitors are disabled.")
        else:
            logging.info("No config found. Please configure via the web UI.")
    except Exception as e:
        logging.error(f"Lifespan startup error: {e}")
    yield
    lpr_monitor.stop()
    fr_monitor.stop()
    await db_manager.disconnect()

app = FastAPI(
    title="Vaidio LPR & FR Monitor",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Safe default until the lifespan startup evaluates the real license state.
app.state.license_state = "locked"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://100.101.159.22:8088",
        "https://localhost:8088",
        "http://localhost:8088",
        "https://127.0.0.1:8088",
        "http://127.0.0.1:8088"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: *; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Routes reachable while the app is license-locked, so an admin can always
# log in and activate a valid license.
_LICENSE_ALLOWED_PREFIXES = ("/static/", "/api/license/")
_LICENSE_ALLOWED_EXACT = {
    "/login", "/license", "/favicon.ico",
    "/api/login", "/api/logout", "/api/change-default-password", "/api/user/me",
}

@app.middleware("http")
async def license_gate(request: Request, call_next):
    if getattr(app.state, "license_state", "locked") != "locked":
        return await call_next(request)
    path = request.url.path
    if path in _LICENSE_ALLOWED_EXACT or any(path.startswith(p) for p in _LICENSE_ALLOWED_PREFIXES):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(
            {"detail": "License required. Activate a valid license to continue."},
            status_code=403,
        )
    return RedirectResponse(url="/license")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    method = request.method
    path = request.url.path
    query = str(request.query_params)
    
    logger.info(f"Incoming Request: {method} {path} query={query}")
    
    start_time = time.time()
    try:
        response = await call_next(request)
        duration = (time.time() - start_time) * 1000
        logger.info(f"Incoming Response: {method} {path} -> Status {response.status_code} ({duration:.2f}ms)")
        return response
    except Exception as e:
        duration = (time.time() - start_time) * 1000
        logger.error(f"Incoming Request Failed: {method} {path} -> Error: {e} ({duration:.2f}ms)", exc_info=True)
        raise

app.include_router(router, prefix="/api")

app.mount("/static", StaticFiles(directory="static"), name="static")

async def get_session_user(request: Request) -> dict | None:
    token = request.cookies.get("session_token")
    if not token:
        return None
    session = await db_manager.get_session(token)
    if not session:
        return None
    expires = session["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        await db_manager.delete_session(token)
        return None
    return session

@app.get("/")
async def serve_lpr(request: Request):
    user = await get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return FileResponse("static/lpr.html")

@app.get("/fr")
async def serve_fr(request: Request):
    user = await get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return FileResponse("static/fr.html")

@app.get("/settings")
async def serve_settings(request: Request):
    user = await get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
    if user["role"] != "Administrator":
        return RedirectResponse(url="/")
    return FileResponse("static/settings.html")

@app.get("/users")
async def serve_users(request: Request):
    user = await get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
    if user["role"] != "Administrator":
        return RedirectResponse(url="/")
    return FileResponse("static/users.html")

@app.get("/license")
async def serve_license(request: Request):
    user = await get_session_user(request)
    if not user:
        return RedirectResponse(url="/login")
    # While locked, any logged-in user may land here (avoids a redirect loop);
    # otherwise the license page is admin-only like Settings/Users.
    locked = getattr(request.app.state, "license_state", "ok") == "locked"
    if user["role"] != "Administrator" and not locked:
        return RedirectResponse(url="/")
    return FileResponse("static/license.html")

@app.get("/login")
async def serve_login(request: Request):
    user = await get_session_user(request)
    if user:
        return RedirectResponse(url="/")
    return FileResponse("static/login.html")

if __name__ == "__main__":
    import uvicorn
    ssl_keyfile = "certs/server.key"
    ssl_certfile = "certs/server.crt"
    if os.path.exists(ssl_keyfile) and os.path.exists(ssl_certfile):
        logger.info("SSL certificates found. Starting server in HTTPS mode...")
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8088,
            reload=False,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile
        )
    else:
        logger.info("SSL certificates not found. Starting server in HTTP mode...")
        uvicorn.run("main:app", host="0.0.0.0", port=8088, reload=False)
