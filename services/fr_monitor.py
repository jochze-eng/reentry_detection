import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from models.config_model import AppConfig
from models.fr_model import FRRecord, FRProcessedRecord
from services.vaidio_client import VaidioClient
from services.db import db_manager

logger = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 200
INIT_LOOKBACK_SECONDS = 30
OVERLAP_SECONDS = 3


class FRMonitor:
    def __init__(self):
        self._reset_state()

    def _reset_state(self):
        self.status: str = "stopped"
        self.cursor_id: int | None = None
        self.cursor_time: datetime | None = None
        self.running: bool = False
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.stats = {
            "total_polled": 0,
            "total_processed": 0,
            "total_triggered": 0,
            "total_errors": 0,
        }
        self.recent_logs: deque[FRProcessedRecord] = deque(maxlen=MAX_LOG_ENTRIES)

    # ------------------------------------------------------------------ #
    #  Start / Stop
    # ------------------------------------------------------------------ #
    async def start(self, cfg: AppConfig):
        if self.running:
            logger.warning("FR Monitor already running — restarting with new config")
            self.stop()
            await asyncio.sleep(0.5)

        self._reset_state()
        self.running = True
        self.status = "initializing"
        self._cfg = cfg
        self._client = VaidioClient(cfg)

        now = datetime.now()
        init_start = now - timedelta(seconds=INIT_LOOKBACK_SECONDS)
        logger.info(f"FR Monitor initializing: fetching records from {init_start} to {now}")

        try:
            records = await self._client.get_fr_records_in_range(init_start, now)
        except Exception as e:
            self.status = "error"
            self.last_error = str(e)
            logger.error(f"FR Monitor initialization failed: {e}")
            return

        if records:
            logger.info(f"FR Init: found {len(records)} record(s), processing...")
            self.status = "processing"
            for record in records:
                result, history_records = await self._process_record(record, cfg)
                await db_manager.insert_fr_log(result)
                if history_records:
                    await db_manager.insert_fr_history_cache(result.faceMatchId, history_records)
                    if cfg.image_cache_hours > 0:
                        urls = []
                        for hr in history_records:
                            if hr.get("file"):
                                urls.append(hr["file"])
                            if hr.get("sceneThumbnail"):
                                urls.append(hr["sceneThumbnail"])
                        if urls:
                            asyncio.create_task(pre_warm_image_cache(self._client, urls))
                self.recent_logs.appendleft(result)
            self.cursor_id = max(r.faceMatchId for r in records)
            self.cursor_time = max(r.datetime for r in records)
            logger.info(f"FR Init complete. Cursor set to id={self.cursor_id}, time={self.cursor_time}")
        else:
            self.cursor_id = 0
            self.cursor_time = now
            logger.info("FR Init: no records found. Cursor set to now.")

        self.status = "idle"
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        self.status = "stopped"
        logger.info("FR Monitor stopped.")

    async def reload(self, cfg: AppConfig):
        """Restart with new config while preserving cursor position."""
        saved_id = self.cursor_id
        saved_time = self.cursor_time
        await self.start(cfg)
        if saved_id is not None:
            self.cursor_id = saved_id
            self.cursor_time = saved_time
            logger.info(f"FR cursor restored to id={saved_id} after config reload")

    # ------------------------------------------------------------------ #
    #  Main Poll Loop
    # ------------------------------------------------------------------ #
    async def _loop(self):
        cfg = self._cfg
        while self.running:
            await asyncio.sleep(cfg.fr.poll_interval_seconds)
            if not self.running:
                break

            self.status = "polling"
            now = datetime.now()
            query_start = self.cursor_time - timedelta(seconds=OVERLAP_SECONDS)
            logger.debug(f"FR Polling: query_start={query_start}, cursor_id={self.cursor_id}")

            try:
                raw_records = await self._client.get_fr_records_in_range(query_start, now)
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                self.stats["total_errors"] += 1
                logger.error(f"FR Poll failed: {e}")
                self.status = "idle"
                continue

            new_records = [r for r in raw_records if r.faceMatchId > self.cursor_id]

            if not new_records:
                logger.debug("FR: No new records after dedup.")
                self.status = "idle"
                continue

            logger.info(f"FR: Found {len(new_records)} new record(s) (raw={len(raw_records)}, deduped={len(raw_records)-len(new_records)})")
            self.status = "processing"
            self.stats["total_polled"] += len(new_records)

            for record in new_records:
                result, history_records = await self._process_record(record, cfg)
                await db_manager.insert_fr_log(result)
                if history_records:
                    await db_manager.insert_fr_history_cache(result.faceMatchId, history_records)
                    if cfg.image_cache_hours > 0:
                        urls = []
                        for hr in history_records:
                            if hr.get("file"):
                                urls.append(hr["file"])
                            if hr.get("sceneThumbnail"):
                                urls.append(hr["sceneThumbnail"])
                        if urls:
                            asyncio.create_task(pre_warm_image_cache(self._client, urls))
                self.recent_logs.appendleft(result)

            latest = max(new_records, key=lambda r: r.faceMatchId)
            self.cursor_id = latest.faceMatchId
            self.cursor_time = latest.datetime
            logger.info(f"FR Cursor advanced to id={self.cursor_id}, time={self.cursor_time}")

            self.status = "idle"

    # ------------------------------------------------------------------ #
    #  Single Record Processing
    # ------------------------------------------------------------------ #
    async def _process_record(self, record: FRRecord, cfg: AppConfig) -> tuple[FRProcessedRecord, list[dict]]:
        descriptor = None
        history_records = []
        try:
            # Check if category is excluded
            is_excluded = False
            if record.faceTargetCategory and cfg.fr.exclude_categories:
                if record.faceTargetCategory in cfg.fr.exclude_categories:
                    is_excluded = True
                    logger.info(f"[FR:{record.faceTargetName}] Excluded from triggering due to category: {record.faceTargetCategory}")

            # Step 1: get descriptor from face image
            descriptor = await self._client.get_face_descriptor(record.file)
            if not descriptor:
                raise ValueError("Empty descriptor returned")

            # Step 2: search history records using descriptor with causal anchor + 5s grace period
            history_records = await self._client.search_face_history(
                descriptor,
                anchor_dt=record.datetime,
                lookback_hours=cfg.fr.lookback_hours
            )
            count = len(history_records)
            self.stats["total_processed"] += 1
            logger.info(f"[FR:{record.faceTargetName}] history_count={count} threshold={cfg.fr.threshold}")

            triggered = False
            event_created = False

            if count > cfg.fr.threshold:
                if is_excluded:
                    logger.info(f"[FR:{record.faceTargetName}] Threshold exceeded ({count} > {cfg.fr.threshold}), but category '{record.faceTargetCategory}' is excluded from triggering.")
                else:
                    logger.warning(f"[FR:{record.faceTargetName}] TRIGGERED — count={count} > threshold={cfg.fr.threshold}")
                    try:
                        event_created = await self._client.create_fr_abnormal_event(record)
                        triggered = True
                        self.stats["total_triggered"] += 1
                    except Exception as e:
                        logger.error(f"[FR:{record.faceTargetName}] Failed to create abnormal event: {e}")
                        return FRProcessedRecord(
                            faceMatchId=record.faceMatchId,
                            faceTargetId=record.faceTargetId,
                            faceTargetName=record.faceTargetName,
                            face_file=record.file,
                            detected_at=record.datetime,
                            cameraId=record.cameraId,
                            history_count=count,
                            triggered=True,
                            event_created=False,
                            error=str(e),
                            position=record.position,
                            confidence=record.confidence,
                            descriptor=descriptor,
                        ), history_records

            return FRProcessedRecord(
                faceMatchId=record.faceMatchId,
                faceTargetId=record.faceTargetId,
                faceTargetName=record.faceTargetName,
                face_file=record.file,
                detected_at=record.datetime,
                cameraId=record.cameraId,
                history_count=count,
                triggered=triggered,
                event_created=event_created,
                position=record.position,
                confidence=record.confidence,
                descriptor=descriptor,
            ), history_records

        except Exception as e:
            self.stats["total_errors"] += 1
            logger.error(f"[FR:{record.faceTargetName}] Processing error: {e}")
            return FRProcessedRecord(
                faceMatchId=record.faceMatchId,
                faceTargetId=record.faceTargetId,
                faceTargetName=record.faceTargetName,
                face_file=record.file,
                detected_at=record.datetime,
                cameraId=record.cameraId,
                history_count=0,
                triggered=False,
                error=str(e),
                position=record.position,
                confidence=record.confidence,
                descriptor=descriptor,
            ), []

    # ------------------------------------------------------------------ #
    #  Status Snapshot (for API responses)
    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        return {
            "status": self.status,
            "cursor_id": self.cursor_id,
            "cursor_time": self.cursor_time.isoformat() if self.cursor_time else None,
            "last_error": self.last_error,
            "stats": self.stats,
        }

    def get_logs(self, limit: int = 50) -> list[dict]:
        return [r.dict() for r in list(self.recent_logs)[:limit]]


async def pre_warm_image_cache(client: VaidioClient, urls: list[str]):
    for url in urls:
        if not url:
            continue
        try:
            cached = await db_manager.get_cached_image(url)
            if not cached:
                logger.info(f"Pre-warming image cache for {url}...")
                async with client._client(timeout=10) as http_client:
                    r = await http_client.get(url)
                    r.raise_for_status()
                    content_type = r.headers.get("content-type", "image/jpeg")
                    await db_manager.insert_cached_image(url, r.content, content_type)
        except Exception as e:
            logger.warning(f"Failed to pre-warm image cache for {url}: {e}")


# Global singleton
fr_monitor = FRMonitor()
