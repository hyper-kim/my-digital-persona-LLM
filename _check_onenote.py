import sqlite3, sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = sqlite3.connect('processed_files.db', timeout=5)

ok   = conn.execute("SELECT COUNT(*) FROM processed   WHERE file_path LIKE '%원노트%'").fetchone()[0]
fail = conn.execute("SELECT COUNT(*) FROM failed_files WHERE file_path LIKE '%원노트%'").fetchone()[0]
print(f"원노트 성공: {ok}개  |  실패: {fail}개")

if fail:
    rows = conn.execute("SELECT file_path, error FROM failed_files WHERE file_path LIKE '%원노트%' LIMIT 10").fetchall()
    for fp, err in rows:
        print(f"  FAIL: {fp[-65:]}")
        print(f"        {str(err)[:90]}")

print()
if ok:
    rows2 = conn.execute("SELECT file_path FROM processed WHERE file_path LIKE '%원노트%' LIMIT 5").fetchall()
    print("성공 샘플:")
    for (fp,) in rows2:
        print(f"  OK: {fp[-65:]}")

print()
total_ok   = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
total_fail = conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0]
hs_ok   = conn.execute("SELECT COUNT(*) FROM processed    WHERE file_path LIKE '%한성과고%'").fetchone()[0]
hs_fail = conn.execute("SELECT COUNT(*) FROM failed_files WHERE file_path LIKE '%한성과고%'").fetchone()[0]
print(f"전체   성공: {total_ok:,}  실패: {total_fail:,}")
print(f"한성과고 성공: {hs_ok:,}  실패: {hs_fail:,}")
conn.close()
