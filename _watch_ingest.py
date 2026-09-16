# -*- coding: utf-8 -*-
"""
인제스트 파이프라인 모니터 + 자동 복구
- 60초마다: processed 카운트, SSH 좀비, VM VRAM 체크
- SSH 좀비 10개 초과 → 자동 종료
- 처리 5분째 0건 → 인제스트 재시작
- VM VRAM에 qwen3-vl 감지 → 자동 언로드
"""
import subprocess, sqlite3, time, os, sys, requests

LOG = "watch_ingest.log"
CHECK_INTERVAL = 60
STALL_THRESHOLD = 300  # 5분 처리 없으면 재시작

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def get_processed():
    try:
        c = sqlite3.connect("processed_files.db")
        n = c.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        c.close()
        return n
    except:
        return -1

def get_ssh_zombie_count():
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "(Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*gcp_key_fixed*'}).Count"],
            capture_output=True, text=True, timeout=10
        )
        return int(r.stdout.strip() or "0")
    except:
        return 0

def kill_ssh_zombies():
    subprocess.run(
        ["powershell", "-Command",
         "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*gcp_key_fixed*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }"],
        capture_output=True, timeout=15
    )

def get_ingest_pids():
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "(Get-WmiObject Win32_Process | Where-Object {$_.CommandLine -like '*1_ingest*'}).ProcessId"],
            capture_output=True, text=True, timeout=10
        )
        pids = [int(p) for p in r.stdout.strip().splitlines() if p.strip().isdigit()]
        return pids
    except:
        return []

def restart_ingest():
    log("⚠️  인제스트 재시작 중...")
    # 기존 종료
    subprocess.run(
        ["powershell", "-Command",
         "Get-WmiObject Win32_Process | Where-Object {$_.CommandLine -like '*1_ingest*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }"],
        capture_output=True, timeout=10
    )
    time.sleep(3)
    env = os.environ.copy()
    env.update({
        "FORCE_VM_ALL_FILES": "true",
        "USE_GCP_SSH_OCR": "true",
        "SKIP_LOCAL_OCR": "true",
        "MAX_GCP_WORKERS": "8",
        "PDF_CHUNK_PAGES": "5",
        "GCP_SSH_OCR_TIMEOUT_SEC": "600",
        "FORCE_CPU_EMBED": "true",
        "VISION_MODEL": "llava:7b",
    })
    subprocess.Popen(
        [r"venv\Scripts\python.exe", "-u", "-X", "utf8",
         "1_ingest_data.py", "G:\\내 드라이브\\해외 대학 편입 정리"],
        env=env, creationflags=0x00000008  # DETACHED_PROCESS
    )
    log("✅ 재시작 완료")

def check_vm_vram():
    """qwen3-vl:8b VRAM 점유 시 언로드"""
    try:
        r = subprocess.run(
            ["ssh", "-i", r"C:\Users\kjy\.ssh\gcp_key_fixed",
             "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             "kjy@34.75.63.124",
             "ollama ps 2>/dev/null"],
            capture_output=True, text=True, timeout=15
        )
        if "qwen3-vl" in r.stdout:
            log("⚠️  qwen3-vl VRAM 감지 → 언로드")
            subprocess.run(
                ["ssh", "-i", r"C:\Users\kjy\.ssh\gcp_key_fixed",
                 "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                 "kjy@34.75.63.124",
                 "python3 -c \"import requests; requests.post('http://localhost:11434/api/generate',json={'model':'qwen3-vl:8b','keep_alive':0},timeout=10)\""],
                capture_output=True, timeout=20
            )
            return True
        return False
    except Exception as e:
        log(f"VM 체크 오류: {e}")
        return False

def main():
    log("🔍 모니터 시작")
    last_count = get_processed()
    last_progress_time = time.time()

    while True:
        time.sleep(CHECK_INTERVAL)

        # 1. SSH 좀비 체크
        zombies = get_ssh_zombie_count()
        if zombies > 10:
            log(f"⚠️  SSH 좀비 {zombies}개 감지 → 종료")
            kill_ssh_zombies()

        # 2. 처리 카운트
        cur_count = get_processed()
        if cur_count < 0:
            log("DB 읽기 실패")
            continue

        delta = cur_count - last_count
        elapsed = time.time() - last_progress_time

        if delta > 0:
            log(f"📈 처리 +{delta}건 (총 {cur_count}건)")
            last_count = cur_count
            last_progress_time = time.time()
        else:
            idle_min = elapsed / 60
            log(f"⏸️  {idle_min:.1f}분째 진행 없음 (총 {cur_count}건)")

        # 3. 인제스트 프로세스 확인
        pids = get_ingest_pids()
        if not pids:
            log("❌ 인제스트 프로세스 없음 → 재시작")
            restart_ingest()
            last_progress_time = time.time()
            continue

        # 4. Stall 5분 이상
        if elapsed > STALL_THRESHOLD and len(pids) > 0:
            log(f"❌ {elapsed/60:.1f}분 스탈 감지 → 재시작")
            kill_ssh_zombies()
            restart_ingest()
            last_count = cur_count
            last_progress_time = time.time()

        # 5. VM VRAM 체크 (5분마다)
        if int(time.time()) % 300 < CHECK_INTERVAL:
            check_vm_vram()

if __name__ == "__main__":
    main()
