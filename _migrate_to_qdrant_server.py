"""
_migrate_to_qdrant_server.py
=============================
로컬 SQLite Qdrant (path 모드) → Qdrant HTTP 서버 마이그레이션
- 23GB SQLite를 배치 단위로 읽어 RAM 폭발 없이 전송
- PENDING_OCR / EMPTY_CONTENT 등 비정상 포인트 제외
- 중단 후 재시작 지원 (이미 업로드된 포인트는 UPSERT로 덮어쓰기)

실행: venv\Scripts\python.exe -u _migrate_to_qdrant_server.py
"""

import sqlite3
import pickle
import time
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, CollectionInfo
)

# ── 설정 ─────────────────────────────────────────────────────────────
SQLITE_PATH   = r"qdrant_db\collection\omni_persona_v3\storage.sqlite"
COLLECTION    = "omni_persona_v3"
QDRANT_URL    = "http://localhost:6333"
VECTOR_SIZE   = 384
BATCH_SIZE    = 2000   # 한 번에 업로드할 포인트 수

# 비정상 마커 (이 문자열이 _node_content에 있으면 스킵)
BAD_MARKERS = [
    "PENDING_OCR",
    "[VISUAL_CONTENT_PENDING_OCR]",
    "EMPTY_CONTENT",
    "❌ ERROR",
    "❌ERROR",
]
# ─────────────────────────────────────────────────────────────────────


def is_bad_point(payload: dict) -> bool:
    nc = str(payload.get("_node_content", "") or "")
    for marker in BAD_MARKERS:
        if marker in nc:
            return True
    return False


def main():
    print(f"[{datetime.now():%H:%M:%S}] Qdrant 서버 연결 중: {QDRANT_URL}")
    server = QdrantClient(url=QDRANT_URL, timeout=60, check_compatibility=False)

    # 컬렉션 생성 (없으면)
    existing = [c.name for c in server.get_collections().collections]
    if COLLECTION not in existing:
        print(f"컬렉션 '{COLLECTION}' 생성 (dim={VECTOR_SIZE}, Cosine)")
        server.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    else:
        cnt = server.count(COLLECTION, exact=False).count
        print(f"컬렉션 기존 존재: 현재 벡터 수 ≈ {cnt:,}")

    # SQLite 연결 (읽기 전용 - 잠금 없음)
    print(f"SQLite 열기: {SQLITE_PATH}")
    _uri = f"file:{SQLITE_PATH}?mode=ro"
    db = sqlite3.connect(_uri, uri=True, check_same_thread=False)
    db.execute("PRAGMA cache_size=-524288")  # 512MB 캐시

    total_rows = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    print(f"총 SQLite rows: {total_rows:,}")

    batch = []
    uploaded = 0
    skipped_bad = 0
    errors = 0
    t0 = time.time()
    last_log = t0

    cursor = db.execute("SELECT id, point FROM points")

    for row_idx, (row_id, blob) in enumerate(cursor):
        try:
            obj = pickle.loads(blob)
        except Exception as e:
            errors += 1
            continue

        # vector 추출
        vec = obj.vector
        if isinstance(vec, dict):
            vec = vec.get("") or vec.get("default")
        if not vec or len(vec) != VECTOR_SIZE:
            errors += 1
            continue

        # payload 검사
        payload = obj.payload or {}
        if is_bad_point(payload):
            skipped_bad += 1
            continue

        batch.append(PointStruct(
            id=str(obj.id),
            vector=list(vec),
            payload=payload,
        ))

        if len(batch) >= BATCH_SIZE:
            server.upsert(collection_name=COLLECTION, points=batch)
            uploaded += len(batch)
            batch.clear()

            now = time.time()
            if now - last_log >= 30:
                elapsed = now - t0
                rate = uploaded / elapsed if elapsed > 0 else 0
                pct = (row_idx + 1) / total_rows * 100
                eta = (total_rows - row_idx - 1) / rate if rate > 0 else 0
                print(f"[{datetime.now():%H:%M:%S}] {row_idx+1:,}/{total_rows:,} ({pct:.1f}%)"
                      f" | 업로드 {uploaded:,} | 속도 {rate:.0f}/s | ETA {eta/60:.0f}분"
                      f" | 스킵 {skipped_bad} | 에러 {errors}", flush=True)
                last_log = now

    # 마지막 배치
    if batch:
        server.upsert(collection_name=COLLECTION, points=batch)
        uploaded += len(batch)

    db.close()

    elapsed = time.time() - t0
    final_cnt = server.count(COLLECTION, exact=True).count
    print(f"\n{'='*60}")
    print(f"마이그레이션 완료!")
    print(f"  소요 시간  : {elapsed/60:.1f}분")
    print(f"  업로드 완료: {uploaded:,}개")
    print(f"  스킵(비정상): {skipped_bad:,}개")
    print(f"  에러       : {errors:,}개")
    print(f"  서버 총 벡터: {final_cnt:,}개")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
