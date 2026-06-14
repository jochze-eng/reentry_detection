import logging
from models.config_model import AppConfig
from services.db import db_manager

logger = logging.getLogger(__name__)

async def load_config() -> AppConfig | None:
    try:
        return await db_manager.load_config()
    except Exception as e:
        logger.error(f"Failed to load config from database: {e}")
        return None

async def save_config(cfg: AppConfig) -> None:
    try:
        await db_manager.save_config(cfg)
        logger.info("Config saved to database.")
    except Exception as e:
        logger.error(f"Failed to save config to database: {e}")
