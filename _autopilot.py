"""
_autopilot.py — 완전 자율 파이프라인 감시/복구 데몬
=====================================================
내가 직접 고칩니다. 사람이 볼 필요 없습니다.

체크 주기별 동작:
  [30초] 프로세스 생존 확인 (sequential_run, watchdog, vm_monitor)
  [5분]  VM IP 변경 감지 → .env + 실행 중인 ingest 프로세스 자동 재시작
  [5분]  WinError 10061 누적 실패 파일 → VM RUNNING 확인 후 DB 정리 (재처리)
  [10분] DB 처리건수 스탈 감지 → 파이프라인 재시작
  [10분] GCP Ops Agent 건강 확인 → 설치/재시작
  [30분] VM 디스크/메모리 이상 감지

실행: venv\Scripts\python.exe -u -X utf8 _autopilot.py
"""

import os, sys, re, time, sqlite3, subprocess, logging, signal, threading
from datetime import datetime
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(PROJECT_DIR, "processed_files.db")
LOG_PATH     = os.path.join(PROJECT_DIR, "autopilot.log")
VENV_PY      = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
PYTHON_EXE   = VENV_PY if os.path.exists(VENV_PY) else sys.executable
SSH_KEY      = r"C:\Users\kjy\.ssh\gcp_key_fixed"
VM_USER      = "kjy"

load_dotenv()

# ── 로깅 ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[AUTOPILOT %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("autopilot")

_stop = threading.Event()


# ── 유틸 ───────────────────────────────────────────────────────────────
def reload_env():
    load_dotenv(override=True)


def get_env_ip() -> str:
    reload_env()
    return os.getenv("CLOUD_VM_IP", "")


def db_count() -> int:
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            return c.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    except Exception:
        return 0


def db_fail_count() -> int:
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            return c.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0]
    except Exception:
        return 0


def running_pids(script_fragment: str) -> list:
    """스크립트명 일부 포함 python 프로세스 PID 목록"""
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             f"(Name like '%python%' AND CommandLine like '%{script_fragment}%')",
             "get", "ProcessId", "/value"],
            capture_output=True, text=True, timeout=10
        )
        return [int(m) for m in re.findall(r"ProcessId=(\d+)", result.stdout) if int(m) != os.getpid()]
    except Exception:
        return []


def kill_pids(pids: list, label: str):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
            log.info(f"  종료: {label} PID {pid}")
        except Exception:
            pass


def start_bg(script: str, args: list = None, label: str = "") -> subprocess.Popen:
    cmd = [PYTHON_EXE, "-u", "-X", "utf8", os.path.join(PROJECT_DIR, script)] + (args or [])
    p = subprocess.Popen(
        cmd,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        stdout=open(os.path.join(PROJECT_DIR, f"{script}.out.log"), "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    log.info(f"  시작: {label or script} PID {p.pid}")
    return p


def ssh_run(ip: str, cmd: str, timeout: int = 20) -> tuple:
    """SSH 단일 명령 실행 → (returncode, stdout)"""
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={timeout}", f"{VM_USER}@{ip}", cmd],
            capture_output=True, text=True, timeout=timeout + 5
        )
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)


# ── 핵심 복구 함수들 ────────────────────────────────────────────────────

def fix_vm_ip() -> bool:
    """VM IP 변경 감지 → .env 업데이트 → ingest 재시작 (변경 있을 때만)"""
    try:
        import _vm_manager as m
        actual_ip = m.get_external_ip()
        env_ip    = get_env_ip()
        if not actual_ip:
            return False
        if actual_ip == env_ip:
            return False
        log.info(f"[IP] 변경 감지: {env_ip} → {actual_ip}")
        m.update_env_ip(actual_ip)
        reload_env()
        # 잘못된 IP로 묶인 ingest 프로세스 재시작
        pids = running_pids("1_ingest_data.py")
        if pids:
            log.info(f"[IP] ingest 프로세스 재시작 (IP 업데이트 반영) - PID {pids}")
            kill_pids(pids, "1_ingest_data")
            # watchdog이 자동으로 재시작함
        return True
    except Exception as e:
        log.error(f"[IP] 확인 실패: {e}")
        return False


def clear_connection_errors():
    """WinError 10061 / timed out 실패 → VM이 RUNNING이면 DB에서 삭제해 재처리"""
    try:
        import _vm_manager as m
        state = m.get_status()
        if state != "RUNNING":
            return 0
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            cur = c.execute(
                "SELECT COUNT(*) FROM failed_files WHERE "
                "error LIKE '%10061%' OR error LIKE '%timed out%' OR "
                "error LIKE '%ConnectionRefusedError%' OR error LIKE '%ConnectTimeout%'"
            )
            cnt = cur.fetchone()[0]
            if cnt > 0:
                c.execute(
                    "DELETE FROM failed_files WHERE "
                    "error LIKE '%10061%' OR error LIKE '%timed out%' OR "
                    "error LIKE '%ConnectionRefusedError%' OR error LIKE '%ConnectTimeout%'"
                )
                log.info(f"[DB] 연결오류 {cnt}건 삭제 → 재처리 큐 복귀")
        return cnt
    except Exception as e:
        log.error(f"[DB] 연결오류 정리 실패: {e}")
        return 0


def ensure_vm_monitor():
    """_vm_monitor.py 실행 중인지 확인 → 없으면 재시작"""
    pids = running_pids("_vm_monitor.py")
    # 2개 이상 중복 → 1개만 남김
    if len(pids) > 1:
        log.info(f"[VMM] 중복 실행 {len(pids)}개 → 1개만 유지")
        kill_pids(pids[1:], "_vm_monitor")
        return
    if len(pids) == 0:
        log.info("[VMM] _vm_monitor.py 없음 → 재시작")
        start_bg("_vm_monitor.py", label="vm_monitor")


def ensure_pipeline():
    """sequential_run.py + run_watchdog.py 생존 확인 → 죽어있으면 재시작"""
    seq_pids = running_pids("sequential_run.py")
    wdg_pids = running_pids("run_watchdog.py")

    if not seq_pids and not wdg_pids:
        log.info("[PIPE] sequential_run.py + watchdog 모두 없음 → sequential_run 재시작")
        start_bg("sequential_run.py", label="sequential_run")
        return

    # watchdog만 없는 경우 (sequential_run이 다음 폴더 실행 전 대기 중이면 정상)
    if seq_pids and not wdg_pids:
        log.info("[PIPE] watchdog 없음 (폴더 전환 중일 수 있음) — 1분 후 재확인")


def detect_and_fix_stall(prev_count: int, stall_since: float) -> tuple:
    """
    처리건수 증가 없으면 stall 감지 → 20분 이상이면 파이프라인 재시작
    Returns: (new_count, new_stall_since)
    """
    now   = time.time()
    count = db_count()

    if count > prev_count:
        return count, 0.0  # 정상 진행 중

    if stall_since == 0.0:
        return count, now  # 처음 멈춤 감지

    stall_min = (now - stall_since) / 60
    if stall_min >= 20:
        log.warning(f"[STALL] {stall_min:.0f}분째 처리 정지 → 파이프라인 재시작")
        # ingest만 재시작 (sequential_run은 유지)
        ingest_pids = running_pids("1_ingest_data.py")
        if ingest_pids:
            kill_pids(ingest_pids, "1_ingest_data")
        # watchdog이 재시작하게 내버려둠 (없으면 sequential_run이 재실행)
        ensure_pipeline()
        return count, 0.0  # 재시작 후 카운터 리셋

    return count, stall_since


def fix_ops_agent():
    """VM에서 Ops Agent 살아있는지 확인 → 죽었으면 재시작, 없으면 설치"""
    ip = get_env_ip()
    if not ip:
        return
    rc, out = ssh_run(ip, "systemctl is-active google-cloud-ops-agent 2>&1", timeout=15)
    if rc != 0 or "inactive" in out or "failed" in out:
        log.info(f"[OPS] Ops Agent 상태: '{out}' → 복구 시도")
        if "not-found" in out or "could not be found" in out:
            log.info("[OPS] Ops Agent 미설치 → 설치 중...")
            install_cmd = (
                "curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh "
                "&& sudo bash add-google-cloud-ops-agent-repo.sh --also-install 2>&1 | tail -5"
            )
            rc2, out2 = ssh_run(ip, install_cmd, timeout=120)
            log.info(f"[OPS] 설치 결과: rc={rc2} {out2[-100:]}")
        else:
            rc2, out2 = ssh_run(ip, "sudo systemctl restart google-cloud-ops-agent 2>&1", timeout=20)
            log.info(f"[OPS] 재시작 결과: rc={rc2} {out2}")
    # GPU(nvml) 메트릭은 설치 직후 자동 수집 안 됨 → 별도 설정 없이 기본 메트릭만 사용


def check_vm_health():
    """VM 메모리/디스크 간단 확인"""
    ip = get_env_ip()
    if not ip:
        return
    rc, out = ssh_run(ip,
        "free -h | awk '/^Mem/{print \"MEM:\", $3\"/\"$2}' && "
        "df -h / | awk 'NR==2{print \"DISK:\", $3\"/\"$2, $5}'",
        timeout=15
    )
    if rc == 0 and out:
        log.info(f"[VM-HEALTH] {out.replace(chr(10), ' | ')}")


# ── 메인 루프 ──────────────────────────────────────────────────────────
def autopilot_loop():
    log.info("=" * 60)
    log.info("오토파일럿 시작 — 이제부터 내가 관리합니다")
    log.info("=" * 60)

    last_ip_check    = 0.0
    last_conn_clear  = 0.0
    last_stall_check = 0.0
    last_ops_check   = 0.0
    last_health      = 0.0
    last_proc_check  = 0.0

    stall_since      = 0.0
    prev_count       = db_count()

    # 시작 즉시 IP 확인
    fix_vm_ip()
    clear_connection_errors()

    while not _stop.wait(30):  # 30초마다 깨어남
        now = time.time()

        # [30초] 프로세스 생존 확인
        if now - last_proc_check >= 30:
            ensure_vm_monitor()
            ensure_pipeline()
            last_proc_check = now

        # [5분] VM IP 변경 감지
        if now - last_ip_check >= 300:
            fix_vm_ip()
            last_ip_check = now

        # [5분] 연결오류 실패 파일 정리
        if now - last_conn_clear >= 300:
            cleared = clear_connection_errors()
            if cleared:
                log.info(f"[DB] {cleared}건 재처리 큐에 복귀")
            last_conn_clear = now

        # [10분] 스탈 감지
        if now - last_stall_check >= 600:
            prev_count, stall_since = detect_and_fix_stall(prev_count, stall_since)
            last_stall_check = now

        # [15분] Ops Agent 확인
        if now - last_ops_check >= 900:
            fix_ops_agent()
            last_ops_check = now

        # [30분] VM 헬스 체크
        if now - last_health >= 1800:
            check_vm_health()
            last_health = now

    log.info("오토파일럿 종료")


def main():
    t = threading.Thread(target=autopilot_loop, daemon=True, name="autopilot")
    t.start()

    def _sig(sig, frame):
        log.info("종료 신호 수신")
        _stop.set()

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    _stop.wait()
    t.join(timeout=30)


if __name__ == "__main__":
    main()
