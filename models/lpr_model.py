from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LPRRecord(BaseModel):
    licensePlateId: int
    cameraId: int
    sceneId: int
    datetime: datetime
    characters: str
    confidence: float
    x: int
    y: int
    w: int
    h: int
    file: Optional[str] = None
    sceneThumbnail: Optional[str] = None
    type: Optional[str] = None
    make: Optional[str] = None
    state: Optional[str] = None

class ProcessedRecord(BaseModel):
    licensePlateId: int
    characters: str
    confidence: float
    detected_at: datetime
    cameraId: int
    history_count: int
    triggered: bool
    event_created: bool = False
    error: Optional[str] = None
    file: Optional[str] = None
    scene_thumbnail: Optional[str] = None
    position: Optional[str] = "0,0,0,0"
