"""One-time backfill of fr_logs.person_cluster_id for existing FR deployments.

New detections get a cluster id assigned online by the FR monitor. Rows that
predate this feature have person_cluster_id = NULL; until reclustered they each
count as their own unique face (the count query falls back to face_match_id).
Run this once after upgrading to assign clusters to the existing rows so the
"unique face" count is accurate for historical data.

Reuses the same Vaidio face-search + clustering logic as the live monitor, so
results are consistent with the recurring/triggered logic.

Run from the repo root (or inside the app container):
    python tools/backfill_fr_clusters.py
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from services.vaidio_client import VaidioClient
from services.db import db_manager


async def main() -> None:
    cfg = await load_config()
    client = VaidioClient(cfg)
    await db_manager.connect()
    async with db_manager.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT face_match_id, detected_at, descriptor FROM fr_logs "
            "WHERE descriptor IS NOT NULL AND person_cluster_id IS NULL ORDER BY detected_at ASC")
    print(f"backfilling {len(rows)} rows with descriptors", flush=True)
    done = 0
    for r in rows:
        own, dt, desc = r["face_match_id"], r["detected_at"], r["descriptor"]
        try:
            recs = await client.search_face_history(desc, anchor_dt=dt, lookback_hours=cfg.fr.lookback_hours)
            matched = [x.get("faceMatchId") for x in recs]
        except Exception as e:
            print(f"  search failed for {own}: {e}", flush=True)
            matched = []
        cid = await db_manager.assign_fr_cluster(own, matched)
        async with db_manager.pool.acquire() as conn:
            await conn.execute("UPDATE fr_logs SET person_cluster_id=$1 WHERE face_match_id=$2", cid, own)
        done += 1
        if done % 50 == 0:
            print(f"  ...{done}/{len(rows)}", flush=True)
    # Rows without a descriptor: each is its own singleton unique.
    async with db_manager.pool.acquire() as conn:
        await conn.execute("UPDATE fr_logs SET person_cluster_id = face_match_id WHERE person_cluster_id IS NULL")
        clusters = await conn.fetchval("SELECT COUNT(DISTINCT person_cluster_id) FROM fr_logs")
        total = await conn.fetchval("SELECT COUNT(*) FROM fr_logs")
    print(f"DONE. total_rows={total} distinct_clusters={clusters}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
