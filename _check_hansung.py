import sqlite3, sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DB = "file:processed_files.db?mode=ro"
conn = sqlite3.connect(DB, uri=True, timeout=5)

# 한성과고 성공
hs_ok  = conn.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%한성과고%'").fetchone()[0]
hs_fail = conn.execute("SELECT COUNT(*) FROM failed_files WHERE file_path LIKE '%한성과고%'").fetchone()[0]
print(f"[한성과고] 성공: {hs_ok:,}개  |  실패: {hs_fail:,}개")
if hs_fail:
    rows = conn.execute("SELECT file_path, error FROM failed_files WHERE file_path LIKE '%한성과고%' LIMIT 10").fetchall()
    for fp, err in rows:
        print(f"  FAIL: {fp[-55:]} | {str(err)[:60]}")

print()

# 전체
total_ok   = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
total_fail = conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0]
print(f"[전체] 성공: {total_ok:,}개  |  실패: {total_fail:,}개")

# 해외 대학 편입 정리 (한성과고 제외)
edit_ok  = conn.execute("SELECT COUNT(*) FROM processed WHERE file_path LIKE '%해외 대학 편입 정리%' AND file_path NOT LIKE '%한성과고%'").fetchone()[0]
edit_fail = conn.execute("SELECT COUNT(*) FROM failed_files WHERE file_path LIKE '%해외 대학 편입 정리%' AND file_path NOT LIKE '%한성과고%'").fetchone()[0]
print(f"[해외대학편입정리-한성과고 제외] 성공: {edit_ok:,}개  |  실패: {edit_fail:,}개")

conn.close()
sys.exit(0)

# 전체 포인트 수
info = client.get_collection(COLLECTION)
total_pts = info.points_count
print(f"Qdrant 전체 포인트: {total_pts:,}")

# 전략: 샘플링으로 빠르게 확인
# 1000개씩 3배치만 스캔해서 PENDING_OCR 비율 + 한성과고 분포 파악
hansung_pending = 0
hansung_total = 0
all_pending = 0

offset = None
SAMPLE_BATCHES = 6
BATCH = 1000

print(f"\n{SAMPLE_BATCHES * BATCH:,}개 샘플 스캔 중...")
for i in range(SAMPLE_BATCHES):
    kwargs = dict(collection_name=COLLECTION, limit=BATCH, with_payload=True, with_vectors=False)
    if offset is not None:
        kwargs['offset'] = offset
    pts, next_offset = client.scroll(**kwargs)
    if not pts:
        break
    for p in pts:
        nc  = str(p.payload.get('_node_content', '') or '')
        txt = str(p.payload.get('text', '') or '')
        combined = nc + txt
        fp = p.payload.get('file_path', '') or ''
        
        is_hansung = '한성과고' in combined or '한성과고' in fp
        is_pending = 'PENDING_OCR' in combined
        
        if is_hansung:
            hansung_total += 1
        if is_pending:
            all_pending += 1
        if is_hansung and is_pending:
            hansung_pending += 1
    
    if next_offset is None:
        break
    offset = next_offset

scanned = SAMPLE_BATCHES * BATCH
print(f"스캔: {scanned:,}개 중")
print(f"  한성과고 청크: {hansung_total}개")
print(f"  전체 PENDING_OCR: {all_pending}개")
print(f"  한성과고 PENDING_OCR: {hansung_pending}개")

# 전체 추정치
if scanned > 0:
    est_total_pending = int(all_pending / scanned * total_pts)
    print(f"\n전체 추정 PENDING_OCR: ~{est_total_pending:,}개 (비율 {all_pending/scanned*100:.1f}%)")
    if hansung_pending == 0:
        print("-> 샘플 내 한성과고 PENDING_OCR 없음 (양호)")
    else:
        print(f"-> 한성과고에 PENDING_OCR 존재 ({hansung_pending}개 확인)")


