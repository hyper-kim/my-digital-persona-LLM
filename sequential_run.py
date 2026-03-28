"""
sequential_run.py ─ 폴더 순차 처리 러너
=========================================
1) 한성과고 폴더 먼저 완전 처리
2) 완료 후 상위 '해외 대학 편입 정리' 전체 처리
   (한성과고 파일은 SQLite processed 테이블에 이미 있으므로 자동 스킵)

실행: python sequential_run.py
"""

import subprocess
import sys
import os
import sqlite3
import time
import atexit
from datetime import datetime

# Windows cp949 환경에서 이모지/한글 깨짐 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))

# .env 파일 로드 (python-dotenv 없이 직접 파싱)
_env_file = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8", errors="ignore") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()

STATE_DB_PATH = os.getenv("STATE_DB_PATH", os.path.join(PROJECT_DIR, "processed_files.db"))
WATCHDOG      = os.path.join(PROJECT_DIR, "run_watchdog.py")

# venv Python 우선 사용 (subprocess 자식도 같은 환경 유지)
_VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PYTHON_EXE   = _VENV_PYTHON if os.path.exists(_VENV_PYTHON) else sys.executable

# 처리 순서.
# 1) 한성과고 → 2) 해외 대학 편입 정리 → 3) Takeout
# 상위 폴더를 2번에 넣으면 1번에서 처리된 파일은 SQLite 기준으로 자동 스킵됨.
TAKEOUT_CANDIDATES = [
    r"G:\내 드라이브\Takeout",
    r"G:\Takeout",
    r"G:\내 드라이브\Google Takeout",
]


def resolve_takeout_path():
    for p in TAKEOUT_CANDIDATES:
        if os.path.isdir(p):
            return p
    # 기본값(없으면 run_folder에서 자동 건너뜀)
    return TAKEOUT_CANDIDATES[0]


FOLDERS = [
    r"G:\내 드라이브\해외 대학 편입 정리\한성과고",
    r"G:\내 드라이브\해외 대학 편입 정리",
    resolve_takeout_path(),
]
VM_MONITOR = os.path.join(PROJECT_DIR, "_vm_monitor.py")
_vm_mon_proc = None


def start_vm_monitor():
    """VM 모니터 백그라운드 시작 (GCP_VM_NAME 설정된 경우만)"""
    global _vm_mon_proc
    vm_name = os.getenv("GCP_VM_NAME", "")
    if not vm_name:
        print("  [VM-MON] GCP_VM_NAME 미설정 - VM 자동 관리 비활성")
        return
    if not os.path.exists(VM_MONITOR):
        return
    _vm_mon_proc = subprocess.Popen(
        [PYTHON_EXE, "-u", "-X", "utf8", VM_MONITOR],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    print(f"  [VM-MON] 시작 (PID {_vm_mon_proc.pid})")


def stop_vm_monitor():
    """VM 모니터 종료 (= VM 자동 중지 트리거)"""
    global _vm_mon_proc
    if _vm_mon_proc and _vm_mon_proc.poll() is None:
        print(f"  [VM-MON] 종료 신호 전송 (PID {_vm_mon_proc.pid})")
        _vm_mon_proc.terminate()
        try:
            _vm_mon_proc.wait(timeout=90)
        except Exception:
            _vm_mon_proc.kill()
    _vm_mon_proc = None

atexit.register(stop_vm_monitor)

def count_processed():
    if not os.path.exists(STATE_DB_PATH):
        return 0
    try:
        with sqlite3.connect(STATE_DB_PATH, timeout=5) as c:
            return c.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    except Exception:
        return 0


def run_folder(folder):
    print(f"\n{'='*60}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] 시작: {folder}")
    print(f"  처리 완료 누적: {count_processed():,}")
    print(f"{'='*60}")

    if not os.path.isdir(folder):
        print(f"  ⚠️  폴더 없음, 건너뜀: {folder}")
        return

    proc = subprocess.run([PYTHON_EXE, "-X", "utf8", WATCHDOG, folder])
    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] 완료: {folder} (exit={proc.returncode})")
    print(f"  처리 완료 누적: {count_processed():,}")


if __name__ == "__main__":
    start_vm_monitor()

    try:
        for folder in FOLDERS:
            run_folder(folder)
    finally:
        stop_vm_monitor()

    print("\n" + "="*60)
    print("  모든 폴더 처리 완료!")
    print(f"  총 임베딩: {count_processed():,} 파일")
    print("="*60)
