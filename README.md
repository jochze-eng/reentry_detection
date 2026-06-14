# Recurring Target Detection Service

This is a FastAPI-based background monitoring service integrated with the **Vaidio AINVR** platform. It tracks how frequently specific individuals (via Face Recognition) and vehicles (via License Plate Recognition) appear on site and triggers automated alarm events when a target's detection count exceeds a configured threshold within a set lookback window.

---

## Key Features

1. **Dashboard Monitors**:
   - Separate dashboards for **License Plate Recognition (LPR)** and **Face Recognition (FR)**.
   - Live status indicators and real-time log streaming using cursor-based polling.
   - Shows resolved **Camera Names** (e.g. `Main Entrance`) instead of raw integer camera IDs.
   - Live metrics showing unique license plates and faces processed within their lookback periods.

2. **Detailed Target Expansion View**:
   - Click any log row to open a detailed slide-in dashboard for that specific target.
   - **Appearance History Timeline**: Day/Hour bar chart visualization showing target frequency over time.
   - **Matched Detections Grid**: Displays chronological occurrence cards showing the crop snapshot, camera name, confidence score, and timestamp.

3. **Interactive Bounding Box & Magnifier Zoom Modal**:
   - Click any detection card to open the high-resolution original scene image in a modal.
   - A reactive **red bounding box** highlights the exact target (face or license plate) position.
   - **Hover Magnifying Glass**: An interactive magnifying lens zooms in on the target area upon hover.
   - Option to toggle the bounding box, magnifying lens, and download the full-resolution image.

4. **Image & Connection Optimization**:
   - Renders lightweight `_thumbnail.jpg` files (10-30KB) in the detections grid to prevent browser concurrent request bottlenecks.
   - Original full-resolution scene snapshots are only fetched on-demand when zooming.

---

## Technology Stack

- **Backend**: FastAPI (Python 3.11), HTTPX (async REST client), Asyncpg (async PostgreSQL driver).
- **Database**: PostgreSQL (persists logs, status cursors, and configuration).
- **Frontend**: Single Page Application using HTML5, Vanilla CSS (custom glassmorphism style), and asynchronous vanilla Javascript.
- **Deployment**: Docker and Docker Compose.

---

## Setup & Running

### Requirements
- Docker and Docker Compose installed.
- Access to a Vaidio AINVR NVR server.

### 1. Configure Environment
Set the following environment variables (or specify them in a `.env` file):
- `DB_HOST`: Database container hostname (default: `db`)
- `DB_PORT`: Database port (default: `5432` internally, mapped to `5434`)
- `DB_USER`: Database username (default: `rtd_user`)
- `DB_PASSWORD`: Database password (default: `rtd_password`)
- `DB_NAME`: Database name (default: `recurring_target_detection`)

### 2. Start Service
Run the following command in the project root:
```bash
docker compose up -d --build
```
This starts two containers:
- `rtd_db`: PostgreSQL container.
- `recurring_target_detection`: The FastAPI monitor app.

The web UI is accessible at `http://<host_ip>:8088`.

### 3. API Key & Settings Configuration
Navigate to `http://<host_ip>:8088/settings` to configure:
- Vaidio Base URL and API Key.
- Enabling/Disabling LPR and FR monitors independently.
- Setting poll intervals, lookback window (hours), and occurrence thresholds.
