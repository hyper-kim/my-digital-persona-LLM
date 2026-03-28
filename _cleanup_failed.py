"""
_cleanup_failed.py
====================
파이프라인 완료 후 실행:
1. Qdrant에서 ❌ EMPTY / PENDING_OCR 벡터 제거
2. DB에서 해당 파일 경로 제거 → 다음 실행 시 재처리
3. 동일 basename이 여러 경로에 있는 중복 DB 항목 리포트 (실제 파일 크기 비교 후 smaller 제거)

사용법:
  venv\Scripts\python.exe -X utf8 _cleanup_failed.py
  venv\Scripts\python.exe -X utf8 _cleanup_failed.py --dry-run   # 삭제 없이 목록만 출력
"""
import sys, os, sqlite3, logging, argparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cleanup")

DRY_RUN = "--dry-run" in sys.argv

PROJ = os.getenv("PROJECT_DIR", os.path.dirname(os.path.abspath(__file__)))
DB   = os.getenv("STATE_DB_PATH",   os.path.join(PROJ, "processed_files.db"))
QDRANT_PATH = os.getenv("QDRANT_PATH", os.path.join(PROJ, "qdrant_db"))
_env_path = os.path.join(PROJ, ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8", errors="ignore") as _f:
        for _l in _f:
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _, _v = _l.partition("=")
                os.environ[_k.strip()] = _v.strip()
QDRANT_URL  = os.getenv("QDRANT_URL", "")
COLL = "omni_persona_v3"

# ──────────────────────────────────────────────
# 1. Qdrant PENDING_OCR / EMPTY 벡터 조회 & 제거
# ──────────────────────────────────────────────
def cleanup_qdrant():
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchText
    except ImportError:
        log.error("qdrant_client 패키지 없음")
        return []

    log.info("Qdrant 연결 중...")
    client = QdrantClient(url=QDRANT_URL) if QDRANT_URL else QdrantClient(path=QDRANT_PATH)
    removed_paths = []

    markers = ["❌ EMPTY", "[VISUAL_CONTENT_PENDING_OCR]", "PENDING_OCR"]

    for marker in markers:
        offset = None
        batch_size = 200
        ids_to_delete = []
        paths_seen = set()

        while True:
            results, next_offset = client.scroll(
                collection_name=COLL,
                scroll_filter=Filter(must=[
                    FieldCondition(key="text", match=MatchText(text=marker))
                ]),
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not results:
                break
            for pt in results:
                ids_to_delete.append(pt.id)
                try:
                    nc = pt.payload.get("_node_content") or pt.payload.get("text", "")
                    import json
                    if isinstance(nc, str) and nc.startswith("{"):
                        nc_obj = json.loads(nc)
                        path = nc_obj.get("metadata", {}).get("file_path", "")
                    else:
                        path = pt.payload.get("metadata_", {}).get("file_path", "")
                    if path:
                        paths_seen.add(path)
                except Exception:
                    pass
            offset = next_offset
            if not next_offset:
                break

        if ids_to_delete:
            if not DRY_RUN:
                from qdrant_client.models import PointIdsList
                client.delete(
                    collection_name=COLL,
                    points_selector=PointIdsList(points=ids_to_delete),
                )
                log.info(f"[QDRANT] 삭제 완료 ({marker}): {len(ids_to_delete)}개 벡터")
            else:
                log.info(f"[DRY-RUN][QDRANT] 삭제 대상 ({marker}): {len(ids_to_delete)}개 벡터")
            removed_paths.extend(paths_seen)

    client.close()
    return list(set(removed_paths))


# ──────────────────────────────────────────────
# 2. DB에서 실패 파일 경로 제거
# ──────────────────────────────────────────────
def cleanup_db(failed_paths: list):
    if not failed_paths:
        log.info("[DB] 제거할 항목 없음")
        return
    conn = sqlite3.connect(DB)
    removed = 0
    for fp in failed_paths:
        exists = conn.execute("SELECT 1 FROM processed WHERE file_path=?", (fp,)).fetchone()
        if exists:
            if not DRY_RUN:
                conn.execute("DELETE FROM processed WHERE file_path=?", (fp,))
                removed += 1
            else:
                log.info(f"[DRY-RUN][DB] 삭제 대상: {os.path.basename(fp)}")
                removed += 1
    if not DRY_RUN:
        conn.commit()
        log.info(f"[DB] {removed}개 파일 경로 제거 완료 (재처리 예약)")
    else:
        log.info(f"[DRY-RUN][DB] 삭제 대상 총 {removed}개")
    conn.close()


# ──────────────────────────────────────────────
# 3. 동일 basename 중복 파일 탐지
# ──────────────────────────────────────────────
def find_duplicates():
    conn = sqlite3.connect(DB)
    all_paths = [r[0] for r in conn.execute("SELECT file_path FROM processed").fetchall()]
    conn.close()

    from collections import defaultdict
    by_name = defaultdict(list)
    for fp in all_paths:
        by_name[os.path.basename(fp)].append(fp)

    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dupes:
        log.info("[DUP] 중복 파일 없음")
        return

    log.info(f"[DUP] 동일 이름 중복 파일 {len(dupes)}종 발견:")
    to_remove = []
    for name, paths in sorted(dupes.items()):
        sizes = []
        for fp in paths:
            try:
                sz = os.path.getsize(fp) if os.path.exists(fp) else -1
            except Exception:
                sz = -1
            sizes.append((sz, fp))
        sizes.sort(key=lambda x: -x[0])  # 큰 것 먼저
        log.info(f"  {name}:")
        for sz, fp in sizes:
            flag = ""
            if sz < max(s for s, _ in sizes) and max(s for s, _ in sizes) > 0:
                flag = " ← 소용량 → 삭제 대상"
                to_remove.append(fp)
            log.info(f"    {sz:>10} bytes  {fp}{flag}")

    if to_remove and not DRY_RUN:
        conn = sqlite3.connect(DB)
        for fp in to_remove:
            conn.execute("DELETE FROM processed WHERE file_path=?", (fp,))
        conn.commit()
        conn.close()
        log.info(f"[DUP] DB에서 {len(to_remove)}개 소용량 중복 항목 제거")
    elif to_remove:
        log.info(f"[DRY-RUN][DUP] 삭제 대상 {len(to_remove)}개 (실제 파일 삭제는 하지 않음)")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    print(f"{'[DRY-RUN MODE]' if DRY_RUN else '[LIVE MODE]'} 실패/중복 파일 정리 시작")
    print("Qdrant가 다른 프로세스에 의해 잠겨있으면 오류 발생합니다.")
    print("파이프라인이 완전히 종료된 후 실행하세요.\n")

    failed = cleanup_qdrant()
    log.info(f"Qdrant에서 {len(failed)}개 경로 수집")
    cleanup_db(failed)
    find_duplicates()
    print("\n정리 완료.")
