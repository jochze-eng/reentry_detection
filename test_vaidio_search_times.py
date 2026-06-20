import asyncio
import logging
from datetime import datetime, timedelta
from services.db import db_manager
from config import load_config
from services.vaidio_client import VaidioClient

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

async def main():
    cfg = await load_config()
    await db_manager.connect()
    
    # Get a recent record
    logs = await db_manager.get_fr_logs(limit=1)
    if not logs:
        print("No FR logs found.")
        return
        
    log = logs[0]
    face_file = log.get('face_file')
    print(f"Testing with file: {face_file}")
    
    client = VaidioClient(cfg)
    descriptor = await db_manager.get_fr_descriptor_by_file(face_file)
    if not descriptor:
        descriptor = await client.get_face_descriptor(face_file)
        
    if not descriptor:
        print("No descriptor found.")
        return
        
    # We will test search with different time windows:
    # 1. 24-hour window ending now
    end_now = datetime.now()
    start_24h_ago = end_now - timedelta(hours=24)
    print(f"\n--- Querying last 24 hours (naive SGT): {start_24h_ago} to {end_now} ---")
    res1 = await client.search_face_history(descriptor, anchor_dt=end_now)
    print(f"Results count: {len(res1)}")
    for r in res1[:5]:
         print(f"  MatchId: {r.get('faceMatchId')}, datetime: {r.get('datetime')}")
         
    # 2. Narrow window that should only contain older events
    # Let's say, 24 hours ago to 2 hours ago. This should EXCLUDE the current event (which happened recently).
    end_2h_ago = end_now - timedelta(hours=2)
    start_26h_ago = end_now - timedelta(hours=26)
    print(f"\n--- Querying window: {start_26h_ago} to {end_2h_ago} ---")
    
    # We call client.search_face_history directly with start and end strings to see what happens
    data = {
        "start":      start_26h_ago.strftime("%Y-%m-%d %H:%M:%S"),
        "end":        end_2h_ago.strftime("%Y-%m-%d %H:%M:%S"),
        "descriptor": descriptor,
        "scores":     "0.7",
        "allCameras": "true"
    }
    async with client._client(timeout=30) as http_client:
        r = await http_client.post(
            f"{client.base_url}/ainvr/api/face/search",
            headers=client.headers,
            data=data,
        )
        r.raise_for_status()
        result = r.json()
    print(f"Direct API call returned: {len(result) if isinstance(result, list) else result} results")
    if isinstance(result, list):
        for item in result[:5]:
            print(f"  MatchId: {item.get('faceKeyId')}, datetime: {item.get('datetime')}")

if __name__ == "__main__":
    asyncio.run(main())
