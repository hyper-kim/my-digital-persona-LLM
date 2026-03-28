"""
run_watchdog.py ─ Omni-Brain 자동 재시작 감시자
=================================================
1_ingest_data.py 가 비정상 종료할 경우 자동으로 재시작합니다.
SQLite processed 테이블 기준으로 미처리 파일이 0개가 되면 자동 종료합니다.

사용법:
  python run_watchdog.py "<입력_폴더_경로>"
  python run_watchdog.py                   # RAW_DATA_DIR 환경변수 사용
"""

import subprocess
import sqlite3
import sys
import os
import time
import signal
from datetime import datetime

# Windows cp949 환경에서 이모지/한글 깨짐 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ─── 환경 변수 (기본값은 GCP Ubuntu 기준) ────────────────────────────────────
PROJECT_DIR   = os.getenv("PROJECT_DIR",   os.path.dirname(os.path.abspath(__file__)))
STATE_DB_PATH = os.getenv("STATE_DB_PATH", os.path.join(PROJECT_DIR, "processed_files.db"))
RAW_DATA_DIR  = os.getenv("RAW_DATA_DIR",  "")

INGEST_SCRIPT        = os.path.join(PROJECT_DIR, "1_ingest_data.py")

# venv Python 우선 사용
_VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
if not os.path.exists(_VENV_PYTHON):  # Ubuntu
    _VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python")
PYTHON_EXE = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable
RETRY_DELAY_SEC      = int(os.getenv("WATCHDOG_RETRY_DELAY",  "10"))   # 재시작 전 대기 초
STUCK_TIMEOUT_MIN    = int(os.getenv("WATCHDOG_STUCK_TIMEOUT", "30"))   # 진행 없으면 강제종료 기준 분
MAX_RETRIES          = int(os.getenv("WATCHDOG_MAX_RETRIES",   "999"))  # 최대 재시도 횟수

# ─── 파일 카운팅 ─────────────────────────────────────────────────────────────
SUPPORTED_EXTS = {
    'txt', 'md', 'csv', 'py', 'json', 'html', 'xml', 'log', 'ini',
    'docx', 'pptx', 'xlsx', 'pdf', 'jpg', 'jpeg', 'png', 'bmp',
    'webp', 'tiff', 'm4a', 'mp3', 'mp4', 'wav', 'flac', 'avi', 'mkv',
    'hwp', 'hwpx',
}

def count_all_files(input_root: str) -> int:
    """입력 폴더 아래의 모든 대상 파일 수를 반환합니다."""
    total = 0
    for dirpath, _, files in os.walk(input_root):
        for fname in files:
            ext = fname.lower().rsplit('.', 1)[-1]
            if ext in SUPPORTED_EXTS:
                total += 1
            else:
                total += 1  # task_any_fallback 대상도 포함
    return total

def count_processed(state_db: str) -> int:
    """SQLite processed 테이블에 완료된 파일 수를 반환합니다."""
    if not os.path.exists(state_db):
        return 0
    try:
        with sqlite3.connect(state_db, timeout=10) as conn:
            row = conn.execute("SELECT COUNT(*) FROM processed").fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

def count_failed(state_db: str) -> int:
    """SQLite failed_files 테이블에 기록된 실패 파일 수를 반환합니다."""
    if not os.path.exists(state_db):
        return 0
    try:
        with sqlite3.connect(state_db, timeout=10) as conn:
            row = conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

def show_failed_summary(state_db: str):
    """실패 파일 목록의 요약을 출력합니다 (최대 20건)."""
    if not os.path.exists(state_db):
        return
    try:
        with sqlite3.connect(state_db, timeout=10) as conn:
            rows = conn.execute(
                "SELECT file_path, error, attempt_count FROM failed_files ORDER BY attempt_count DESC LIMIT 20"
            ).fetchall()
        if rows:
            print("\n──── 실패 파일 목록 (상위 20건) ────")
            for fp, err, cnt in rows:
                short = os.path.basename(fp)
                print(f"  [{cnt}회] {short} | {str(err)[:120]}")
    except Exception as e:
        print(f"  (실패 목록 조회 오류: {e})")

# ─── 메인 감시 루프 ─────────────────────────────────────────────────────────
def main():
    # 입력 경로 결정
    if len(sys.argv) >= 2:
        input_arg = sys.argv[1]
    elif RAW_DATA_DIR:
        input_arg = RAW_DATA_DIR
    else:
        print("❌ 입력 경로를 지정해 주세요.")
        print("   사용법: python run_watchdog.py <입력_폴더>")
        print("   또는 RAW_DATA_DIR 환경변수를 설정하세요.")
        sys.exit(1)

    input_arg = os.path.normpath(input_arg)
    if not os.path.isdir(input_arg):
        print(f"❌ 유효하지 않은 디렉터리: {input_arg}")
        sys.exit(1)

    print("=" * 60)
    print(f"  Omni-Brain 감시자 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  입력 경로  : {input_arg}")
    print(f"  상태 DB    : {STATE_DB_PATH}")
    print(f"  재시작 대기: {RETRY_DELAY_SEC}s | 최대 시도: {MAX_RETRIES}회")
    print("=" * 60)

    prev_processed   = -1
    consecutive_stuck = 0
    attempt          = 0
    current_proc     = None

    # Ctrl+C → 현재 자식 프로세스도 종료
    def _sigint(sig, frame):
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
        print("\n감시자가 사용자 중단으로 종료되었습니다.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    while attempt < MAX_RETRIES:
        attempt += 1
        start_ts = datetime.now().strftime('%H:%M:%S')
        print(f"\n[시도 #{attempt}] {start_ts} | python 1_ingest_data.py 시작...")

        cmd = [PYTHON_EXE, "-X", "utf8", INGEST_SCRIPT, input_arg]
        current_proc = subprocess.Popen(cmd)
        returncode = current_proc.wait()
        current_proc = None

        processed_now = count_processed(STATE_DB_PATH)
        total_files   = count_all_files(input_arg)
        pending       = max(0, total_files - processed_now)

        print(f"[시도 #{attempt}] 종료 코드={returncode} | "
              f"처리완료={processed_now}/{total_files} | 미처리={pending}")

        # ── 정상 종료 ──
        if returncode == 0 or pending == 0:
            print("\n✅ 모든 파일 처리 완료!")
            break

        # ── 진행이 없으면 stuck 카운트 증가 ──
        if processed_now == prev_processed:
            consecutive_stuck += 1
            print(f"⚠️  진행 없음 ({consecutive_stuck}/{STUCK_TIMEOUT_MIN // RETRY_DELAY_SEC}회)")
            # N분간 진행이 없으면 중단 (무한루프 방지)
            max_stuck = max(1, STUCK_TIMEOUT_MIN * 60 // RETRY_DELAY_SEC)
            if consecutive_stuck >= max_stuck:
                print(f"\n🛑 {STUCK_TIMEOUT_MIN}분 동안 진행이 없어 감시자를 종료합니다.")
                print("   수동으로 오류를 확인한 뒤 다시 실행해 주세요.")
                break
        else:
            consecutive_stuck = 0

        prev_processed = processed_now

        print(f"   {RETRY_DELAY_SEC}초 후 재시작합니다... (남은 파일: {pending}개)")
        time.sleep(RETRY_DELAY_SEC)

    else:
        print(f"\n🛑 최대 재시도 횟수({MAX_RETRIES}회)에 도달했습니다.")

    # ── 최종 요약 ──
    final_processed = count_processed(STATE_DB_PATH)
    final_failed    = count_failed(STATE_DB_PATH)
    total_files     = count_all_files(input_arg)

    print("\n" + "=" * 60)
    print(f"  완료 시각   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  전체 파일   : {total_files:,}")
    print(f"  임베딩 완료 : {final_processed:,}")
    print(f"  실패/건너뜀 : {final_failed:,}")
    print(f"  미처리 잔량 : {max(0, total_files - final_processed):,}")
    print("=" * 60)

    show_failed_summary(STATE_DB_PATH)

    print("\n🛑 GCP VM 중지를 잊지 마세요!")
    print("   gcloud compute instances stop <VM_이름> --zone <ZONE>")


if __name__ == "__main__":
    main()
