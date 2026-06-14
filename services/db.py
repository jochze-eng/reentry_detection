import os
import json
import logging
import asyncio
import asyncpg
from datetime import datetime
from models.config_model import AppConfig, VaidioConfig, JobConfig, FRConfig
from models.lpr_model import ProcessedRecord
from models.fr_model import FRProcessedRecord

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5434"))  # Default to 5434 for local dev to match remote mapped port
DB_USER = os.getenv("DB_USER", "rtd_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "rtd_password")
DB_NAME = os.getenv("DB_NAME", "recurring_target_detection")


class DatabaseManager:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self, max_retries: int = 10, delay: float = 2.0):
        if self.pool is not None:
            return
        
        logger.info(f"Connecting to database {DB_NAME} at {DB_HOST}:{DB_PORT}...")
        for attempt in range(1, max_retries + 1):
            try:
                self.pool = await asyncpg.create_pool(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    min_size=1,
                    max_size=10
                )
                logger.info("Database connection pool established.")
                return
            except Exception as e:
                logger.warning(f"Database connection attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries:
                    logger.error(f"Failed to establish database connection pool after {max_retries} attempts.")
                    raise e
                await asyncio.sleep(delay)

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Database connection pool closed.")

    async def initialize_schema(self):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            # 1. Config table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    vaidio_base_url TEXT NOT NULL,
                    vaidio_api_key TEXT NOT NULL,
                    lpr_enabled BOOLEAN NOT NULL,
                    lpr_poll_interval INTEGER NOT NULL,
                    lpr_page_size INTEGER NOT NULL,
                    lpr_lookback_hours INTEGER NOT NULL,
                    lpr_threshold INTEGER NOT NULL,
                    fr_enabled BOOLEAN NOT NULL,
                    fr_poll_interval INTEGER NOT NULL,
                    fr_lookback_hours INTEGER NOT NULL,
                    fr_threshold INTEGER NOT NULL
                )
            """)

            # 2. LPR logs table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS lpr_logs (
                    id SERIAL PRIMARY KEY,
                    license_plate_id BIGINT UNIQUE NOT NULL,
                    characters TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL,
                    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    camera_id INTEGER NOT NULL,
                    history_count INTEGER NOT NULL,
                    triggered BOOLEAN NOT NULL,
                    event_created BOOLEAN NOT NULL,
                    error_msg TEXT
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_lpr_logs_plate ON lpr_logs (characters)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_lpr_logs_detected ON lpr_logs (detected_at DESC)")
            # Add file, scene_thumbnail, position columns if they don't exist yet (migration for existing deployments)
            await conn.execute("""
                ALTER TABLE lpr_logs ADD COLUMN IF NOT EXISTS file TEXT
            """)
            await conn.execute("""
                ALTER TABLE lpr_logs ADD COLUMN IF NOT EXISTS scene_thumbnail TEXT
            """)
            await conn.execute("""
                ALTER TABLE lpr_logs ADD COLUMN IF NOT EXISTS position TEXT DEFAULT '0,0,0,0'
            """)

            # 3. FR logs table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fr_logs (
                    id SERIAL PRIMARY KEY,
                    face_match_id BIGINT UNIQUE NOT NULL,
                    face_target_id TEXT NOT NULL,
                    face_target_name TEXT NOT NULL,
                    face_file TEXT NOT NULL,
                    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    camera_id INTEGER NOT NULL,
                    history_count INTEGER NOT NULL,
                    triggered BOOLEAN NOT NULL,
                    event_created BOOLEAN NOT NULL,
                    error_msg TEXT
                )
            """)
            # Add position column if it doesn't exist yet (migration for existing deployments)
            await conn.execute("""
                ALTER TABLE fr_logs ADD COLUMN IF NOT EXISTS position TEXT DEFAULT '0,0,0,0'
            """)
            # Add confidence column if it doesn't exist yet
            await conn.execute("""
                ALTER TABLE fr_logs ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION DEFAULT 0.0
            """)
            # Add descriptor column if it doesn't exist yet
            await conn.execute("""
                ALTER TABLE fr_logs ADD COLUMN IF NOT EXISTS descriptor TEXT
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fr_logs_target ON fr_logs (face_target_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fr_logs_detected ON fr_logs (detected_at DESC)")

            # 3b. FR history cache table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fr_history_cache (
                    id SERIAL PRIMARY KEY,
                    parent_face_match_id BIGINT REFERENCES fr_logs(face_match_id) ON DELETE CASCADE,
                    face_match_id BIGINT NOT NULL,
                    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    camera_id INTEGER NOT NULL,
                    face_target_id TEXT NOT NULL,
                    face_target_name TEXT NOT NULL,
                    face_file TEXT NOT NULL,
                    scene_thumbnail TEXT NOT NULL,
                    position TEXT DEFAULT '0,0,0,0',
                    confidence DOUBLE PRECISION DEFAULT 0.0
                )
            """)
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fr_history_cache_unique ON fr_history_cache (parent_face_match_id, face_match_id)")

            # Add camera selection columns to config table if they don't exist yet
            await conn.execute("""
                ALTER TABLE config ADD COLUMN IF NOT EXISTS lpr_camera_ids TEXT DEFAULT ''
            """)
            await conn.execute("""
                ALTER TABLE config ADD COLUMN IF NOT EXISTS fr_camera_ids TEXT DEFAULT ''
            """)

            # 4. Cameras cache table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cameras (
                    camera_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    is_activate BOOLEAN NOT NULL,
                    plugins TEXT,
                    engine_models TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 5. Image Cache table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS image_cache (
                    url TEXT PRIMARY KEY,
                    content BYTEA NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_image_cache_created_at ON image_cache (created_at)")

            # Add image_cache_hours column to config table if it doesn't exist yet
            await conn.execute("""
                ALTER TABLE config ADD COLUMN IF NOT EXISTS image_cache_hours INTEGER DEFAULT 72 NOT NULL
            """)

            logger.info("Database schemas verified/initialized successfully.")

    # ── Configuration Persistence ──
    async def load_config(self) -> AppConfig | None:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM config WHERE id = 1")
            if not row:
                # Initialize default config row if empty
                default_cfg = AppConfig(
                    vaidio=VaidioConfig(base_url="https://localhost", api_key="placeholder_key_change_me"),
                    job=JobConfig(enabled=False, poll_interval_seconds=30, page_size=100, lookback_hours=24, threshold=10, camera_ids=[]),
                    fr=FRConfig(enabled=False, poll_interval_seconds=30, lookback_hours=24, threshold=3, camera_ids=[]),
                    image_cache_hours=72
                )
                await self.save_config(default_cfg)
                return default_cfg
            
            lpr_cam_str = row["lpr_camera_ids"] if "lpr_camera_ids" in row else ""
            lpr_camera_ids = [int(x) for x in lpr_cam_str.split(",") if x.strip()]
            
            fr_cam_str = row["fr_camera_ids"] if "fr_camera_ids" in row else ""
            fr_camera_ids = [int(x) for x in fr_cam_str.split(",") if x.strip()]
            
            return AppConfig(
                vaidio=VaidioConfig(
                    base_url=row["vaidio_base_url"],
                    api_key=row["vaidio_api_key"]
                ),
                job=JobConfig(
                    enabled=row["lpr_enabled"],
                    poll_interval_seconds=row["lpr_poll_interval"],
                    page_size=row["lpr_page_size"],
                    lookback_hours=row["lpr_lookback_hours"],
                    threshold=row["lpr_threshold"],
                    camera_ids=lpr_camera_ids
                ),
                fr=FRConfig(
                    enabled=row["fr_enabled"],
                    poll_interval_seconds=row["fr_poll_interval"],
                    lookback_hours=row["fr_lookback_hours"],
                    threshold=row["fr_threshold"],
                    camera_ids=fr_camera_ids
                ),
                image_cache_hours=row["image_cache_hours"] if "image_cache_hours" in row else 72
            )

    async def save_config(self, cfg: AppConfig):
        if not self.pool:
            await self.connect()

        lpr_cam_str = ",".join(map(str, cfg.job.camera_ids))
        fr_cam_str = ",".join(map(str, cfg.fr.camera_ids))

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO config (
                    id, vaidio_base_url, vaidio_api_key,
                    lpr_enabled, lpr_poll_interval, lpr_page_size, lpr_lookback_hours, lpr_threshold,
                    fr_enabled, fr_poll_interval, fr_lookback_hours, fr_threshold,
                    lpr_camera_ids, fr_camera_ids, image_cache_hours
                ) VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (id) DO UPDATE SET
                    vaidio_base_url = EXCLUDED.vaidio_base_url,
                    vaidio_api_key = EXCLUDED.vaidio_api_key,
                    lpr_enabled = EXCLUDED.lpr_enabled,
                    lpr_poll_interval = EXCLUDED.lpr_poll_interval,
                    lpr_page_size = EXCLUDED.lpr_page_size,
                    lpr_lookback_hours = EXCLUDED.lpr_lookback_hours,
                    lpr_threshold = EXCLUDED.lpr_threshold,
                    fr_enabled = EXCLUDED.fr_enabled,
                    fr_poll_interval = EXCLUDED.fr_poll_interval,
                    fr_lookback_hours = EXCLUDED.fr_lookback_hours,
                    fr_threshold = EXCLUDED.fr_threshold,
                    lpr_camera_ids = EXCLUDED.lpr_camera_ids,
                    fr_camera_ids = EXCLUDED.fr_camera_ids,
                    image_cache_hours = EXCLUDED.image_cache_hours
            """,
            cfg.vaidio.base_url,
            cfg.vaidio.api_key,
            cfg.job.enabled,
            cfg.job.poll_interval_seconds,
            cfg.job.page_size,
            cfg.job.lookback_hours,
            cfg.job.threshold,
            cfg.fr.enabled,
            cfg.fr.poll_interval_seconds,
            cfg.fr.lookback_hours,
            cfg.fr.threshold,
            lpr_cam_str,
            fr_cam_str,
            cfg.image_cache_hours
            )
            logger.info("Configuration saved to database.")

    # ── LPR Log Persistence ──
    async def insert_lpr_log(self, r: ProcessedRecord):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO lpr_logs (
                    license_plate_id, characters, confidence, detected_at, camera_id, history_count, triggered, event_created, error_msg,
                    file, scene_thumbnail, position
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (license_plate_id) DO NOTHING
            """,
            r.licensePlateId,
            r.characters,
            r.confidence,
            r.detected_at,
            r.cameraId,
            r.history_count,
            r.triggered,
            r.event_created,
            r.error,
            r.file,
            r.scene_thumbnail,
            r.position
            )

    async def get_lpr_logs(self, limit: int = 50) -> list[dict]:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT license_plate_id, characters, confidence, detected_at, camera_id, history_count, triggered, event_created, error_msg as error,
                       COALESCE(file, '') as file, COALESCE(scene_thumbnail, '') as scene_thumbnail, COALESCE(position, '0,0,0,0') as position
                FROM lpr_logs
                ORDER BY detected_at DESC
                LIMIT $1
            """, limit)
            
            logs = []
            for row in rows:
                d = dict(row)
                # Convert datetime to isoformat
                if isinstance(d["detected_at"], datetime):
                    d["detected_at"] = d["detected_at"].isoformat()
                # Rename database columns to match API JSON response schema
                d["licensePlateId"] = d.pop("license_plate_id")
                d["cameraId"] = d.pop("camera_id")
                d["history_count"] = d.pop("history_count")
                d["event_created"] = d.pop("event_created")
                d["sceneThumbnail"] = d.pop("scene_thumbnail")
                logs.append(d)
            return logs

    async def get_lpr_logs_by_target(
        self, characters: str, lookback_hours: int
    ) -> list[dict]:
        """Return all LPR log entries for specific plate characters within the lookback window."""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT license_plate_id, characters, confidence, detected_at, camera_id,
                       history_count, triggered, event_created, error_msg as error,
                       COALESCE(file, '') as file,
                       COALESCE(scene_thumbnail, '') as scene_thumbnail,
                       COALESCE(position, '0,0,0,0') as position
                FROM lpr_logs
                WHERE characters = $1
                  AND detected_at >= NOW() - ($2 * INTERVAL '1 hour')
                ORDER BY detected_at DESC
            """, characters, lookback_hours)

            records = []
            for row in rows:
                d = dict(row)
                if isinstance(d["detected_at"], datetime):
                    d["detected_at"] = d["detected_at"].isoformat()
                d["licensePlateId"] = d.pop("license_plate_id")
                d["file"] = d.pop("file")
                d["sceneThumbnail"] = d.pop("scene_thumbnail")
                d["datetime"] = d.pop("detected_at")
                d["cameraId"] = d.pop("camera_id")
                d["history_count"] = d.pop("history_count")
                d["event_created"] = d.pop("event_created")
                records.append(d)
            return records

    async def get_unique_lpr_count(self, lookback_hours: int) -> int:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COUNT(DISTINCT characters) as count
                FROM lpr_logs
                WHERE detected_at >= NOW() - ($1 * INTERVAL '1 hour')
            """, lookback_hours)
            return row["count"] if row else 0

    async def get_lpr_chart_data(self, lookback_hours: int, interval_minutes: int) -> list:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    bin_start as time,
                    COALESCE(COUNT(DISTINCT l.characters), 0)::int as count
                FROM (
                    SELECT generate_series(
                        NOW() - ($1::int * INTERVAL '1 hour'), 
                        NOW(), 
                        $2::int * INTERVAL '1 minute'
                    ) as bin_start
                ) gs
                LEFT JOIN lpr_logs l ON 
                    l.detected_at >= gs.bin_start 
                    AND l.detected_at < gs.bin_start + ($2::int * INTERVAL '1 minute')
                GROUP BY bin_start
                ORDER BY bin_start ASC;
            """, lookback_hours, interval_minutes)
            
            return [{"time": r["time"].isoformat() if r["time"] else "", "count": r["count"]} for r in rows]


    # ── FR Log Persistence ──
    async def insert_fr_log(self, r: FRProcessedRecord):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO fr_logs (
                    face_match_id, face_target_id, face_target_name, face_file, detected_at, camera_id,
                    history_count, triggered, event_created, error_msg, position, confidence, descriptor
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (face_match_id) DO NOTHING
            """,
            r.faceMatchId,
            r.faceTargetId,
            r.faceTargetName,
            r.face_file,
            r.detected_at,
            r.cameraId,
            r.history_count,
            r.triggered,
            r.event_created,
            r.error,
            r.position,
            r.confidence,
            r.descriptor
            )

    async def get_fr_descriptor_by_file(self, face_file: str) -> str | None:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT descriptor FROM fr_logs WHERE face_file = $1 AND descriptor IS NOT NULL LIMIT 1
            """, face_file)
            return row["descriptor"] if row else None

    async def update_fr_descriptor(self, face_file: str, descriptor: str):
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE fr_logs SET descriptor = $1 WHERE face_file = $2
            """, descriptor, face_file)


    async def insert_fr_history_cache(self, parent_face_match_id: int, records: list[dict]):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for r in records:
                    dt = r.get("datetime")
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt)
                    await conn.execute("""
                        INSERT INTO fr_history_cache (
                            parent_face_match_id, face_match_id, detected_at, camera_id,
                            face_target_id, face_target_name, face_file, scene_thumbnail,
                            position, confidence
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (parent_face_match_id, face_match_id) DO NOTHING
                    """,
                    parent_face_match_id,
                    int(r.get("faceMatchId") or r.get("face_match_id") or 0),
                    dt,
                    int(r.get("cameraId") or 0),
                    r.get("faceTargetId") or "unknown",
                    r.get("faceTargetName") or "unknown",
                    r.get("file") or r.get("face_file") or "",
                    r.get("sceneThumbnail") or r.get("scene_thumbnail") or "",
                    r.get("position") or "0,0,0,0",
                    float(r.get("confidence") or 0.0)
                    )

    async def get_fr_history_cache(self, parent_face_match_id: int) -> list[dict] | None:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT face_match_id, detected_at, camera_id,
                       face_target_id, face_target_name, face_file, scene_thumbnail,
                       COALESCE(position, '0,0,0,0') as position,
                       COALESCE(confidence, 0.0) as confidence
                FROM fr_history_cache
                WHERE parent_face_match_id = $1
                ORDER BY detected_at DESC
            """, parent_face_match_id)
            
            if not rows:
                return None
                
            records = []
            for row in rows:
                d = dict(row)
                if isinstance(d["detected_at"], datetime):
                    d["datetime"] = d["detected_at"].isoformat()
                else:
                    d["datetime"] = d["detected_at"]
                
                records.append({
                    "faceMatchId": d["face_match_id"],
                    "datetime": d["datetime"],
                    "cameraId": d["camera_id"],
                    "faceTargetId": d["face_target_id"],
                    "faceTargetName": d["face_target_name"],
                    "file": d["face_file"],
                    "sceneThumbnail": d["scene_thumbnail"],
                    "position": d["position"],
                    "confidence": d["confidence"]
                })
            return records


    async def get_fr_logs(self, limit: int = 50) -> list[dict]:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT face_match_id, face_target_id, face_target_name, face_file, detected_at, camera_id,
                       history_count, triggered, event_created, error_msg as error,
                       COALESCE(position, '0,0,0,0') as position,
                       COALESCE(confidence, 0.0) as confidence
                FROM fr_logs
                ORDER BY detected_at DESC
                LIMIT $1
            """, limit)
            
            logs = []
            for row in rows:
                d = dict(row)
                if isinstance(d["detected_at"], datetime):
                    d["detected_at"] = d["detected_at"].isoformat()
                d["faceMatchId"] = d.pop("face_match_id")
                d["faceTargetId"] = d.pop("face_target_id")
                d["faceTargetName"] = d.pop("face_target_name")
                d["face_file"] = d.pop("face_file")
                d["cameraId"] = d.pop("camera_id")
                d["history_count"] = d.pop("history_count")
                d["event_created"] = d.pop("event_created")
                logs.append(d)
            return logs

    async def get_fr_logs_by_target(
        self, face_target_id: str, lookback_hours: int
    ) -> list[dict]:
        """Return all FR log entries for a specific face target within the lookback window."""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT face_match_id, face_target_id, face_target_name, face_file, detected_at, camera_id,
                       history_count, triggered, event_created, error_msg as error,
                       COALESCE(position, '0,0,0,0') as position,
                       COALESCE(confidence, 0.0) as confidence
                FROM fr_logs
                WHERE face_target_id = $1
                  AND detected_at >= NOW() - ($2 * INTERVAL '1 hour')
                ORDER BY detected_at DESC
            """, face_target_id, lookback_hours)

            records = []
            for row in rows:
                d = dict(row)
                if isinstance(d["detected_at"], datetime):
                    d["detected_at"] = d["detected_at"].isoformat()
                # Normalise field names for the frontend
                d["faceMatchId"] = d.pop("face_match_id")
                d["faceTargetId"] = d.pop("face_target_id")
                d["faceTargetName"] = d.pop("face_target_name")
                # Keep 'file' key so JS grid code can use item.file
                d["file"] = d.pop("face_file")
                d["datetime"] = d.pop("detected_at")
                d["cameraId"] = d.pop("camera_id")
                records.append(d)
            return records

    async def get_fr_logs_by_face_file(self, face_file: str) -> list[dict]:
        """Return the specific DB log entry matching a face_file URL.
        Used when Vaidio similarity search returns empty for a stranger — ensures
        clicking a 1-hit row always shows at least that 1 detection record.
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT face_match_id, face_target_id, face_target_name, face_file, detected_at, camera_id,
                       history_count, triggered, event_created, error_msg as error,
                       COALESCE(position, '0,0,0,0') as position,
                       COALESCE(confidence, 0.0) as confidence
                FROM fr_logs
                WHERE face_file = $1
                ORDER BY detected_at DESC
            """, face_file)

            records = []
            for row in rows:
                d = dict(row)
                if isinstance(d["detected_at"], datetime):
                    d["detected_at"] = d["detected_at"].isoformat()
                d["faceMatchId"] = d.pop("face_match_id")
                d["faceTargetId"] = d.pop("face_target_id")
                d["faceTargetName"] = d.pop("face_target_name")
                d["file"] = d.pop("face_file")
                d["datetime"] = d.pop("detected_at")
                d["cameraId"] = d.pop("camera_id")
                records.append(d)
            return records

    async def get_unique_fr_count(self, lookback_hours: int) -> int:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COUNT(DISTINCT face_target_id) as count
                FROM fr_logs
                WHERE detected_at >= NOW() - ($1 * INTERVAL '1 hour')
            """, lookback_hours)
            return row["count"] if row else 0

    async def get_fr_chart_data(self, lookback_hours: int, interval_minutes: int) -> list:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    bin_start as time,
                    COALESCE(COUNT(DISTINCT f.face_target_id), 0)::int as count
                FROM (
                    SELECT generate_series(
                        NOW() - ($1::int * INTERVAL '1 hour'), 
                        NOW(), 
                        $2::int * INTERVAL '1 minute'
                    ) as bin_start
                ) gs
                LEFT JOIN fr_logs f ON 
                    f.detected_at >= gs.bin_start 
                    AND f.detected_at < gs.bin_start + ($2::int * INTERVAL '1 minute')
                GROUP BY bin_start
                ORDER BY bin_start ASC;
            """, lookback_hours, interval_minutes)
            
            return [{"time": r["time"].isoformat() if r["time"] else "", "count": r["count"]} for r in rows]


    async def upsert_cameras(self, cameras: list[dict]):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for cam in cameras:
                    plugins = cam.get("plugins")
                    if isinstance(plugins, list):
                        plugins_str = ",".join(plugins)
                    else:
                        plugins_str = plugins or ""

                    engine_models = cam.get("engineModels") or cam.get("engine_models")
                    if isinstance(engine_models, list):
                        engine_models_str = ",".join(engine_models)
                    else:
                        engine_models_str = engine_models or ""

                    await conn.execute("""
                        INSERT INTO cameras (camera_id, name, is_activate, plugins, engine_models, updated_at)
                        VALUES ($1, $2, $3, $4, $5, NOW())
                        ON CONFLICT (camera_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            is_activate = EXCLUDED.is_activate,
                            plugins = EXCLUDED.plugins,
                            engine_models = EXCLUDED.engine_models,
                            updated_at = NOW()
                    """,
                    int(cam["cameraId"]),
                    cam.get("name", f"Camera {cam['cameraId']}"),
                    bool(cam.get("is_activate", False)),
                    plugins_str,
                    engine_models_str
                    )

    async def get_cached_cameras(self) -> list[dict]:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM cameras ORDER BY name ASC")
            return [dict(row) for row in rows]

    async def get_last_camera_sync_time(self) -> datetime | None:
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT MAX(updated_at) as max_ts FROM cameras")
            return row["max_ts"] if row else None

    async def get_cached_image(self, url: str) -> dict | None:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT content, content_type FROM image_cache WHERE url = $1
            """, url)
            if row:
                return {"content": row["content"], "content_type": row["content_type"]}
            return None

    async def insert_cached_image(self, url: str, content: bytes, content_type: str):
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO image_cache (url, content, content_type, created_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (url) DO UPDATE SET
                    content = EXCLUDED.content,
                    content_type = EXCLUDED.content_type,
                    created_at = NOW()
            """, url, content, content_type)

    async def delete_expired_cached_images(self, lookback_hours: int):
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            if lookback_hours <= 0:
                await conn.execute("TRUNCATE TABLE image_cache")
                logger.info("Cleared all cached images (caching disabled).")
            else:
                res = await conn.execute("""
                    DELETE FROM image_cache
                    WHERE created_at < NOW() - ($1 * INTERVAL '1 hour')
                """, lookback_hours)
                logger.info(f"Deleted expired cached images: {res}")



# Singleton Database Manager Instance
db_manager = DatabaseManager()
