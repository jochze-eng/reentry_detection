# Recurring Target Detection — Agent & Codebase Guide

## Overview

This is a **FastAPI-based monitoring service** that integrates with the **Vaidio AINVR platform** to automatically detect and flag recurring security threats. It polls the Vaidio API for two types of detection events, tracks how frequently each target appears, and creates abnormal scene events when a target exceeds a configured threshold within a lookback window.

**Two detection modes are supported:**

| Mode | What it tracks | Default threshold | Default lookback |
|------|---------------|-------------------|------------------|
| **LPR** (License Plate Recognition) | Vehicles by plate characters | 10 detections | 24 hours |
| **FR** (Face Recognition) | Individuals by face identity | 3 detections | 24 hours |

The service exposes a web UI at `https://<host>:8088` for configuration and real-time log monitoring.

---

## Architecture

```
main.py                  ← FastAPI app entry point, lifespan startup/shutdown
config.py                ← Load/save config.json (Pydantic-backed)
api/
  routes.py              ← REST endpoints (/api/config, /api/monitor/*, /api/fr/*, /api/image)
models/
  config_model.py        ← AppConfig, VaidioConfig, JobConfig, FRConfig (Pydantic)
  lpr_model.py           ← LPRRecord, ProcessedRecord
  fr_model.py            ← FRRecord, FRProcessedRecord
services/
  lpr_monitor.py         ← LPRMonitor: async polling loop for license plates
  fr_monitor.py          ← FRMonitor: async polling loop for face matches
  vaidio_client.py       ← VaidioClient: all HTTP calls to the Vaidio AINVR API
static/
  lpr.html               ← LPR Monitor dashboard page (served at /)
  fr.html                ← FR Monitor dashboard page (served at /fr)
  settings.html          ← Settings configuration page (served at /settings)
  style.css              ← Stylesheet
```

---

## How It Works

### Startup

`main.py` uses an async lifespan context manager. On startup, it reads the configuration from the database and conditionally launches `LPRMonitor` and/or `FRMonitor` as background tasks (`asyncio.create_task`) depending on whether each is enabled. This ensures that the web application starts up instantly and passes Docker healthchecks without being blocked by long-running monitor initialization loops that query external NVR endpoints. On shutdown, both monitors are stopped gracefully.

### Cursor-Based Polling Loop (shared by both monitors)

Each monitor runs an independent async polling loop:

1. **Initialize**: Fetch records from the last 30 seconds to set an initial cursor (`cursor_id`, `cursor_time`).
2. **Poll every N seconds** (configurable, default 30s):
   - Query Vaidio for records from `cursor_time - 3s` to now (3-second overlap prevents missed events).
   - Filter out records with ID ≤ `cursor_id` to deduplicate overlapping records.
   - Advance cursor to the highest ID/time in the new batch.
3. **Process each new record** (see below).
4. **Log result** to an in-memory deque (max 200 entries, FIFO).

### LPR Processing (`services/lpr_monitor.py:146–198`)

For each new license plate detection:
1. Call `get_plate_history_count()` — counts all detections of that plate string in the lookback window.
2. If count > threshold → call `create_lpr_abnormal_event()` to post a scene event to Vaidio with vehicle bounding box and snapshot image.
3. Record the result (triggered / not triggered / error) in the log.

### FR Processing (`services/fr_monitor.py:146–192`)

For each new face match detection (two-step):
1. **Extract descriptor**: POST the face image URL to `/ainvr/api/face` → get a neural embedding vector.
2. **Search history**: POST that descriptor to `/ainvr/api/face/search` → count similar faces in the lookback window.
3. If count > threshold → call `create_fr_abnormal_event()` to post a scene event with person bounding box and face thumbnail.
4. Record result in the log; thumbnail is proxied via `/api/image` to bypass CORS.

### State Machine

Each monitor transitions through these states:

```
stopped → initializing → idle → polling → processing → idle → ...
                                                      ↘ error
```

State is visible in the web UI status badge (with animated pulse during active states).

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the LPR Monitor dashboard page (`static/lpr.html`) |
| `GET` | `/fr` | Serves the FR Monitor dashboard page (`static/fr.html`) |
| `GET` | `/settings` | Serves the Settings page (`static/settings.html`) |
| `GET` | `/api/image?url=...` | Image proxy (bypasses SSL/CORS for Vaidio images) |
| `GET` | `/api/config` | Returns current config (API key masked to first 6 chars) |
| `POST` | `/api/config` | Saves config and restarts monitors |
| `POST` | `/api/config/test` | Tests connectivity to the Vaidio server |
| `GET` | `/api/cameras/by-engine` | Returns available cameras grouped by enabled engine (LPR/FR) |
| `GET` | `/api/monitor/status` | LPR monitor status snapshot (includes `unique_lpr_count` in stats) |
| `GET` | `/api/monitor/logs` | Recent LPR log entries (default 50, max 200) |
| `GET` | `/api/fr/status` | FR monitor status snapshot (includes `unique_fr_count` in stats) |
| `GET` | `/api/fr/logs` | Recent FR log entries (default 50, max 200) |
| `GET` | `/api/fr/target/history?face_file=...` | Retrieves face similarity matches history from Vaidio for target details view |

---

## Configuration & Data Persistence (PostgreSQL)

Rather than using local files (`config.json`) or in-memory arrays (caching), the application stores both its configuration parameters and processed target logs in a PostgreSQL database.

### Database Connection
Connection parameters are loaded from environment variables with the following defaults:
- `DB_HOST`: Hostname of the PostgreSQL server (default: `db` inside Docker network, or `localhost` for local dev)
- `DB_PORT`: Port (default: `5432` internally, mapped to `5434` externally on host)
- `DB_USER`: Username (default: `rtd_user`)
- `DB_PASSWORD`: Password (default: `rtd_password`)
- `DB_NAME`: Database name (default: `recurring_target_detection`)

### Database Schema

Upon startup, the application automatically initializes the following tables if they do not exist:

#### 1. `config` Table
Stores the global app configuration (exactly 1 row).
*   `id` (integer, Primary Key, constraint: `id = 1` to guarantee a single row)
*   `vaidio_base_url` (text)
*   `vaidio_api_key` (text)
*   `lpr_enabled` (boolean)
*   `lpr_poll_interval` (integer)
*   `lpr_page_size` (integer)
*   `lpr_lookback_hours` (integer)
*   `lpr_threshold` (integer)
*   `lpr_camera_ids` (text) — comma-separated string of selected camera IDs
*   `fr_enabled` (boolean)
*   `fr_poll_interval` (integer)
*   `fr_lookback_hours` (integer)
*   `fr_threshold` (integer)
*   `fr_camera_ids` (text) — comma-separated string of selected camera IDs
*   `image_cache_hours` (integer) — duration in hours to cache proxy images (default: 72 hours, 0 to disable)

#### 2. `lpr_logs` Table
Persists processed License Plate Recognition detection events.
*   `id` (serial, Primary Key)
*   `license_plate_id` (bigint, unique index)
*   `characters` (text)
*   `confidence` (double precision)
*   `detected_at` (timestamp with time zone)
*   `camera_id` (integer)
*   `history_count` (integer)
*   `triggered` (boolean)
*   `event_created` (boolean)
*   `error_msg` (text, nullable)
*   `file` (text, nullable) — cropped plate image URL
*   `scene_thumbnail` (text, nullable) — scene thumbnail URL
*   `position` (text, default '0,0,0,0') — plate bounding box coordinates

#### 3. `fr_logs` Table
Persists processed Face Recognition detection events.
*   `id` (serial, Primary Key)
*   `face_match_id` (bigint, unique index)
*   `face_target_id` (text)
*   `face_target_name` (text)
*   `face_file` (text) — face crop image URL
*   `detected_at` (timestamp with time zone)
*   `camera_id` (integer)
*   `history_count` (integer)
*   `triggered` (boolean)
*   `event_created` (boolean)
*   `error_msg` (text, nullable)
*   `position` (text, default '0,0,0,0') — face bounding box coordinates
*   `confidence` (double precision, default 0.0) — match confidence score

#### 4. `cameras` Table
Caches the list of available NVR cameras to support fast settings page loading and camera status checks.
*   `camera_id` (integer, Primary Key)
*   `name` (text)
*   `is_activate` (boolean) — true if camera is activated, false if deactivated
*   `plugins` (text, nullable) — comma-separated string of active plugins on this camera
*   `engine_models` (text, nullable) — comma-separated string of running engines on this camera
*   `updated_at` (timestamp with time zone) — last cache update timestamp

#### 5. `image_cache` Table
Persists proxied images to avoid slow rendering times in the web UI.
*   `url` (text, Primary Key) — image source URL
*   `content` (bytea) — raw image binary data
*   `content_type` (text) — MIME type (e.g. `image/jpeg`)
*   `created_at` (timestamp with time zone) — creation timestamp used for automatic daily/hourly cleanup

#### 6. `fr_history_cache` Table
Persists dynamically-grouped historical match records for face detections to support instant loading on details expansion.
*   `id` (serial, Primary Key)
*   `parent_face_match_id` (bigint, Foreign Key to `fr_logs(face_match_id)` on delete cascade)
*   `face_match_id` (bigint)
*   `detected_at` (timestamp with time zone)
*   `camera_id` (integer)
*   `face_target_id` (text)
*   `face_target_name` (text)
*   `face_file` (text) — face crop image URL
*   `scene_thumbnail` (text) — scene thumbnail URL
*   `position` (text, default '0,0,0,0') — face bounding box coordinates
*   `confidence` (double precision, default 0.0) — match confidence score
*   Unique index on `(parent_face_match_id, face_match_id)` to prevent duplicates.

---

## Direct Vaidio NVR Search & Image Optimization

To guarantee backward compatibility with legacy database records and optimize network load times:
1. **Target History Routing**: For Face Recognition (FR), target history is retrieved directly from the local `fr_history_cache` database table (populated during polling or cached on first fallback load). This completely bypasses slow Vaidio NVR queries and loads instantly (sub-10ms). For License Plate Recognition (LPR) or legacy FR records (cache miss), history views query the Vaidio NVR search APIs directly (`/ainvr/api/lpr/plates` and `/ainvr/api/face/search` via face descriptors) to retrieve occurrence lists, coordinates, and images, and cache FR results to the local DB for subsequent loads.
2. **Lightweight Thumbnails**: Frontend grids on the detail expansion pages load small `_thumbnail.jpg` scene images (10-30KB) instead of high-res scene snapshots (300KB - 1MB+), preventing connection queuing and ensuring fast rendering. Full-res snapshots are fetched on-demand inside the magnifying glass zoom modal.
3. **Database Caching of Camera List & Status**: To guarantee sub-10ms settings page load times, the list of cameras (with their corresponding `"Activate"` / `"Deactivate"` status) is cached in the local PostgreSQL database. If the cache is stale (older than 5 minutes), a FastAPI background task asynchronously updates it from the Vaidio NVR without blocking the UI request.
4. **Database Image Proxy Caching**: To accelerate face target details, license plate cards, and image grid loading times, requests to the `/api/image` endpoint are intercepted and cached in the local PostgreSQL database for a configurable retention window (default 72 hours). Setting this window to 0 disables the cache and purges all records.
5. **Background Image Pre-Warming**: When a face match is processed and triggered, a background task automatically pre-fetches and caches all history face crops and scene thumbnails into the local `image_cache` table, guaranteeing instant image rendering when the user expands the detail view.

---

## System & API Logging (7-day Rotating Logs)

The service implements a detailed logging mechanism for system activities, incoming client API calls, outgoing requests to the Vaidio server, and errors/exceptions.

### Log Rotation & Storage
- **File Rotation**: Log records are written using a `TimedRotatingFileHandler` with daily rotation (`when='D'`, `interval=1`) retaining a backup count of 7 (`backupCount=7`) to guarantee 7 days of rolling logs.
- **Log Location**: Logs are written to `/app/logs/app.log` inside the container. This is mapped via Docker volumes to `./logs/app.log` on the host. If the directory is not writable, it falls back to a local `logs/` directory inside the project root.
- **Formatter**: `%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s`

### Logging Scope
1. **Incoming Request Middleware**: Logged in `main.py` using a FastAPI middleware that intercepts all incoming client HTTP calls, logging the request method, path, query parameters, response status codes, processing time (duration in ms), and any unhandled exceptions (along with their stack trace).
2. **Outgoing Vaidio Client HTTP Calls**: Logged in `services/vaidio_client.py` using HTTPX event hooks on `httpx.AsyncClient`:
   - **Request Hook**: Logs method, URL, headers, and request body (truncating large body texts and logging `[Multipart/Form-Data File Upload]` for file uploads to avoid bloating). Sensitive header parameters like `Vaidio-API-key` are masked.
   - **Response Hook**: Logs method, URL, status code, and response body (truncating responses to 1000 characters and summarizing binary content types like images).
3. **Uvicorn Server Logs**: Server logs from uvicorn loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) are also piped to the rotating file handler.

---

## Docker & Deployment Environment

### Production Docker Container Status on Test PC

The service is currently running in a Docker container on the remote test machine (`100.101.159.22`).

**Container Configuration details:**
- **Container Name**: `recurring_target_detection`
- **Image**: `recurring-target-detection:latest`
- **Port Mapping**: `8088:8088` (external web UI is accessible at `https://100.101.159.22:8088`)
- **Docker Network**: Runs in its own private network with a dedicated database container named `rtd_db` (accessible inside the docker-compose stack as service `db` on port 5432).
- **Config & Log persistence**: stored in the dedicated database container `rtd_db` using volume `rtd_db_data` (mapped to host port `5434` for external access).
- **System time / TZ**: synchronized with host `/etc/localtime`, set to `Asia/Singapore`.

### Remote Test PC Credentials
- **IP Address**: `100.101.159.22`
- **Username**: `superuser`
- **Password**: `usersuper888`

---

## HTTPS & SSL Certificate Configuration

The application enforces HTTPS (SSL/TLS) for secure web access, served on port `8088`.

- **Certificate Generation**: A self-signed certificate and private key are generated using OpenSSL:
  ```bash
  openssl req -x509 -nodes -days 36500 -newkey rsa:2048 -keyout certs/server.key -out certs/server.crt -subj "/C=SG/O=Vaidio/CN=localhost"
  ```
  This certificate is valid for 100 years (36,500 days), ensuring it never expires in practice.
- **Docker Integration**:
  - The `certs/` folder is copied into the Docker container.
  - Uvicorn is executed with SSL flags: `--ssl-keyfile certs/server.key --ssl-certfile certs/server.crt`.
  - The container healthcheck is updated to use HTTPS and ignore certificate validation errors (since it is self-signed):
    `CMD python -c "import urllib.request, ssl; urllib.request.urlopen('https://localhost:8088/', context=ssl._create_unverified_context())" || exit 1`
- **Local Execution**:
  If certificates exist under the `certs/` folder locally, `main.py` automatically initializes Uvicorn with SSL parameters, running it in HTTPS mode. If they do not exist, it falls back to HTTP.

---

## User Management & Role-Based Access Control (RBAC)

The application implements database-backed user authentication and authorization using cookie-based sessions (`session_token`).

### User Groups & Roles
Two roles are supported to enforce access control:
1.  **Administrator**: Full access to all monitoring dashboards, target history, and settings configuration. Administrators can manage all user accounts (create users, reset passwords, delete users) via the User Management panel.
2.  **Operator**: Restricted access. Operators can view the LPR and FR dashboards and logs, but have no access to the Settings configuration or the User Management panel.

### Authentication Database Tables

#### 1. `users` Table
Stores registered accounts and their hashed credentials.
*   `id` (serial, Primary Key)
*   `username` (text, unique index)
*   `password_hash` (text) — salted PBKDF2-SHA256 password hash
*   `role` (text) — constraint: `role IN ('Administrator', 'Operator')`

#### 2. `user_sessions` Table
Persists active user sessions for stateful authorization.
*   `session_token` (text, Primary Key) — random 32-byte hex token
*   `username` (text)
*   `role` (text)
*   `expires_at` (timestamp with time zone)

### Security Enforcement
*   **Frontend**: Sidebars dynamically hide administrative links (`/settings` and `/users`) from users with the `Operator` role. AJAX requests automatically redirect to `/login` if a `401 Unauthorized` response is received.
*   **Backend Page Routes**: Router intercepts page requests to `/settings` and `/users` and redirects unauthorized users.
*   **Backend API Endpoints**: Endpoints are wrapped in FastAPI dependency injection:
    *   `Depends(get_current_user)` checks if a request includes a valid session cookie.
    *   `Depends(require_admin)` guarantees that only users in the `Administrator` group can call config-related and user-administration APIs.

---

## Key Implementation Notes

- **SSL verification disabled** (`verify=False` in `vaidio_client.py:20`) to support self-signed Vaidio certificates.
- **Overlap window**: 3-second overlap on each poll prevents missed events at interval boundaries.
- **JPEG validation**: Image bytes are validated by checking for `\xff\xd8` magic bytes before uploading to Vaidio.
- **Image URL normalization**: Suffixes like `_face1.jpg` and `_thumbnail.jpg` are stripped before downloading full images.
- **Scene Image Suffix Derivation**: Resolves the original scene snapshot URL directly from face crop URLs without extra NVR queries (substituting `_crop.jpg` or `_face*.jpg` naming conventions).
- **Dynamic Bounding Box & Magnifier**: Bounding boxes scale reactively based on natural-to-client dimension ratios, coupled with a hover-based magnifying zoom lens.
- **Async throughout**: All I/O (HTTP, file ops) uses `asyncio` / `httpx` / `aiofiles` — no blocking calls.
- **OnError Infinite Loop Fixed**: Resolved a critical bug where failed image loads recursively triggered the `onerror` handler by setting `this.src` to the stylesheet `/static/style.css`. Failed loads now cleanly hide images via `display='none'` and terminate the handler.
- **Remote Machine Deployment**: The image caching system and the `onerror` loop fix have been fully deployed and verified on the remote Ubuntu test PC (`100.101.159.22`) using rsync and container rebuilds.
- **FR Detail View — Time-Anchored History**: When expanding an FR log row, the `detected_at` timestamp from the clicked row is passed to `/api/fr/target/history`. The Vaidio face search window is `detected_at - lookback_hours → detected_at` (converted to local SGT time), exactly replicating the monitor's evaluation window so `history_count` matches the number of results shown.
- **FR Detail View — Flat Search API Parsing**: The Vaidio `/ainvr/api/face/search` endpoint returns a flat structure (`faceKeyId`, `cameraId`, `file`, `position`, `confidence` directly on each item), unlike the match-polling API which uses nested `faceKey`/`faceTarget` objects. `search_face_history()` was fixed to read the correct flat fields.
- **FR Detail View — Stranger DB Fallback**: When Vaidio's similarity search returns empty for a stranger detection (no similar faces in the window), the route falls back to `get_fr_logs_by_face_file(face_file)` to return the specific DB record so the detail page always shows at least the clicked detection.
- **FR Detail View — Auto-Refresh Guard**: `updateFRLogs()` in `fr.html` now has a `if (currentView === 'details') return;` guard to prevent the 3-second polling loop from overwriting the detail view.
- **sceneThumbnail Derivation**: `search_face_history()` now derives `sceneThumbnail` from the face crop URL using `re.sub(r'_face(?:\d+|_\d+_crop)\.jpg$', '_thumbnail.jpg', face_file)`.
- **FR Detail View Deployment**: All FR Monitor detail page fixes deployed and verified on remote Ubuntu test PC (`100.101.159.22`) using rsync and container rebuilds.
