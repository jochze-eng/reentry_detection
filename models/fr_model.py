from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class FRRecord(BaseModel):
    faceMatchId: int
    datetime: datetime
    cameraId: int
    sceneId: int
    faceTargetId: str
    faceTargetName: str
    file: str           # faceKey.file — used to get descriptor and as log display
    position: str       # raw string "x,y,w,h"
    confidence: float

class FRProcessedRecord(BaseModel):
    faceMatchId: int
    faceTargetId: str
    faceTargetName: str
    face_file: str      # original faceKey.file URL for display
    detected_at: datetime
    cameraId: int
    history_count: int
    triggered: bool
    event_created: bool = False
    error: Optional[str] = None
    position: str = "0,0,0,0"   # bounding box "x,y,w,h" from faceKey.position
    confidence: float = 0.0      # match confidence from faceKey.confidence
