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
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fr_logs_target ON fr_logs (face_target_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_fr_logs_detected ON fr_logs (detected_at DESC)")

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
                    job=JobConfig(enabled=False, poll_interval_seconds=30, page_size=100, lookback_hours=24, threshold=10),
                    fr=FRConfig(enabled=False, poll_interval_seconds=30, lookback_hours=24, threshold=3)
                )
                await self.save_config(default_cfg)
                return default_cfg
            
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
                    threshold=row["lpr_threshold"]
                ),
                fr=FRConfig(
                    enabled=row["fr_enabled"],
                    poll_interval_seconds=row["fr_poll_interval"],
                    lookback_hours=row["fr_lookback_hours"],
                    threshold=row["fr_threshold"]
                )
            )

    async def save_config(self, cfg: AppConfig):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO config (
                    id, vaidio_base_url, vaidio_api_key,
                    lpr_enabled, lpr_poll_interval, lpr_page_size, lpr_lookback_hours, lpr_threshold,
                    fr_enabled, fr_poll_interval, fr_lookback_hours, fr_threshold
                ) VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                    fr_threshold = EXCLUDED.fr_threshold
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
            cfg.fr.threshold
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

    # ── FR Log Persistence ──
    async def insert_fr_log(self, r: FRProcessedRecord):
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO fr_logs (
                    face_match_id, face_target_id, face_target_name, face_file, detected_at, camera_id,
                    history_count, triggered, event_created, error_msg, position, confidence
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
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
            r.confidence
            )

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


# Singleton Database Manager Instance
db_manager = DatabaseManager()
