import asyncio
import httpx
from services.db import db_manager

async def test_endpoint():
    await db_manager.connect()
    
    # Get the latest FR log face_match_id and face_file
    async with db_manager.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT face_match_id, face_file, face_target_id, detected_at
            FROM fr_logs
            ORDER BY detected_at DESC
            LIMIT 1
        """)
    
    if not row:
        print("No FR logs in DB to test.")
        return
        
    face_match_id = row["face_match_id"]
    face_file = row["face_file"]
    face_target_id = row["face_target_id"]
    detected_at = row["detected_at"].isoformat()
    
    print(f"Testing with face_match_id={face_match_id}")
    
    # Clear cache for this face_match_id first to ensure a cache miss
    async with db_manager.pool.acquire() as conn:
        await conn.execute("DELETE FROM fr_history_cache WHERE parent_face_match_id = $1", face_match_id)
    print("Cleared any existing cache for this record.")
    
    url = "http://localhost:8088/api/fr/target/history"
    params = {
        "face_target_id": face_target_id,
        "face_file": face_file,
        "detected_at": detected_at,
        "face_match_id": face_match_id
    }
    
    # Query 1 (Cache Miss)
    async with httpx.AsyncClient(timeout=30) as client:
        print("\n--- Request 1 (Expect Cache Miss) ---")
        t0 = asyncio.get_event_loop().time()
        r1 = await client.get(url, params=params)
        duration1 = asyncio.get_event_loop().time() - t0
        print(f"Status: {r1.status_code}, Duration: {duration1:.3f}s")
        print(f"Returned {len(r1.json())} records.")
        
    # Query 2 (Expect Cache Hit)
    async with httpx.AsyncClient(timeout=30) as client:
        print("\n--- Request 2 (Expect Cache Hit) ---")
        t0 = asyncio.get_event_loop().time()
        r2 = await client.get(url, params=params)
        duration2 = asyncio.get_event_loop().time() - t0
        print(f"Status: {r2.status_code}, Duration: {duration2:.3f}s")
        print(f"Returned {len(r2.json())} records.")
        
    # Verify records in db table
    async with db_manager.pool.acquire() as conn:
        rows = await conn.fetch("SELECT count(*) FROM fr_history_cache WHERE parent_face_match_id = $1", face_match_id)
        print(f"\nCache Table Count in DB: {rows[0]['count']}")

if __name__ == "__main__":
    asyncio.run(test_endpoint())
