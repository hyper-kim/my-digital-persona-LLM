"""
_overnight_master.py ─ 오버나이트 완전 처리 마스터 스크립트
=============================================================
Phase 0: 기존 파이썬 프로세스 정리
Phase 1: Qdrant SQLite 비정상 벡터 삭제 (PENDING_OCR/EMPTY/ERROR)
Phase 2: processed_files.db 복원 (Qdrant 정상 벡터 → DB)
Phase 3: .env 리소스 최대화
Phase 4: sequential_run.py 파이프라인 실행
Phase 5: 5분 간격 모니터링
Phase 6: 완료 후 품질 검증 + VM SSH 종료

실행: python _overnight_master.py
"""

import os, sys, sqlite3, pickle, json, subprocess, time, signal
from datetime import datetime

# ─── 경로 설정 ───────────────────────────────────────────────
BASE   = os.path.dirname(os.path.abspath(__file__))
Q_DB   = os.path.join(BASE, "qdrant_db", "collection", "omni_persona_v3", "storage.sqlite")
P_DB   = os.path.join(BASE, "processed_files.db")
LOCK   = os.path.join(BASE, "qdrant_db", ".lock")
COLL   = "omni_persona_v3"
LOG    = os.path.join(BASE, "overnight_progress.log")
PYTHON = os.path.join(BASE, "venv", "Scripts", "python.exe")
SEQ    = os.path.join(BASE, "sequential_run.py")

VM_IP  = "35.211.58.231"
VM_USR = "kjy"
SSH_KEY= r"C:\Users\kjy\.ssh\gcp_key_fixed"

BAD_MARKERS = ["PENDING_OCR", "[VISUAL_CONTENT_PENDING_OCR]", "EMPTY_CONTENT",
               "❌ ERROR", "❌ERROR", "EMPTY_TEXT", "[EMPTY]"]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ─── 유틸 ─────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def remove_lock():
    if os.path.exists(LOCK):
        try:
            os.remove(LOCK)
            log("  Qdrant .lock 제거")
        except Exception as e:
            log(f"  .lock 제거 실패: {e}")


# ─── Phase 0: 기존 파이썬 프로세스 정리 ──────────────────────────
def phase0_cleanup_processes():
    log("=" * 60)
    log("Phase 0: Qdrant 락 파일 정리")
    remove_lock()
    time.sleep(1)


# ─── Phase 1: Qdrant 비정상 벡터 삭제 ────────────────────────────
def phase1_cleanup_qdrant():
    log("=" * 60)
    log("Phase 1: Qdrant SQLite 비정상 벡터 삭제")
    
    db = sqlite3.connect(Q_DB, timeout=60)
    total = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    log(f"  전체 포인트 수: {total:,}")

    bad_ids = []
    good_paths = set()
    hansung_total = 0
    batch_size = 5000
    offset = 0
    processed = 0

    while True:
        rows = db.execute("SELECT id, point FROM points LIMIT ? OFFSET ?",
                          (batch_size, offset)).fetchall()
        if not rows:
            break
        for (pid, blob) in rows:
            try:
                pt = pickle.loads(blob)
                payload = getattr(pt, "payload", None) or {}
                fp = payload.get("file_path", "") or ""
                if "한성과고" not in fp:
                    continue
                hansung_total += 1
                nc = payload.get("_node_content", "") or ""
                try:
                    text = json.loads(nc).get("text", "") or ""
                except Exception:
                    text = nc
                if any(m in text for m in BAD_MARKERS):
                    bad_ids.append(pid)
                else:
                    good_paths.add(fp)
            except Exception:
                pass
        processed += len(rows)
        offset += batch_size
        if processed % 100000 == 0:
            log(f"  스캔 진행: {processed:,}/{total:,} | 한성과고: {hansung_total:,} | 비정상: {len(bad_ids):,}")

    log(f"  스캔 완료: 한성과고 총 {hansung_total:,}청크, 비정상 {len(bad_ids):,}개, 정상 파일 {len(good_paths):,}개")

    # 비정상 벡터 삭제
    if bad_ids:
        log(f"  비정상 {len(bad_ids):,}개 삭제 중...")
        chunk_size = 500
        for i in range(0, len(bad_ids), chunk_size):
            batch = bad_ids[i:i + chunk_size]
            placeholders = ",".join("?" * len(batch))
            db.execute(f"DELETE FROM points WHERE id IN ({placeholders})", batch)
        db.commit()
        after = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        log(f"  삭제 후 전체 포인트: {after:,}")
        log("  VACUUM 실행 중 (DB 최적화)...")
        db.execute("VACUUM")
        db.commit()
        log("  VACUUM 완료")
    else:
        log("  비정상 벡터 없음 (이미 정상)")

    db.close()
    return good_paths


# ─── Phase 2: processed_files.db 복원 ───────────────────────────
def phase2_restore_db(good_paths: set):
    log("=" * 60)
    log("Phase 2: processed_files.db 복원")

    conn = sqlite3.connect(P_DB, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS processed (file_path TEXT PRIMARY KEY)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_files (
            file_path TEXT PRIMARY KEY,
            error_msg TEXT,
            timestamp TEXT
        )
    """)
    
    before = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    log(f"  복원 전 DB 행 수: {before:,}")
    
    inserted = 0
    for fp in good_paths:
        try:
            conn.execute("INSERT OR IGNORE INTO processed (file_path) VALUES (?)", (fp,))
            inserted += 1
        except Exception:
            pass
    conn.commit()
    
    after = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    log(f"  복원 후 DB 행 수: {after:,} (+{after - before:,}건 추가)")
    conn.close()


# ─── Phase 3: .env 리소스 최대화 ─────────────────────────────────
def phase3_maximize_resources():
    log("=" * 60)
    log("Phase 3: .env 리소스 최대화 (RAM 32GB, L4 GPU 최대 활용)")
    
    env_path = os.path.join(BASE, ".env")
    if not os.path.exists(env_path):
        log("  .env 없음, 스킵")
        return
    
    with open(env_path, encoding="utf-8") as f:
        content = f.read()
    
    original = content
    # 워커 수 최대화
    replacements = {
        "MAX_VISION_WORKERS=2": "MAX_VISION_WORKERS=6",
        "MAX_VISION_WORKERS=1": "MAX_VISION_WORKERS=6",
        "MAX_AUDIO_WORKERS=1":  "MAX_AUDIO_WORKERS=2",
        "MAX_GCP_WORKERS=4":    "MAX_GCP_WORKERS=8",
        "MAX_COORD_WORKERS=4":  "MAX_COORD_WORKERS=8",
    }
    for old, new in replacements.items():
        if old in content:
            content = content.replace(old, new)
            log(f"  {old} → {new}")
    
    # SKIP_LOCAL_OCR=false 확인 (VM 없이도 처리 중단 X)
    if "SKIP_LOCAL_OCR=true" in content:
        content = content.replace("SKIP_LOCAL_OCR=true", "SKIP_LOCAL_OCR=false")
        log("  SKIP_LOCAL_OCR=true → false (VM OCR 활성)")
    
    if content != original:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
        log("  .env 저장 완료")
    else:
        log("  .env 이미 최적화됨")


# ─── Phase 4: 파이프라인 실행 ─────────────────────────────────────
def phase4_run_pipeline():
    log("=" * 60)
    log("Phase 4: sequential_run.py 파이프라인 시작")
    log("  순서: 한성과고 → 해외대학편입정리 → Takeout")
    
    proc = subprocess.Popen(
        [PYTHON, "-u", "-X", "utf8", SEQ],
        cwd=BASE,
        stdout=open(os.path.join(BASE, "overnight_pipeline.log"), "w", encoding="utf-8", buffering=1),
        stderr=subprocess.STDOUT,
    )
    log(f"  파이프라인 PID: {proc.pid}")
    return proc


# ─── Phase 5: 모니터링 ────────────────────────────────────────────
def phase5_monitor(proc):
    log("=" * 60)
    log("Phase 5: 5분 간격 모니터링 시작")
    
    last_count = 0
    stall_count = 0
    
    while True:
        ret = proc.poll()
        if ret is not None:
            log(f"  파이프라인 종료 (exit code: {ret})")
            break
        
        # DB 카운트
        try:
            conn = sqlite3.connect(P_DB, timeout=5)
            count = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
            conn.close()
        except Exception:
            count = -1
        
        # GPU (nvidia-smi)
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            gpu_info = r.stdout.strip()
        except Exception:
            gpu_info = "N/A"
        
        # VM GPU (SSH)
        try:
            r = subprocess.run(
                ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=5",
                 f"{VM_USR}@{VM_IP}",
                 "nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv,noheader 2>/dev/null"],
                capture_output=True, text=True, timeout=10
            )
            vm_gpu = r.stdout.strip()
        except Exception:
            vm_gpu = "N/A"
        
        delta = count - last_count if last_count >= 0 else 0
        log(f"  DB: {count:,}건 (+{delta}/5분) | 로컬GPU: {gpu_info} | VMGPU: {vm_gpu}")
        
        if delta == 0 and count > 0:
            stall_count += 1
            if stall_count >= 6:  # 30분 이상 변화 없으면 경고
                log("  ⚠️  30분간 DB 증가 없음 → 파이프라인 확인 필요")
                stall_count = 0
        else:
            stall_count = 0
        
        last_count = count
        time.sleep(300)  # 5분


# ─── Phase 6: 완료 후 품질 검증 + VM 종료 ────────────────────────
def phase6_verify_and_shutdown():
    log("=" * 60)
    log("Phase 6: 품질 검증 + VM 종료")
    
    # 비정상 벡터 잔량 재확인
    try:
        db = sqlite3.connect(Q_DB, timeout=30)
        total = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        
        remaining_bad = 0
        batch_size = 5000
        offset = 0
        while True:
            rows = db.execute("SELECT point FROM points LIMIT ? OFFSET ?",
                              (batch_size, offset)).fetchall()
            if not rows:
                break
            for (blob,) in rows:
                try:
                    pt = pickle.loads(blob)
                    payload = getattr(pt, "payload", None) or {}
                    fp = payload.get("file_path", "") or ""
                    if "한성과고" not in fp:
                        continue
                    nc = payload.get("_node_content", "") or ""
                    try:
                        text = json.loads(nc).get("text", "") or ""
                    except Exception:
                        text = nc
                    if any(m in text for m in BAD_MARKERS):
                        remaining_bad += 1
                except Exception:
                    pass
            offset += batch_size
        db.close()
        log(f"  최종 검증: 전체 {total:,}포인트, 잔여 비정상 {remaining_bad:,}개")
    except Exception as e:
        log(f"  품질 검증 오류: {e}")
    
    # 최종 DB 카운트
    try:
        conn = sqlite3.connect(P_DB, timeout=5)
        final_count = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        conn.close()
        log(f"  최종 DB: {final_count:,}건 처리 완료")
    except Exception as e:
        log(f"  DB 확인 오류: {e}")
    
    # VM SSH 종료
    log("  GCP VM SSH 종료 시도...")
    try:
        result = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10",
             f"{VM_USR}@{VM_IP}",
             "sudo shutdown -h now"],
            capture_output=True, text=True, timeout=15
        )
        log(f"  VM 종료 명령 전송 완료 (stdout: {result.stdout.strip()[:100]})")
    except Exception as e:
        log(f"  VM 종료 실패 (수동으로 종료 필요): {e}")
    
    log("=" * 60)
    log("🏁 오버나이트 처리 완전 완료!")


# ─── 메인 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 60)
    log("_overnight_master.py 시작")
    log(f"  시작 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    phase0_cleanup_processes()
    good_paths = phase1_cleanup_qdrant()
    phase2_restore_db(good_paths)
    phase3_maximize_resources()
    proc = phase4_run_pipeline()
    
    try:
        phase5_monitor(proc)
    except KeyboardInterrupt:
        log("  사용자 중단 (KeyboardInterrupt)")
        proc.terminate()
    
    phase6_verify_and_shutdown()
