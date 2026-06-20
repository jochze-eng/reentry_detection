import asyncio
from datetime import datetime
from services.db import db_manager
from config import load_config
from services.vaidio_client import VaidioClient

async def main():
    cfg = await load_config()
    
    # Connect and get recent FR logs
    await db_manager.connect()
    logs = await db_manager.get_fr_logs(limit=10)
    print("\n--- Recent FR Logs in DB ---")
    for log in logs:
        print(f"ID: {log.get('faceMatchId')}, Name: {log.get('faceTargetName')}, Detected At: {log.get('detected_at') or log.get('datetime')}, History Count: {log.get('history_count')}")
        
        # Test fetching history for this record
        client = VaidioClient(cfg)
        face_file = log.get('face_file')
        face_target_id = log.get('faceTargetId')
        dt_str = log.get('detected_at') or log.get('datetime')
        
        if face_file and dt_str:
            anchor_dt = datetime.fromisoformat(dt_str)
            descriptor = await db_manager.get_fr_descriptor_by_file(face_file)
            if not descriptor:
                descriptor = await client.get_face_descriptor(face_file)
            if descriptor:
                records = await client.search_face_history(
                    descriptor,
                    anchor_dt=anchor_dt,
                    lookback_hours=cfg.fr.lookback_hours,
                )
                print(f"  Vaidio Search returned {len(records)} records for anchor_dt={anchor_dt}")
                for idx, r in enumerate(records):
                    print(f"    [{idx}] MatchId: {r.get('faceMatchId')}, datetime: {r.get('datetime')}, Name: {r.get('faceTargetName')}")
            else:
                print("  No descriptor found/extracted.")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
