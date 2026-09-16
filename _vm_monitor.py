"""
_vm_monitor.py
==============
GCP VM 자동 관리 데몬
- ingestion_log.txt 감시 → GCP SSH 활동 추적
- 이미지/OCR 필요 파일 처리 시작 시 VM 자동 시작
- VM_IDLE_TIMEOUT_MIN 분 동안 GCP 활동 없으면 VM 자동 중지
- sequential_run.py 종료 시 VM 중지

실행: venv\Scripts\python.exe -u _vm_monitor.py
(sequential_run.py 가 자동으로 백그라운드 시작)
"""
import os, sys, re, time, signal, threading, logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────
IDLE_TIMEOUT   = int(os.getenv("VM_IDLE_TIMEOUT_MIN", "10")) * 60   # 유휴 시 VM 종료 (초)
VM_AUTO_STOP   = os.getenv("VM_AUTO_STOP", "false").lower() in ("1", "true", "yes", "on")
START_COOLDOWN = 180   # VM 시작 후 최소 유지 시간 (초) - 너무 짧으면 재시작 반복
CHECK_INTERVAL = 30    # 로그 확인 주기 (초)
VM_STATUS_INTERVAL = 300  # VM 실제 상태 API 확인 주기 (초) - API 비용 절약

_PROJECT_DIR = os.getenv("PROJECT_DIR", os.path.dirname(os.path.abspath(__file__)))
LOG_FILE    = os.getenv("LOG_FILE",
              os.path.join(_PROJECT_DIR, "ingestion_log.txt"))
MON_LOG     = os.path.join(_PROJECT_DIR, "vm_monitor.log")

VM_NAME     = os.getenv("GCP_VM_NAME", "")
VM_ZONE     = os.getenv("GCP_VM_ZONE", "")

IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".gif"}

# ── 로깅 ──────────────────────────────────────────────────────────────
_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.append(logging.FileHandler(MON_LOG, encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="[VM-MON %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)

# ── 패턴 ──────────────────────────────────────────────────────────────
RE_GCP_SUCCESS = re.compile(r"\[GCP-SSH\].*\bOK\b|GCP.*완료", re.IGNORECASE)
RE_GCP_SENT    = re.compile(r"scp.*→.*GCP|GCP.*전송")
RE_GCP_FAIL    = re.compile(r"\[GCP-SSH\] 실패|\[GCP-SSH\].*exit status 255")
RE_GPU_ON      = re.compile(r"GPU OCR 활성화|GCP SSH OCR 활성화")

_stop_event = threading.Event()


# ── 유틸 ──────────────────────────────────────────────────────────────
def _read_new_lines(path: str, pos: int) -> tuple[list[str], int]:
    """pos 오프셋 이후 새로 추가된 줄만 반환. (pos, new_pos) 튜플 반환."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            new_lines = f.readlines()
            new_pos = f.tell()
        return new_lines, new_pos
    except Exception:
        try:
            return [], os.path.getsize(path)
        except Exception:
            return [], pos


def folder_has_images(folder: str) -> bool:
    """폴더 내 이미지 파일 여부 빠르게 확인"""
    if not os.path.isdir(folder):
        return False
    try:
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                    return True
    except Exception:
        pass
    return False


def _call_manager_ip(new_ip: str) -> None:
    """_vm_manager.update_env_ip 호출 후 글로벌 CLOUD_VM_IP 재로드."""
    try:
        import _vm_manager as m
        m.update_env_ip(new_ip)
        # 1_ingest_data 등이 import될 때 읽은 값은 변경 불가이므로
        # 실제 SSH 연결이 새 IP를 쓰도록 load_dotenv re-override
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception as e:
        logging.error(f"[ENV] IP 업데이트 실패: {e}")


def _call_manager(fn_name: str) -> str:
    try:
        import _vm_manager as m
        return getattr(m, fn_name)()
    except Exception as e:
        return f"error:{e}"


# ── 메인 루프 ─────────────────────────────────────────────────────────
def monitor_loop():
    last_gcp_t   = 0.0    # 마지막 GCP 성공 타임스탬프
    last_vm_api  = 0.0    # 마지막 VM 상태 API 호출
    vm_started_t = 0.0    # VM 시작 요청 타임스탬프
    vm_state     = "UNKNOWN"   # RUNNING / TERMINATED / UNKNOWN
    consec_fail  = 0
    _start_backoff_until = 0.0  # Spot 리소스 부족 시 재시도 유예 시각
    # 로그 파일 오프셋 — 기동 시점 이전의 과거 기록은 무시
    try:
        _log_pos = os.path.getsize(LOG_FILE)
    except Exception:
        _log_pos = 0

    logging.info(f"VM 모니터 시작 | VM={VM_NAME or '미설정'} ZONE={VM_ZONE or '미설정'} "
                 f"IDLE_TIMEOUT={IDLE_TIMEOUT//60}분 AUTO_STOP={VM_AUTO_STOP}")

    if not VM_NAME or not VM_ZONE:
        logging.warning("⚠️  GCP_VM_NAME / GCP_VM_ZONE 미설정 → VM 자동 관리 비활성")

    while not _stop_event.wait(CHECK_INTERVAL):
        now = time.time()
        lines, _log_pos = _read_new_lines(LOG_FILE, _log_pos)

        # GCP 활동 집계 (새로 추가된 줄만)
        success = sum(1 for ln in lines if RE_GCP_SUCCESS.search(ln)
                                        or RE_GCP_SENT.search(ln))
        fail    = sum(1 for ln in lines if RE_GCP_FAIL.search(ln))

        if success > 0:
            last_gcp_t   = now
            consec_fail  = 0
        consec_fail += fail

        idle_secs = (now - last_gcp_t) if last_gcp_t > 0 else 9999

        # VM 실제 상태 주기적 API 확인
        if VM_NAME and VM_ZONE and (now - last_vm_api > VM_STATUS_INTERVAL):
            vm_state    = _call_manager("get_status")
            last_vm_api = now
            logging.info(f"VM={vm_state} | GCP활동(성공{success}/실패{fail}) | "
                         f"유휴{idle_secs:.0f}s")

        if not VM_NAME or not VM_ZONE:
            continue

        # ── VM RUNNING인데 SSH 연속실패 → IP 변경 감지 (재시작 금지) ──
        if consec_fail >= 5 and vm_state == "RUNNING":
            logging.info(f"VM RUNNING 상태인데 SSH 연속실패 {consec_fail}회 → IP 변경 여부 확인")
            try:
                import _vm_manager as m
                actual_ip = m.get_external_ip()
                env_ip = os.getenv("CLOUD_VM_IP", "")
                if actual_ip and actual_ip != env_ip:
                    logging.info(f"IP 변경 감지: {env_ip} → {actual_ip}, .env 업데이트")
                    _call_manager_ip(actual_ip)
                else:
                    logging.info(f"IP 동일({actual_ip}), VM 재시작은 수행하지 않음 (과잉복구 방지)")
                consec_fail = 0
                last_vm_api = 0
            except Exception as e:
                logging.error(f"IP 확인 실패: {e}")
                consec_fail = 0

        # ── VM 시작 조건 ──
        # 연속 실패 5개 이상이고 VM이 꺼져있음 → 재시작
        if consec_fail >= 5 and vm_state != "RUNNING":
            if now < _start_backoff_until:
                remaining = int(_start_backoff_until - now)
                logging.info(f"리소스 부족 백오프 중 ({remaining}s 남음) → VM 시작 보류")
                consec_fail = 0
            elif "404" in vm_state or "not_found" in vm_state.lower():
                # ── 인스턴스 자체가 없음 → 새로 생성 ──
                logging.info(f"GCP SSH 연속실패 {consec_fail}회, VM 404 → 새 인스턴스 생성 (Standard)")
                result = _call_manager("create_vm")          # dict {"status":..., "ip":...}
                logging.info(f"VM create = {result}")
                if isinstance(result, dict):
                    status = result.get("status", "")
                    new_ip = result.get("ip", "")
                    if status.startswith("ok") or "already_exists" in status:
                        vm_state     = "STAGING"
                        vm_started_t = now
                        if new_ip:
                            _call_manager_ip(new_ip)         # .env IP 업데이트
                            logging.info(f"VM IP 업데이트: {new_ip}")
                    elif "RESOURCE_POOL_EXHAUSTED" in status.upper():
                        logging.warning("⚠️  Standard VM 리소스 부족 → 10분 후 재시도")
                        _start_backoff_until = now + 600
                    else:
                        logging.error(f"VM 생성 실패: {status}")
                        _start_backoff_until = now + 300    # 5분 후 재시도
                consec_fail = 0
                last_vm_api = 0
            else:
                logging.info(f"GCP SSH 연속실패 {consec_fail}회 → VM 시작 시도")
                r = _call_manager("start_vm")
                logging.info(f"VM start = {r}")
                if r == "ok" or r in ("already_running", "starting"):
                    vm_state     = "STAGING"
                    vm_started_t = now
                    _start_backoff_until = 0
                elif r and "RESOURCE_POOL_EXHAUSTED" in r.upper():
                    logging.warning("⚠️  Spot VM 리소스 부족 (ZONE_RESOURCE_POOL_EXHAUSTED) "
                                    "→ 10분 후 재시도")
                    _start_backoff_until = now + 600
                consec_fail  = 0
                last_vm_api  = 0   # 다음 주기에 상태 재확인

        # ── VM 중지 조건 ──
        # 유휴 타임아웃 + 최소 유지 시간 경과
        min_hold_ok = (now - vm_started_t) > START_COOLDOWN
        # last_gcp_t=0 이면 VM 시작 후 IDLE_TIMEOUT 동안 GCP 한 번도 안 쓴 것 → 동일 처리
        effective_last_gcp = last_gcp_t if last_gcp_t > 0 else vm_started_t
        effective_idle = (now - effective_last_gcp) if effective_last_gcp > 0 else 0
        if (VM_AUTO_STOP
            and effective_last_gcp > 0
                and effective_idle > IDLE_TIMEOUT
                and vm_state == "RUNNING"
                and min_hold_ok):
            reason = "GCP 미사용" if last_gcp_t == 0 else f"GCP 유휴 {effective_idle/60:.1f}분"
            logging.info(f"{reason} 초과 → VM 중지")
            r = _call_manager("stop_vm")
            logging.info(f"VM stop = {r}")
            vm_state   = "STOPPING"
            last_gcp_t = 0

    # ── 종료 시 VM 중지 ──
    logging.info("모니터 종료 신호 수신")
    if VM_NAME and VM_ZONE and vm_state == "RUNNING":
        logging.info("파이프라인 완료 → VM 자동 중지")
        r = _call_manager("stop_vm")
        logging.info(f"VM stop = {r}")
    logging.info("VM 모니터 종료")


def main():
    t = threading.Thread(target=monitor_loop, daemon=True, name="vm-monitor")
    t.start()

    def _sig(sig, frame):
        logging.info("SIGNAL 수신 → 종료")
        _stop_event.set()

    try:
        signal.signal(signal.SIGINT,  _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    _stop_event.wait()
    t.join(timeout=60)


if __name__ == "__main__":
    main()
