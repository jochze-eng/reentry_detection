from pydantic import BaseModel, Field

class VaidioConfig(BaseModel):
    base_url: str = Field(..., example="http://172.16.22.183")
    api_key: str = Field(..., example="fQjDajuwrmNE3Qu7QZwc8Gkr4GoqYPu0QH1nuKhL")

class JobConfig(BaseModel):
    enabled: bool = False
    poll_interval_seconds: int = Field(30, ge=5)
    page_size: int = Field(100, ge=1)
    lookback_hours: int = Field(24, ge=1)
    threshold: int = Field(10, ge=1)
    camera_ids: list[int] = Field(default_factory=list)

class FRConfig(BaseModel):
    enabled: bool = False
    poll_interval_seconds: int = Field(30, ge=5)
    lookback_hours: int = Field(24, ge=1)
    threshold: int = Field(3, ge=1)
    camera_ids: list[int] = Field(default_factory=list)

class AppConfig(BaseModel):
    vaidio: VaidioConfig
    job: JobConfig
    fr: FRConfig
    image_cache_hours: int = Field(72, ge=0)

