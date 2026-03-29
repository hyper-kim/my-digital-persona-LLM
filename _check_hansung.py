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

