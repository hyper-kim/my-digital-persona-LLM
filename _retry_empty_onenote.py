"""CV 경로에서 EMPTY로 기록된 실패 항목을 failed_files에서 제거 → 파이프라인이 재처리하도록.

사용:
  python _retry_empty_onenote.py            # 원노트 EMPTY만 (기존 동작)
  python _retry_empty_onenote.py --all      # 전체 EMPTY 168개
  python _retry_empty_onenote.py --all --dry-run
"""
import sqlite3, sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DRY      = '--dry-run' in sys.argv
ALL_MODE = '--all'     in sys.argv
DB       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'processed_files.db')

conn = sqlite3.connect(DB, timeout=10)

if ALL_MODE:
    rows = conn.execute(
        "SELECT file_path FROM failed_files WHERE error LIKE '%EMPTY%'"
    ).fetchall()
    label = "전체 EMPTY"
    where = "error LIKE '%EMPTY%'"
else:
    rows = conn.execute(
        "SELECT file_path FROM failed_files "
        "WHERE file_path LIKE '%원노트%' AND error LIKE '%EMPTY%'"
    ).fetchall()
    label = "원노트 EMPTY"
    where = "file_path LIKE '%원노트%' AND error LIKE '%EMPTY%'"

print(f"재처리 대상 ({label}): {len(rows)}개 {'(dry-run)' if DRY else ''}")
for (fp,) in rows:
    print(f"  {fp[-80:]}")

if not DRY and rows:
    conn.execute(f"DELETE FROM failed_files WHERE {where}")
    conn.commit()
    print(f"\n삭제 완료 ({len(rows)}개) → 파이프라인이 다음 순회 시 자동 재처리합니다.")
elif DRY:
    print("\n(dry-run: 실제 삭제 없음)")

conn.close()

