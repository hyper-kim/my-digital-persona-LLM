"""파이프라인 상시 모니터 — python _monitor.py 로 별도 터미널에서 실행.
60초마다 갱신. 이상 감지 시 소리/색상 경보.
"""
import os, sys, time, sqlite3, re, subprocess
from datetime import datetime, timedelta
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_files.db")
ERR_LOG     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingestion_log.txt")
OUT_LOG     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sequential_run_out.log")
ERR_TERM    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sequential_run_err.log")
INTERVAL    = 60   # seconds

ANSI_RED    = "\033[91m"
ANSI_GRN    = "\033[92m"
ANSI_YLW    = "\033[93m"
ANSI_CYN    = "\033[96m"
ANSI_RST    = "\033[0m"
ANSI_BOLD   = "\033[1m"

# ── DB 쿼리 ──────────────────────────────────────────────────────────────
def db_counts():
    try:
        conn = sqlite3.connect(DB, timeout=5)
        processed = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        failed    = conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0]
        conn.close()
        return processed, failed
    except Exception:
        return 0, 0

def db_recent_errors(n=10):
    try:
        conn = sqlite3.connect(DB, timeout=5)
        rows = conn.execute(
            "SELECT file_path, error FROM failed_files ORDER BY last_attempt DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []

# ── 로그 파일 파싱 ────────────────────────────────────────────────────────
def tail(path, lines=200):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except Exception:
        return []

def parse_progress(err_lines):
    """sequential_run_err.log tqdm 진행률 파싱 → (done, total, pct, speed, eta)"""
    for line in reversed(err_lines):
        m = re.search(r"(\d+)/(\d+)\s+\[[\d:]+<([\d:]+),\s+([\d.]+)s/file\]", line)
        if m:
            done  = int(m.group(1))
            total = int(m.group(2))
            eta_s = m.group(3)
            spd   = float(m.group(4))
            pct   = done / total * 100 if total else 0
            return done, total, pct, spd, eta_s
    return 0, 0, 0.0, 0.0, "?"

def parse_error_counts(log_lines):
    """ingestion_log 최근 200줄에서 에러 종류 카운트"""
    c = Counter()
    for line in log_lines:
        if "GCP-SSH" in line and "실패" in line:
            c["GCP-SSH 실패"] += 1
        elif "ABBYY-FUT" in line:
            c["ABBYY 실패"] += 1
        elif "EMPTY" in line:
            c["EMPTY"] += 1
        elif "ERROR" in line:
            c["기타 에러"] += 1
    return c

def parse_vm_state(out_lines):
    """sequential_run_out.log 최근 줄에서 VM 상태 파싱"""
    for line in reversed(out_lines):
        m = re.search(r"VM=([\w:]+)", line)
        if m:
            return m.group(1)
    return "UNKNOWN"

def parse_gcp_activity(out_lines):
    """GCP 성공/실패 카운트"""
    for line in reversed(out_lines):
        m = re.search(r"GCP활동\(성공(\d+)/실패(\d+)\)", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0

def detect_stall(err_lines, threshold_min=8):
    """마지막 진행 로그가 threshold_min 분 이상 없으면 stall 판단"""
    for line in reversed(err_lines):
        m = re.search(r"(\d+)/(\d+)\s+\[", line)
        if m:
            # tqdm은 타임스탬프 없음 → out.log 마지막 타임스탬프로 판단
            return False
    return True   # tqdm 라인 자체가 없으면 아직 시작 안 됐거나 종료

def last_log_age_min(out_lines):
    """마지막 [HH:MM:SS] 타임스탬프 기준 경과분"""
    now = datetime.now()
    for line in reversed(out_lines):
        m = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", line)
        if m:
            try:
                t = datetime.strptime(m.group(1), "%H:%M:%S").replace(
                    year=now.year, month=now.month, day=now.day
                )
                diff = (now - t).total_seconds() / 60
                return max(0, diff)
            except Exception:
                pass
    return 999

# ── 경보 소리 (Windows beep) ─────────────────────────────────────────────
def beep():
    try:
        import winsound
        winsound.Beep(880, 400)
    except Exception:
        pass

# ── 메인 루프 ────────────────────────────────────────────────────────────
def print_status(prev_processed):
    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    processed, failed = db_counts()
    delta      = processed - prev_processed

    err_lines  = tail(ERR_LOG, 200)
    out_lines  = tail(OUT_LOG, 60)
    term_lines = tail(ERR_TERM, 50)

    done, total, pct, spd, eta = parse_progress(term_lines)
    err_counts  = parse_error_counts(err_lines)
    vm_state    = parse_vm_state(out_lines)
    gcp_ok, gcp_fail = parse_gcp_activity(out_lines)
    log_age     = last_log_age_min(out_lines)

    # VM 상태 문자열
    if "404" in vm_state or "ERROR" in vm_state:
        vm_str = f"{ANSI_RED}⚠  VM 없음(404) — GCP 콘솔에서 인스턴스 재생성 필요{ANSI_RST}"
    elif vm_state == "RUNNING":
        vm_str = f"{ANSI_GRN}● RUNNING{ANSI_RST}"
    elif vm_state == "TERMINATED":
        vm_str = f"{ANSI_YLW}○ TERMINATED (시작 대기){ANSI_RST}"
    else:
        vm_str = f"{ANSI_YLW}? {vm_state}{ANSI_RST}"

    # 파이프라인 활성 여부
    if log_age > 10:
        alive_str = f"{ANSI_RED}⚠  마지막 활동 {log_age:.0f}분 전 — 멈춤 의심{ANSI_RST}"
        beep()
    else:
        alive_str = f"{ANSI_GRN}● 활성 ({log_age:.0f}분 전){ANSI_RST}"

    # 진행률 바
    bar_len = 30
    filled  = int(bar_len * pct / 100) if total else 0
    bar     = "█" * filled + "░" * (bar_len - filled)

    # 속도 (파일/분)
    speed_str = f"{spd:.1f}s/file ({60/spd:.0f}파일/분)" if spd > 0 else "측정중"

    os.system("cls")
    print(f"{ANSI_BOLD}{'='*62}{ANSI_RST}")
    print(f"  {ANSI_BOLD}⚙  파이프라인 모니터{ANSI_RST}  {now}")
    print(f"{'='*62}")

    print(f"  파이프라인:   {alive_str}")
    print(f"  GCP VM:       {vm_str}")
    print(f"  GCP 활동:     성공 {gcp_ok}  /  실패 {gcp_fail}")
    print()

    if total:
        print(f"  진행:  [{bar}] {pct:.1f}%  ({done:,}/{total:,})")
        print(f"  속도:  {speed_str}   ETA: {eta}")
    else:
        print(f"  진행:  tqdm 정보 없음 (아직 시작 전 또는 완료)")
    print()

    print(f"  DB 현황:")
    print(f"    처리 완료: {ANSI_GRN}{processed:,}{ANSI_RST}  "
          f"(+{delta} since last check)")
    print(f"    실패 누적: {ANSI_RED if failed > 100 else ANSI_YLW}{failed:,}{ANSI_RST}")
    print()

    if err_counts:
        print(f"  최근 200줄 에러 요약:")
        for kind, cnt in err_counts.most_common():
            col = ANSI_RED if cnt > 5 else ANSI_YLW
            print(f"    {col}{kind}: {cnt}건{ANSI_RST}")
        print()

    recent = db_recent_errors(5)
    if recent:
        print(f"  최근 실패 파일 (5개):")
        for fp, err in recent:
            short_fp  = os.path.basename(fp)[:50]
            short_err = str(err)[:70] if err else ""
            print(f"    {ANSI_YLW}{short_fp}{ANSI_RST}")
            print(f"      {short_err}")
        print()

    # 알림 메시지
    alerts = []
    if "404" in vm_state or "ERROR" in vm_state:
        alerts.append("🔴 GCP VM 인스턴스가 존재하지 않습니다. GCP 콘솔에서 재생성하세요.")
    if gcp_fail > 20 and gcp_ok == 0:
        alerts.append("🔴 GCP SSH 연속 실패 중 — VM 재생성 또는 IP 확인 필요")
    if delta == 0 and log_age < 10:
        alerts.append("🟡 처리 증가 없음 — 어렵거나 큰 파일 처리 중일 수 있음")
    if failed > 200:
        alerts.append(f"🟡 실패 누적 {failed}개 — _retry_empty_onenote.py --all 실행 고려")

    if alerts:
        print(f"  {ANSI_BOLD}[알림]{ANSI_RST}")
        for a in alerts:
            print(f"    {a}")
        print()

    print(f"  다음 갱신: {INTERVAL}초 후  (Ctrl+C 종료)")
    print(f"{'='*62}")

    return processed


def main():
    print("파이프라인 모니터 시작... (Ctrl+C로 종료)")
    prev = 0
    try:
        while True:
            prev = print_status(prev)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n모니터 종료.")


if __name__ == "__main__":
    main()
