"""
2_reprocess_pending.py — PENDING_OCR 파일 재처리 스크립트

현재 파이프라인 완료 후 실행:
  python -X utf8 2_reprocess_pending.py

동작:
  1. Qdrant에서 PENDING_OCR 텍스트가 포함된 포인트를 스캔
  2. 해당 file_path를 추출 (payload._node_content JSON 파싱)
  3. SQLite processed 테이블에서 해당 파일 삭제 (재처리 허용)
  4. Qdrant에서 해당 포인트 삭제
  5. sequential_run.py 재실행 → GCP SSH OCR로 실제 텍스트 추출
"""
import sys, os, re, json, sqlite3, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_DB_PATH = os.path.join(PROJECT_DIR, "processed_files.db")
QDRANT_PATH   = os.path.join(PROJECT_DIR, "qdrant_db")
COLLECTION    = "omni_persona_v3"
BATCH         = 500   # 한 번에 스크롤할 포인트 수

_VENV_PYTHON  = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PYTHON_EXE    = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable

# ── .env 로드 ──────────────────────────────────────────────────────────────────
_env = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env):
    with open(_env, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()

QDRANT_URL    = os.getenv("QDRANT_URL", "")

# ── 진행 상황 확인 ──────────────────────────────────────────────────────────────
def current_pending_count():
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchText
        c = QdrantClient(url=QDRANT_URL) if QDRANT_URL else QdrantClient(path=QDRANT_PATH)
        cnt = c.count(
            collection_name=COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="text", match=MatchText(text="PENDING_OCR"))]),
            exact=False,
        )
        return cnt.count
    except Exception as e:
        print(f"  ⚠️  Qdrant count 조회 실패: {e}")
        return -1

# ── PENDING_OCR 파일 경로 수집 & 삭제 ──────────────────────────────────────────
def collect_and_delete_pending():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchText

    client = QdrantClient(url=QDRANT_URL) if QDRANT_URL else QdrantClient(path=QDRANT_PATH)
    pending_file_paths = set()
    point_ids_to_delete = []
    offset = None
    scanned = 0

    print("📦 Qdrant 스캔 중 (PENDING_OCR 포인트 추출)...")
    while True:
        pts, next_offset = client.scroll(
            collection_name=COLLECTION,
            limit=BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not pts:
            break

        scanned += len(pts)
        for pt in pts:
            payload = pt.payload or {}
            # LlamaIndex는 텍스트를 _node_content(JSON) 또는 text 필드에 저장
            text = ""
            if "_node_content" in payload:
                try:
                    nc = json.loads(payload["_node_content"])
                    text = nc.get("text", "")
                except Exception:
                    text = str(payload.get("_node_content", ""))
            elif "text" in payload:
                text = payload["text"]

            if "PENDING_OCR" in text:
                point_ids_to_delete.append(pt.id)
                # file_path 추출 (stub 텍스트에서 파싱)
                m = re.search(r"file_path=(.+)", text)
                if m:
                    fp = m.group(1).strip().split("\n")[0].strip()
                    pending_file_paths.add(fp)

        if scanned % 5000 == 0:
            print(f"  스캔: {scanned:,}개 포인트 | PENDING 발견: {len(point_ids_to_delete):,}")

        if next_offset is None:
            break
        offset = next_offset

    print(f"\n  총 스캔: {scanned:,}개 포인트")
    print(f"  PENDING_OCR 포인트: {len(point_ids_to_delete):,}개")
    print(f"  PENDING_OCR 파일: {len(pending_file_paths):,}개")

    if not point_ids_to_delete:
        print("  ✅ 재처리할 PENDING_OCR 없음")
        return []

    # Qdrant에서 삭제 (배치)
    print("\n🗑️  Qdrant에서 PENDING 포인트 삭제 중...")
    from qdrant_client.models import PointIdsList
    chunk_size = 200
    for i in range(0, len(point_ids_to_delete), chunk_size):
        chunk = point_ids_to_delete[i:i+chunk_size]
        client.delete(
            collection_name=COLLECTION,
            points_selector=PointIdsList(points=chunk),
        )
    print(f"  ✅ {len(point_ids_to_delete):,}개 포인트 삭제 완료")

    # SQLite에서 삭제 (재처리 허용)
    print("\n🗑️  SQLite에서 해당 파일 항목 삭제 중...")
    conn = sqlite3.connect(STATE_DB_PATH, timeout=10)
    removed = 0
    for fp in pending_file_paths:
        cur = conn.execute("DELETE FROM processed WHERE file_path = ?", (fp,))
        removed += cur.rowcount
    conn.commit()
    conn.close()
    print(f"  ✅ {removed:,}개 항목 삭제 완료 (재처리 대상)")

    return list(pending_file_paths)


# ── 메인 ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  PENDING_OCR 재처리 스크립트")
    print("=" * 60)

    # 現재 파이프라인 실행 중이면 경고
    import psutil
    pipeline_running = any(
        "1_ingest_data" in " ".join(p.cmdline())
        for p in psutil.process_iter(["cmdline"])
        if p.cmdline()
    )
    if pipeline_running:
        print("\n⚠️  경고: 1_ingest_data.py 파이프라인이 실행 중입니다!")
        ans = input("  계속 진행하시겠습니까? (yes/no): ").strip().lower()
        if ans != "yes":
            print("  취소됨.")
            sys.exit(0)

    # PENDING 포인트 수집 및 삭제
    pending_files = collect_and_delete_pending()

    if not pending_files:
        print("\n✅ 모든 파일이 정상 처리됨. 재실행 불필요.")
        sys.exit(0)

    print(f"\n🚀 재처리 대상: {len(pending_files):,}개 파일")
    print("   sequential_run.py로 재처리 시작...")
    print("   (GCP SSH OCR 활성화 상태에서 실행됩니다)\n")

    proc = subprocess.run(
        [PYTHON_EXE, "-X", "utf8", "sequential_run.py"],
        cwd=PROJECT_DIR,
    )
    print(f"\n완료 (exit={proc.returncode})")
