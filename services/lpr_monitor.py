import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from models.config_model import AppConfig
from models.lpr_model import LPRRecord, ProcessedRecord
from services.vaidio_client import VaidioClient
from services.db import db_manager

logger = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 200
INIT_LOOKBACK_SECONDS = 30
OVERLAP_SECONDS = 3


class LPRMonitor:
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
        self.recent_logs: deque[ProcessedRecord] = deque(maxlen=MAX_LOG_ENTRIES)

    # ------------------------------------------------------------------ #
    #  Start / Stop
    # ------------------------------------------------------------------ #
    async def start(self, cfg: AppConfig):
        if self.running:
            logger.warning("LPR Monitor already running — restarting with new config")
            self.stop()
            await asyncio.sleep(0.5)

        self._reset_state()
        self.running = True
        self.status = "initializing"
        self._cfg = cfg
        self._client = VaidioClient(cfg)

        now = datetime.now()
        init_start = now - timedelta(seconds=INIT_LOOKBACK_SECONDS)
        logger.info(f"LPR Monitor initializing: fetching records from {init_start} to {now}")

        try:
            records = await self._client.get_lpr_records_in_range(init_start, now)
        except Exception as e:
            self.status = "error"
            self.last_error = str(e)
            logger.error(f"LPR Monitor initialization failed: {e}")
            return

        if records:
            logger.info(f"LPR Init: found {len(records)} record(s), processing...")
            self.status = "processing"
            for record in records:
                result = await self._process_record(record, cfg)
                await db_manager.insert_lpr_log(result)
                self.recent_logs.appendleft(result)
            self.cursor_id = max(r.licensePlateId for r in records)
            self.cursor_time = max(r.datetime for r in records)
            logger.info(f"LPR Init complete. Cursor set to id={self.cursor_id}, time={self.cursor_time}")
        else:
            self.cursor_id = 0
            self.cursor_time = now
            logger.info("LPR Init: no records found. Cursor set to now.")

        self.status = "idle"
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        self.status = "stopped"
        logger.info("LPR Monitor stopped.")

    async def reload(self, cfg: AppConfig):
        """Restart with new config while preserving cursor position."""
        saved_id = self.cursor_id
        saved_time = self.cursor_time
        await self.start(cfg)
        if saved_id is not None:
            self.cursor_id = saved_id
            self.cursor_time = saved_time
            logger.info(f"LPR cursor restored to id={saved_id} after config reload")

    # ------------------------------------------------------------------ #
    #  Main Poll Loop
    # ------------------------------------------------------------------ #
    async def _loop(self):
        cfg = self._cfg
        while self.running:
            await asyncio.sleep(cfg.job.poll_interval_seconds)
            if not self.running:
                break

            self.status = "polling"
            now = datetime.now()
            query_start = self.cursor_time - timedelta(seconds=OVERLAP_SECONDS)
            logger.debug(f"LPR Polling: query_start={query_start}, cursor_id={self.cursor_id}")

            try:
                raw_records = await self._client.get_lpr_records_in_range(query_start, now)
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                self.stats["total_errors"] += 1
                logger.error(f"LPR Poll failed: {e}")
                self.status = "idle"
                continue

            new_records = [r for r in raw_records if r.licensePlateId > self.cursor_id]

            if not new_records:
                logger.debug("LPR: No new records after dedup.")
                self.status = "idle"
                continue

            logger.info(f"LPR: Found {len(new_records)} new record(s) (raw={len(raw_records)}, deduped={len(raw_records)-len(new_records)})")
            self.status = "processing"
            self.stats["total_polled"] += len(new_records)

            for record in new_records:
                result = await self._process_record(record, cfg)
                await db_manager.insert_lpr_log(result)
                self.recent_logs.appendleft(result)

            latest = max(new_records, key=lambda r: r.licensePlateId)
            self.cursor_id = latest.licensePlateId
            self.cursor_time = latest.datetime
            logger.info(f"LPR Cursor advanced to id={self.cursor_id}, time={self.cursor_time}")

            self.status = "idle"

    # ------------------------------------------------------------------ #
    #  Single Record Processing
    # ------------------------------------------------------------------ #
    async def _process_record(self, record: LPRRecord, cfg: AppConfig) -> ProcessedRecord:
        try:
            count = await self._client.get_plate_history_count(record.characters)
            self.stats["total_processed"] += 1
            logger.info(f"[LPR:{record.characters}] history_count={count} threshold={cfg.job.threshold}")

            triggered = False
            event_created = False

            pos_str = f"{record.x},{record.y},{record.w},{record.h}"

            if count > cfg.job.threshold:
                logger.warning(f"[LPR:{record.characters}] TRIGGERED — count={count} > threshold={cfg.job.threshold}")
                try:
                    event_created = await self._client.create_lpr_abnormal_event(record)
                    triggered = True
                    self.stats["total_triggered"] += 1
                except Exception as e:
                    logger.error(f"[LPR:{record.characters}] Failed to create abnormal event: {e}")
                    return ProcessedRecord(
                        licensePlateId=record.licensePlateId,
                        characters=record.characters,
                        confidence=record.confidence,
                        detected_at=record.datetime,
                        cameraId=record.cameraId,
                        history_count=count,
                        triggered=True,
                        event_created=False,
                        error=str(e),
                        file=record.file,
                        scene_thumbnail=record.sceneThumbnail,
                        position=pos_str,
                    )

            return ProcessedRecord(
                licensePlateId=record.licensePlateId,
                characters=record.characters,
                confidence=record.confidence,
                detected_at=record.datetime,
                cameraId=record.cameraId,
                history_count=count,
                triggered=triggered,
                event_created=event_created,
                file=record.file,
                scene_thumbnail=record.sceneThumbnail,
                position=pos_str,
            )

        except Exception as e:
            self.stats["total_errors"] += 1
            logger.error(f"[LPR:{record.characters}] Processing error: {e}")
            pos_str = f"{record.x},{record.y},{record.w},{record.h}" if 'record' in locals() and hasattr(record, 'x') else "0,0,0,0"
            file_val = record.file if 'record' in locals() and hasattr(record, 'file') else None
            thumb_val = record.sceneThumbnail if 'record' in locals() and hasattr(record, 'sceneThumbnail') else None
            return ProcessedRecord(
                licensePlateId=record.licensePlateId,
                characters=record.characters,
                confidence=record.confidence,
                detected_at=record.datetime,
                cameraId=record.cameraId,
                history_count=0,
                triggered=False,
                error=str(e),
                file=file_val,
                scene_thumbnail=thumb_val,
                position=pos_str,
            )

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


# Global singleton
lpr_monitor = LPRMonitor()
