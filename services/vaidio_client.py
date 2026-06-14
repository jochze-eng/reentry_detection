import httpx
import json
import re
import logging
from datetime import datetime, timedelta
from models.lpr_model import LPRRecord
from models.fr_model import FRRecord
from models.config_model import AppConfig

logger = logging.getLogger(__name__)


class VaidioClient:
    def __init__(self, cfg: AppConfig):
        self.base_url = cfg.vaidio.base_url.rstrip("/")
        self.headers = {"Vaidio-API-key": cfg.vaidio.api_key}
        self.job = cfg.job
        self.fr = cfg.fr
        # Disable SSL verification to support self-signed certificates
        self._http = {"verify": False, "timeout": 15}

    def _client(self, timeout: int = 15) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            event_hooks={
                "request": [self._log_request],
                "response": [self._log_response],
            }
        )

    async def _log_request(self, request: httpx.Request):
        headers = dict(request.headers)
        if "vaidio-api-key" in headers:
            key = headers["vaidio-api-key"]
            headers["vaidio-api-key"] = key[:6] + "*" * (len(key) - 6) if len(key) > 6 else "***"
        
        body_summary = ""
        content_type = headers.get("content-type", "")
        if "multipart" in content_type:
            body_summary = "[Multipart/Form-Data File Upload]"
        else:
            try:
                content = await request.aread()
                if content:
                    if len(content) > 1000:
                        body_summary = content[:1000].decode("utf-8", errors="replace") + " ... [TRUNCATED]"
                    else:
                        body_summary = content.decode("utf-8", errors="replace")
                else:
                    body_summary = "[Empty Body]"
            except Exception as e:
                body_summary = f"[Unreadable Stream/Body: {e}]"
        
        logger.info(
            f"Vaidio API Request: {request.method} {request.url}\n"
            f"Headers: {json.dumps(headers)}\n"
            f"Body: {body_summary}"
        )

    async def _log_response(self, response: httpx.Response):
        request = response.request
        content_type = response.headers.get("content-type", "").lower()
        
        # Ensure the response content is read before accessing response.text
        try:
            await response.aread()
        except Exception:
            pass
        
        if "image" in content_type or "octet-stream" in content_type:
            try:
                content_len = len(response.content)
            except Exception:
                content_len = "unknown"
            body_summary = f"[Binary Content, {content_len} bytes]"
        else:
            try:
                text = response.text
                if len(text) > 1000:
                    body_summary = text[:1000] + " ... [TRUNCATED]"
                else:
                    body_summary = text
            except Exception as e:
                body_summary = f"[Failed to decode response text: {e}]"
                
        logger.info(
            f"Vaidio API Response: {request.method} {request.url} -> Status {response.status_code}\n"
            f"Body: {body_summary}"
        )

    # ------------------------------------------------------------------ #
    #  LPR: raw paginated GET to /ainvr/api/lpr/plates
    # ------------------------------------------------------------------ #
    async def _get_lpr_page(
        self,
        start: datetime,
        end: datetime,
        page: int = 0,
        characters: str | None = None,
    ) -> dict:
        params = {
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end":   end.strftime("%Y-%m-%d %H:%M:%S"),
            "page": page,
            "size": self.job.page_size,
        }
        if self.job.camera_ids:
            params["cameraIds"] = ",".join(map(str, self.job.camera_ids))
        else:
            params["allCameras"] = "true"

        if characters:
            params["characters"] = characters

        async with self._client() as client:
            r = await client.get(
                f"{self.base_url}/ainvr/api/lpr/plates",
                headers=self.headers,
                params=params,
            )
            r.raise_for_status()
            return r.json()

    async def get_lpr_records_in_range(
        self, start: datetime, end: datetime
    ) -> list[LPRRecord]:
        data = await self._get_lpr_page(start=start, end=end, page=0)
        return [LPRRecord(**r) for r in data.get("content", [])]

    async def get_plate_history_count(self, characters: str) -> int:
        now = datetime.now()
        start = now - timedelta(hours=self.job.lookback_hours)
        data = await self._get_lpr_page(start=start, end=now, characters=characters)
        return data.get("totalElements", 0)

    async def search_lpr_history(self, characters: str) -> list[dict]:
        now = datetime.now()
        start = now - timedelta(hours=self.job.lookback_hours)
        data = await self._get_lpr_page(start=start, end=now, characters=characters)
        
        records = []
        for item in data.get("content", []):
            try:
                records.append({
                    "licensePlateId": item.get("licensePlateId"),
                    "characters": item.get("characters"),
                    "confidence": item.get("confidence", 0.0),
                    "datetime": item.get("datetime"),
                    "cameraId": item.get("cameraId", 0),
                    "file": item.get("file", ""),
                    "sceneThumbnail": item.get("sceneThumbnail", ""),
                    "position": f"{item.get('x',0)},{item.get('y',0)},{item.get('w',0)},{item.get('h',0)}",
                    "triggered": False,
                    "event_created": False,
                    "history_count": data.get("totalElements", 0),
                })
            except Exception as e:
                logger.warning(f"Failed to parse LPR search result item: {e}")
        return records

    # ------------------------------------------------------------------ #
    #  LPR: create abnormal event
    # ------------------------------------------------------------------ #
    async def create_lpr_abnormal_event(self, record: LPRRecord) -> bool:
        dt_str = record.datetime.isoformat()
        scene_payload = {
            "cameraId": record.cameraId,
            "sceneObjects": [
                {
                    "objectType": record.type or "car",
                    "x": record.x,
                    "y": record.y,
                    "w": record.w,
                    "h": record.h,
                    "confidence": record.confidence,
                }
            ],
            "hashtags": ["abnormal", "repeat_lpr"],
            "datetime": dt_str,
        }
        logger.info(f"[LPR:{record.characters}] Sending scene payload: {json.dumps(scene_payload)}")

        img_url = None
        if record.sceneThumbnail:
            img_url = record.sceneThumbnail.replace("_thumbnail.jpg", ".jpg")
        img_bytes = await self._download_image(img_url)
        if img_bytes:
            is_jpeg = img_bytes[:2] == b'\xff\xd8'
            logger.info(f"[LPR:{record.characters}] Image: {len(img_bytes)} bytes, is_jpeg={is_jpeg}, first4={img_bytes[:4].hex()}")
        else:
            logger.warning(f"[LPR:{record.characters}] Image download failed — submitting without image")

        return await self._post_scene(scene_payload, img_bytes, record.file, f"LPR:{record.characters}")

    # ------------------------------------------------------------------ #
    #  FR: fetch all face category names from /ainvr/api/face/categories
    # ------------------------------------------------------------------ #
    async def get_face_category_names(self) -> list[str]:
        async with self._client(timeout=10) as client:
            r = await client.get(
                f"{self.base_url}/ainvr/api/face/categories",
                headers=self.headers,
            )
            r.raise_for_status()
            data = r.json()
        return [item["name"] for item in data if item.get("name")]

    # ------------------------------------------------------------------ #
    #  Shared: fetch all cameras → {cameraId: name} dict
    # ------------------------------------------------------------------ #
    async def get_cameras(self) -> dict[int, str]:
        """Return a mapping of cameraId (int) → camera name (str)."""
        result: dict[int, str] = {}
        page = 0
        while True:
            async with self._client(timeout=15) as client:
                r = await client.get(
                    f"{self.base_url}/ainvr/api/cameras",
                    headers=self.headers,
                    params={"page": page, "size": 200},
                )
                r.raise_for_status()
                data = r.json()
            for cam in data.get("content", []):
                cam_id = cam.get("cameraId")
                cam_name = cam.get("name", "")
                if cam_id is not None:
                    result[int(cam_id)] = cam_name
            if data.get("last", True):
                break
            page += 1
        logger.info(f"Fetched {len(result)} cameras from Vaidio")
        return result

    async def get_cameras_detailed(self) -> list[dict]:
        """Return a list of all cameras with full details (plugins, engineModels etc)."""
        result: list[dict] = []
        page = 0
        while True:
            async with self._client(timeout=15) as client:
                r = await client.get(
                    f"{self.base_url}/ainvr/api/cameras",
                    headers=self.headers,
                    params={"page": page, "size": 200},
                )
                r.raise_for_status()
                data = r.json()
            for cam in data.get("content", []):
                result.append(cam)
            if data.get("last", True):
                break
            page += 1
        logger.info(f"Fetched {len(result)} detailed cameras from Vaidio")
        return result

    async def get_cameras_with_status(self) -> list[dict]:
        """Fetch all cameras, querying activated and deactivated separately to determine status."""
        result: list[dict] = []
        for is_active in [True, False]:
            page = 0
            while True:
                async with self._client(timeout=15) as client:
                    r = await client.get(
                        f"{self.base_url}/ainvr/api/cameras",
                        headers=self.headers,
                        params={"page": page, "size": 200, "isActivate": str(is_active).lower()},
                    )
                    r.raise_for_status()
                    data = r.json()
                for cam in data.get("content", []):
                    cam["is_activate"] = is_active
                    result.append(cam)
                if data.get("last", True):
                    break
                page += 1
        logger.info(f"Fetched {len(result)} cameras with status from Vaidio")
        return result

    # ------------------------------------------------------------------ #
    #  FR: raw GET to /ainvr/api/face/matches
    #  Always includes "-" (strangers) and all known categories, scores=0
    # ------------------------------------------------------------------ #
    async def get_fr_records_in_range(
        self, start: datetime, end: datetime
    ) -> list[FRRecord]:
        try:
            category_names = await self.get_face_category_names()
        except Exception as e:
            logger.warning(f"Failed to fetch face categories: {e}, using '-' only")
            category_names = []
        categories_param = ",".join(["-"] + category_names)

        params = {
            "start":      start.strftime("%Y-%m-%d %H:%M:%S"),
            "end":        end.strftime("%Y-%m-%d %H:%M:%S"),
            "page":       0,
            "size":       100,
            "categories": categories_param,
            "scores":     "0",
        }
        if self.fr.camera_ids:
            params["cameraIds"] = ",".join(map(str, self.fr.camera_ids))
        else:
            params["allCameras"] = "true"

        records = []
        while True:
            async with self._client() as client:
                r = await client.get(
                    f"{self.base_url}/ainvr/api/face/matches",
                    headers=self.headers,
                    params=params,
                )
                r.raise_for_status()
                data = r.json()

            for item in data.get("content", []):
                try:
                    fk = item.get("faceKey") or {}
                    ft = item.get("faceTarget") or {}
                    records.append(FRRecord(
                        faceMatchId=item["faceMatchId"],
                        datetime=item["datetime"],
                        cameraId=fk.get("cameraId", 0),
                        sceneId=fk.get("sceneId", 0),
                        faceTargetId=ft.get("faceTargetId", "unknown"),
                        faceTargetName=ft.get("name", "unknown"),
                        file=fk.get("file", ""),
                        position=fk.get("position", "0,0,0,0"),
                        confidence=fk.get("confidence", 0.0),
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse FR record {item.get('faceMatchId')}: {e}")

            if data.get("last", True):
                break
            params["page"] += 1

        return records

    # ------------------------------------------------------------------ #
    #  FR: get face descriptor from file URL
    # ------------------------------------------------------------------ #
    async def get_face_descriptor(self, file_url: str) -> str | None:
        async with self._client(timeout=20) as client:
            r = await client.post(
                f"{self.base_url}/ainvr/api/face",
                headers=self.headers,
                files={"url": (None, file_url)},
            )
            r.raise_for_status()
            result = r.json()

        if not result or not isinstance(result, list):
            logger.warning(f"No face descriptor returned for {file_url}")
            return None

        descriptor = result[0].get("descriptor")
        if not descriptor:
            logger.warning(f"Empty descriptor for {file_url}")
            return None

        return descriptor

    # ------------------------------------------------------------------ #
    #  FR: search face history count using descriptor
    # ------------------------------------------------------------------ #
    async def search_face_count(self, descriptor: str) -> int:
        now = datetime.now()
        start = now - timedelta(hours=self.fr.lookback_hours)
        data = {
            "start":      start.strftime("%Y-%m-%d %H:%M:%S"),
            "end":        now.strftime("%Y-%m-%d %H:%M:%S"),
            "descriptor": descriptor,
            "scores":     "0.7",
        }
        if self.fr.camera_ids:
            data["cameraIds"] = ",".join(map(str, self.fr.camera_ids))
        else:
            data["allCameras"] = "true"
        async with self._client(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/ainvr/api/face/search",
                headers=self.headers,
                data=data,
            )
            r.raise_for_status()
            result = r.json()

        if not isinstance(result, list):
            return 0
        return len(result)

    # ------------------------------------------------------------------ #
    #  FR: search face history records using descriptor
    # ------------------------------------------------------------------ #
    async def search_face_history(self, descriptor: str, anchor_dt=None, lookback_hours: int = None) -> list[dict]:
        # When anchor_dt is provided (user clicked a specific log row), the window is
        # anchor_dt - lookback_hours → anchor_dt  — exactly the same window that was
        # used by the monitor when it computed history_count for that detection.
        # This means clicking a row with history_count=1 will return exactly 1 result.
        # Falls back to now - lookback_hours → now when anchor_dt is None (monitoring path).
        if lookback_hours is None:
            lookback_hours = self.fr.lookback_hours
        if anchor_dt is not None:
            # Convert to local time (naive) — Vaidio expects local server time in query strings,
            # matching datetime.now() used in the fallback path and in search_face_count().
            from datetime import timezone as _tz
            if hasattr(anchor_dt, 'tzinfo') and anchor_dt.tzinfo is not None:
                # Convert UTC-aware → local naive (astimezone uses the OS local TZ, i.e. SGT)
                anchor_dt = anchor_dt.astimezone().replace(tzinfo=None)
            end = anchor_dt
            start = anchor_dt - timedelta(hours=lookback_hours)
        else:
            end = datetime.now()
            start = end - timedelta(hours=lookback_hours)
        data = {
            "start":      start.strftime("%Y-%m-%d %H:%M:%S"),
            "end":        end.strftime("%Y-%m-%d %H:%M:%S"),
            "descriptor": descriptor,
            "scores":     "0.7",
        }
        if self.fr.camera_ids:
            data["cameraIds"] = ",".join(map(str, self.fr.camera_ids))
        else:
            data["allCameras"] = "true"
        async with self._client(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/ainvr/api/face/search",
                headers=self.headers,
                data=data,
            )
            r.raise_for_status()
            result = r.json()

        if not isinstance(result, list):
            return []

        records = []
        for item in result:
            try:
                # /ainvr/api/face/search returns a FLAT structure (not nested faceKey/faceTarget)
                # Fields: faceKeyId, cameraId, sceneId, datetime, file, position, confidence, ...
                face_file = item.get("file", "")
                # Derive scene thumbnail from face crop URL (e.g. _face0.jpg → _thumbnail.jpg)
                scene_thumbnail = re.sub(r'_face(?:\d+|_\d+_crop)\.jpg$', '_thumbnail.jpg', face_file) if face_file else ""
                records.append({
                    "faceMatchId": item.get("faceKeyId") or item.get("faceMatchId"),
                    "datetime": item.get("datetime"),
                    "cameraId": item.get("cameraId", 0),
                    "sceneId": item.get("sceneId", 0),
                    "faceTargetId": item.get("faceTargetId", "unknown"),
                    "faceTargetName": item.get("faceTargetName", "unknown"),
                    "file": face_file,
                    "sceneThumbnail": scene_thumbnail,
                    "position": item.get("position", "0,0,0,0"),
                    "confidence": item.get("confidence", 0.0),
                })
            except Exception as e:
                logger.warning(f"Failed to parse search result item: {e}")
        return records


    # ------------------------------------------------------------------ #
    #  FR: create abnormal event
    # ------------------------------------------------------------------ #
    async def create_fr_abnormal_event(self, record: FRRecord) -> bool:
        try:
            parts = record.position.split(",")
            x, y, w, h = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        except Exception:
            logger.error(f"[FR:{record.faceTargetName}] Failed to parse position: {record.position}")
            x, y, w, h = 0, 0, 100, 100

        dt_str = record.datetime.isoformat()
        scene_payload = {
            "cameraId": record.cameraId,
            "sceneObjects": [
                {
                    "objectType": "person",
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "confidence": record.confidence,
                }
            ],
            "hashtags": ["abnormal", "repeat_face"],
            "datetime": dt_str,
        }
        logger.info(f"[FR:{record.faceTargetName}] Sending scene payload: {json.dumps(scene_payload)}")

        img_url = re.sub(r'_face\d+\.jpg$', '.jpg', record.file)
        img_bytes = await self._download_image(img_url)
        if img_bytes:
            is_jpeg = img_bytes[:2] == b'\xff\xd8'
            logger.info(f"[FR:{record.faceTargetName}] Image: {len(img_bytes)} bytes, is_jpeg={is_jpeg}")
        else:
            logger.warning(f"[FR:{record.faceTargetName}] Image download failed — submitting without image")

        return await self._post_scene(scene_payload, img_bytes, img_url, f"FR:{record.faceTargetName}")

    # ------------------------------------------------------------------ #
    #  Shared: POST to /ainvr/api/scenes
    # ------------------------------------------------------------------ #
    async def _post_scene(
        self,
        scene_payload: dict,
        img_bytes: bytes | None,
        img_url: str | None,
        log_tag: str,
    ) -> bool:
        async with self._client(timeout=20) as client:
            scene_str = json.dumps(scene_payload)
            if img_bytes:
                filename = img_url.split("/")[-1] if img_url else "image.jpg"
                r = await client.post(
                    f"{self.base_url}/ainvr/api/scenes",
                    headers=self.headers,
                    data={"scene": scene_str},
                    files={"file": (filename, img_bytes, "image/jpeg")},
                )
            else:
                r = await client.post(
                    f"{self.base_url}/ainvr/api/scenes",
                    headers=self.headers,
                    data={"scene": scene_str},
                )
            logger.info(f"[{log_tag}] Scene API response: status={r.status_code} body={r.text}")
            r.raise_for_status()
            return True

    # ------------------------------------------------------------------ #
    #  Shared: download image bytes
    # ------------------------------------------------------------------ #
    async def _download_image(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            async with self._client(timeout=10) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.content
        except Exception as e:
            logger.warning(f"Image download failed {url}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Connection test
    # ------------------------------------------------------------------ #
    async def test_connection(self) -> bool:
        try:
            now = datetime.now()
            await self._get_lpr_page(start=now - timedelta(minutes=1), end=now, page=0)
            return True
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
