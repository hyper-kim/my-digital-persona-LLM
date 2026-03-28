"""
watch_ui.py — Gradio UI 헬스체크 + 자동 재시작 감시자
=====================================================
- 30초마다 http://127.0.0.1:7860 GET 체크
- 응답 없으면 3_ui.py 재시작
- 별도 터미널에서: python watch_ui.py
"""

import subprocess
import sys
import os
import time
import urllib.request
import urllib.error
import platform
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_SCRIPT   = os.path.join(PROJECT_DIR, "3_ui.py")
UI_URL      = "http://127.0.0.1:7860"
CHECK_INTERVAL = 30   # 초
MAX_START_WAIT = 25   # UI 재기동 후 응답 대기
LOG_FILE       = os.path.join(PROJECT_DIR, "watch_ui.log")

# venv Python 우선
_VENV_PY = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PYTHON   = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

_ui_proc = None

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _is_ui_alive() -> bool:
    try:
        with urllib.request.urlopen(UI_URL, timeout=6) as r:
            return r.status == 200
    except Exception:
        return False

def _start_ui():
    global _ui_proc
    _log("UI 재시작 중...")
    if _ui_proc and _ui_proc.poll() is None:
        _ui_proc.terminate()
        time.sleep(1)
    out = open(os.path.join(PROJECT_DIR, "ui_out.log"), "a", encoding="utf-8")
    err = open(os.path.join(PROJECT_DIR, "ui_err2.log"), "a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    _ui_proc = subprocess.Popen(
        [PYTHON, "-X", "utf8", UI_SCRIPT],
        stdout=out, stderr=err,
        cwd=PROJECT_DIR,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
    )
    _log(f"UI 시작됨: PID {_ui_proc.pid}")

    # 응답 대기
    for _ in range(MAX_START_WAIT):
        time.sleep(1)
        if _is_ui_alive():
            _log(f"UI {UI_URL} 응답 확인 ✓")
            return
    _log(f"⚠️  UI 기동 후 {MAX_START_WAIT}초 내 응답 없음 — 다음 주기에 재시도")


if __name__ == "__main__":
    _log(f"=== UI Watchdog 시작 (체크 주기: {CHECK_INTERVAL}s) ===")

    # 처음에 UI가 없으면 기동
    if not _is_ui_alive():
        _start_ui()
    else:
        _log(f"UI {UI_URL} 이미 실행 중 — 모니터링 시작")

    while True:
        time.sleep(CHECK_INTERVAL)
        alive = _is_ui_alive()
        if alive:
            _log(f"UI 정상 ✓  ({UI_URL})")
        else:
            _log(f"⚠️  UI 응답 없음 → 재시작")
            _start_ui()
