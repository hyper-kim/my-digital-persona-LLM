"""
3_ui.py ─ Omni-Brain 파일 투입 & 모니터링 UI
=============================================
- 폴더/파일을 드래그 앤 드롭 또는 경로 입력으로 추가
- 로컬 CPU·GPU·RAM / Cloud GPU 자원 유휴 없이 풀가동
- SQLite 실시간 진행 상황 표시
- GCP Spot VM 상태 표시

실행: python 3_ui.py
브라우저: http://127.0.0.1:7860
"""

import gradio as gr
import sqlite3
import subprocess
import sys
import os
import json
import time
import threading
import psutil
import platform
from datetime import datetime

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
PROJECT_DIR   = os.getenv("PROJECT_DIR",   os.path.dirname(os.path.abspath(__file__)))
STATE_DB_PATH = os.getenv("STATE_DB_PATH", os.path.join(PROJECT_DIR, "processed_files.db"))
LOG_FILE      = os.getenv("LOG_FILE",      os.path.join(PROJECT_DIR, "ingestion_log.txt"))
WATCHDOG_SCRIPT = os.path.join(PROJECT_DIR, "run_watchdog.py")

# ─── 상태 관리 ────────────────────────────────────────────────────────────────
_proc_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_start_time: float | None = None
_extra_folders: list[str] = []   # UI에서 추가 투입된 폴더 목록


# ─── SQLite 헬퍼 ──────────────────────────────────────────────────────────────
def _db_query(sql: str, params=()):
    if not os.path.exists(STATE_DB_PATH):
        return []
    try:
        with sqlite3.connect(STATE_DB_PATH, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def get_counts():
    processed = _db_query("SELECT COUNT(*) as n FROM processed")
    failed    = _db_query("SELECT COUNT(*) as n FROM failed_files")
    n_proc  = processed[0]["n"] if processed else 0
    n_fail  = failed[0]["n"]    if failed    else 0
    return n_proc, n_fail


def get_failed_rows(limit=100):
    rows = _db_query(
        "SELECT file_path, error, attempt_count, last_attempt "
        "FROM failed_files ORDER BY last_attempt DESC LIMIT ?", (limit,)
    )
    return [
        [os.path.basename(r["file_path"]), r["attempt_count"],
         str(r["error"])[:120], r["last_attempt"]]
        for r in rows
    ] if rows else []


def tail_log(n=40):
    if not os.path.exists(LOG_FILE):
        return "(로그 없음)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:]) if lines else "(로그 비어 있음)"
    except Exception as e:
        return f"(로그 읽기 실패: {e})"


# ─── 시스템 자원 ──────────────────────────────────────────────────────────────
def _nvidia_smi_local() -> str:
    """로컬 nvidia-smi로 VRAM 사용량 조회 (torch.cuda보다 정확 — 전체 VRAM 표시)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            timeout=5, stderr=subprocess.DEVNULL
        ).decode().strip()
        lines = []
        for row in out.splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 5:
                name, mem_used, mem_total, util, temp = parts[:5]
                lines.append(
                    f"  🎮 {name}: VRAM {mem_used}/{mem_total} MiB  "
                    f"GPU {util}%  {temp}°C"
                )
        return "\n".join(lines) if lines else "  (nvidia-smi: GPU 없음)"
    except FileNotFoundError:
        return "  (nvidia-smi 미설치)"
    except Exception:
        return "  (nvidia-smi 오류)"


def _nvidia_smi_cloud() -> str:
    """클라우드 VM에 SSH로 nvidia-smi 조회. VM 꺼져 있으면 빠르게 실패."""
    vm_ip   = os.getenv("CLOUD_VM_IP",   "35.211.58.231")
    ssh_key = os.getenv("SSH_KEY_PATH",  r"C:\Users\kjy\.ssh\gcp_key_fixed")
    user    = os.getenv("CLOUD_VM_USER", "kjy")
    cmd = [
        "ssh", "-i", ssh_key,
        "-o", "ConnectTimeout=4",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        f"{user}@{vm_ip}",
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu "
        "--format=csv,noheader,nounits 2>/dev/null"
    ]
    try:
        out = subprocess.check_output(
            cmd, timeout=8, stderr=subprocess.DEVNULL
        ).decode().strip()
        if not out:
            return "  (모델 미로드 또는 GPU 없음)"
        lines = []
        for row in out.splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 5:
                name, mem_used, mem_total, util, temp = parts[:5]
                lines.append(
                    f"  ☁️ {name}: VRAM {mem_used}/{mem_total} MiB  "
                    f"GPU {util}%  {temp}°C"
                )
        return "\n".join(lines) if lines else "  (nvidia-smi 응답 없음)"
    except subprocess.TimeoutExpired:
        return "  🔴 VM 응답 없음 (중지됨 또는 선점)"
    except Exception:
        return "  🔴 SSH 연결 실패"


def system_stats() -> str:
    try:
        cpu  = psutil.cpu_percent(interval=0.3)
        ram  = psutil.virtual_memory()
        ram_used  = ram.used  / 1024**3
        ram_total = ram.total / 1024**3
        ram_pct   = ram.percent
        local_vram = _nvidia_smi_local()
        return (
            f"  🖥️  CPU: {cpu:.0f}%\n"
            f"  🧠 RAM: {ram_used:.1f}/{ram_total:.1f} GB ({ram_pct:.0f}%)\n"
            f"{local_vram}\n"
        )
    except Exception:
        return "  (자원 정보 불가)\n"


# ─── 프로세스 제어 ────────────────────────────────────────────────────────────
def is_running() -> bool:
    with _proc_lock:
        return _proc is not None and _proc.poll() is None


def start_ingestion(folder_path: str):
    global _proc, _start_time
    folder_path = folder_path.strip()
    if not folder_path or not os.path.isdir(folder_path):
        return f"❌ 유효하지 않은 폴더: {folder_path}"

    with _proc_lock:
        if _proc and _proc.poll() is None:
            return "⚠️ 이미 실행 중입니다."
        log_out = open(os.path.join(PROJECT_DIR, "watchdog_ui.log"), "a", encoding="utf-8")
        _proc = subprocess.Popen(
            [sys.executable, WATCHDOG_SCRIPT, folder_path],
            stdout=log_out, stderr=log_out,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        _start_time = time.time()

    return f"✅ 시작됨 (PID {_proc.pid}) │ 폴더: {folder_path}"


def stop_ingestion():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            return "🛑 중단 신호 전송 (현재 파일 처리 완료 후 종료)"
        return "⚠️ 실행 중인 프로세스가 없습니다."


# ─── 라이브 통계 ──────────────────────────────────────────────────────────────
def live_status():
    n_proc, n_fail = get_counts()
    running = is_running()
    status_icon = "🟢 실행 중" if running else "🔴 중지됨"

    elapsed = ""
    if _start_time and running:
        sec = int(time.time() - _start_time)
        elapsed = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"

    cloud_down = _is_cloud_down()
    cloud_icon = "🔴 다운(로컬 폴백)" if cloud_down else "🟢 정상"
    cloud_vram = _nvidia_smi_cloud() if not cloud_down else "  🔴 VM 오프 — 수동으로 켜세요"

    lines = [
        f"═══ Omni-Brain 상태 ({datetime.now().strftime('%H:%M:%S')}) ═══",
        f"  파이프라인 : {status_icon}  {elapsed}",
        f"  Cloud GPU  : {cloud_icon}  ({os.getenv('CLOUD_OLLAMA_URL','?')})",
        f"  임베딩 완료: {n_proc:,} 파일",
        f"  실패/건너뜀: {n_fail:,} 파일",
        "",
        "─── 로컬 하드웨어 ──────────────────────────────────────",
        system_stats(),
        "─── GCP VM GPU ─────────────────────────────────────────",
        cloud_vram,
    ]
    return "\n".join(lines)


def _is_cloud_down() -> bool:
    """간단한 Cloud Ollama 헬스체크."""
    try:
        import urllib.request
        url = os.getenv("CLOUD_OLLAMA_URL", "http://35.211.58.231:11434")
        urllib.request.urlopen(f"{url}/api/tags", timeout=3)
        return False
    except Exception:
        return True


# ─── Gradio UI 구성 ──────────────────────────────────────────────────────────
def build_ui():
    with gr.Blocks(title="Omni-Brain 학습 파이프라인") as app:

        gr.Markdown("# 🧠 Omni-Brain 학습 파이프라인")
        gr.Markdown(
            "로컬 CPU·GPU·RAM + GCP L4 GPU 를 모두 풀가동해 파일을 병렬 임베딩합니다."
        )

        # ── 탭 1: 실행 제어 ──────────────────────────────────────────────────
        with gr.Tab("▶ 실행"):
            with gr.Row():
                folder_input = gr.Textbox(
                    value=r"G:\\",
                    label="📂 처리할 최상위 폴더 경로",
                    placeholder=r"예: G:\내 드라이브  또는  /mnt/data",
                    scale=4,
                )
                with gr.Column(scale=1, min_width=140):
                    start_btn = gr.Button("▶ 시작", variant="primary", size="lg")
                    stop_btn  = gr.Button("■ 중단", variant="stop",    size="lg")

            run_msg = gr.Textbox(label="", lines=1, interactive=False)

            gr.Markdown("---")
            gr.Markdown("### 📁 추가 폴더 개별 투입")
            gr.Markdown("여러 폴더를 순차적으로 추가 투입합니다 (이미 처리된 파일은 SQLite로 자동 스킵).")

            with gr.Row():
                extra_input = gr.Textbox(
                    label="추가 폴더 경로",
                    placeholder=r"예: G:\수학노트",
                    scale=4,
                )
                add_btn = gr.Button("➕ 투입", scale=1)

            extra_log = gr.Textbox(label="투입 로그", lines=4, interactive=False)

            def _add_extra(path: str):
                path = path.strip()
                if not path or not os.path.isdir(path):
                    return f"❌ 유효하지 않은 경로: {path}"
                msg = start_ingestion(path)
                _extra_folders.append(path)
                return "\n".join([extra_log.value or "", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"])

            start_btn.click(start_ingestion, inputs=folder_input, outputs=run_msg)
            stop_btn.click(stop_ingestion, outputs=run_msg)
            add_btn.click(_add_extra, inputs=extra_input, outputs=extra_log)

        # ── 탭 2: 실시간 모니터 ──────────────────────────────────────────────
        with gr.Tab("📊 모니터"):
            status_box = gr.Textbox(
                label="실시간 상태",
                lines=12,
                interactive=False,
            )
            refresh_btn = gr.Button("🔄 수동 갱신")

            refresh_btn.click(live_status, outputs=status_box)
            # 탭 열릴 때 즉시 갱신
            app.load(live_status, outputs=status_box)

            # 5초 폴링
            gr.Timer(5.0).tick(live_status, outputs=status_box)

        # ── 탭 3: 실패 파일 ──────────────────────────────────────────────────
        with gr.Tab("❌ 실패 파일"):
            fail_table = gr.Dataframe(
                headers=["파일명", "시도횟수", "오류 내용", "마지막 시도"],
                datatype=["str", "number", "str", "str"],
                interactive=False,
                wrap=True,
            )
            refresh_fail_btn = gr.Button("🔄 목록 갱신")
            refresh_fail_btn.click(get_failed_rows, outputs=fail_table)
            app.load(get_failed_rows, outputs=fail_table)

        # ── 탭 4: 로그 뷰어 ─────────────────────────────────────────────────
        with gr.Tab("📜 에러 로그"):
            log_box = gr.Textbox(
                label=f"에러 로그 ({LOG_FILE})",
                lines=20,
                interactive=False,
                max_lines=40,
            )
            refresh_log_btn = gr.Button("🔄 로그 갱신")
            refresh_log_btn.click(tail_log, outputs=log_box)
            app.load(tail_log, outputs=log_box)
            gr.Timer(15.0).tick(tail_log, outputs=log_box)

        # ── 탭 5: GCP 관리 ───────────────────────────────────────────────────
        with gr.Tab("☁️ GCP"):
            gr.Markdown(f"""
### GCP Spot VM 관리

| 항목 | 값 |
|---|---|
| VM IP | `35.211.58.231` |
| Ollama 포트 | `11434` |
| 모델 | `qwen3-vl:8b` |

**Spot VM 선제 감지**: Cloud OCR 5회 연속 실패 시 자동으로 로컬 RTX 4060으로 전환 + 긴급 백업 실행

---
**Ollama 서버 시작 명령 (GCP VM 내부):**
```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

**VM 중지 (과금 방지):**
```bash
gcloud compute instances stop <VM_이름> --zone <ZONE>
```
""")
            cloud_status_btn = gr.Button("🔍 Cloud GPU 헬스체크")
            cloud_status_out = gr.Textbox(label="", lines=2, interactive=False)

            def check_cloud():
                down = _is_cloud_down()
                if down:
                    return "🔴 Cloud GPU 응답 없음 (Spot VM 선제됐거나 Ollama 미실행)\n→ 로컬 RTX 4060 폴백 중"
                return "🟢 Cloud GPU 정상 응답 (35.211.58.231:11434)"

            cloud_status_btn.click(check_cloud, outputs=cloud_status_out)

    return app


if __name__ == "__main__":
    try:
        import psutil
    except ImportError:
        print("💡 psutil이 없으면 자원 모니터가 제한됩니다. pip install psutil")

    print("=" * 55)
    print("  Omni-Brain UI 시작 중...")
    print("  브라우저: http://127.0.0.1:7860")
    print("=" * 55)

    app = build_ui()
    app.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        show_error=True,
        theme=gr.themes.Soft(),
    )
