"""
_vm_manager.py  (rename from _gcp_vm_info.py)
==============================================
GCP VM 시작 / 정지 / Spot→Standard 업그레이드
SA 에 'Compute Instance Admin (v1)' 역할 필요:
  GCP 콘솔 → IAM → mydigital-persona-embedder@... → 역할 추가
"""
import os, sys, time, logging
from dotenv import load_dotenv
load_dotenv(override=True)

try:
    import google.auth.transport.requests
    from google.oauth2 import service_account
    import requests as _req
    _GCP_AVAIL = True
except ImportError:
    _GCP_AVAIL = False

SA_KEY   = os.getenv("GOOGLE_SA_KEY_PATH",
           r"C:\Users\kjy\.ssh\project-1ffe4ac8-e493-4a26-ac3-9adced7fa7f2.json")
PROJECT  = os.getenv("GCP_PROJECT_ID",  "project-1ffe4ac8-e493-4a26-ac3")
VM_ZONE  = os.getenv("GCP_VM_ZONE",  "")
VM_NAME  = os.getenv("GCP_VM_NAME",  "")

_creds = None
_creds_ts = 0.0

def _get_token():
    global _creds, _creds_ts
    if not _GCP_AVAIL:
        raise ImportError("google-auth 또는 requests 미설치")
    if _creds is None or time.time() - _creds_ts > 3000:
        _creds = service_account.Credentials.from_service_account_file(
            SA_KEY,
            scopes=["https://www.googleapis.com/auth/compute"]
        )
        _creds.refresh(google.auth.transport.requests.Request())
        _creds_ts = time.time()
    return _creds.token

def _hdr():
    return {"Authorization": f"Bearer {_get_token()}"}

def _base():
    return (f"https://compute.googleapis.com/compute/v1"
            f"/projects/{PROJECT}/zones/{VM_ZONE}/instances/{VM_NAME}")

# ── 공개 API ──────────────────────────────────────────────────────────

def get_status() -> str:
    """RUNNING / TERMINATED / STAGING / STOPPING / UNKNOWN / ERROR:..."""
    if not VM_NAME or not VM_ZONE:
        return "UNCONFIGURED"
    try:
        r = _req.get(_base(), headers=_hdr(), timeout=10)
        if r.status_code == 200:
            return r.json().get("status", "UNKNOWN")
        return f"ERROR:{r.status_code}"
    except Exception as e:
        return f"ERROR:{e}"

def _wait_operation(op_url: str, timeout: int = 120) -> str:
    """zone operation 완료까지 폴링. 반환: done / fail:REASON"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _req.get(op_url, headers=_hdr(), timeout=10)
            if r.status_code != 200:
                return f"fail:op_http_{r.status_code}"
            d = r.json()
            if d.get("status") == "DONE":
                errs = d.get("error", {}).get("errors", [])
                if errs:
                    code = errs[0].get("code", "UNKNOWN")
                    msg  = errs[0].get("message", "")
                    return f"fail:{code} {msg}"
                return "done"
        except Exception as e:
            return f"fail:{e}"
        time.sleep(8)
    return "fail:operation_timeout"

def start_vm() -> str:
    """VM 시작. 반환값: already_running / starting / ok / fail:...
    Spot은 ZONE_RESOURCE_POOL_EXHAUSTED 로 실패 가능 → 명시적으로 반환."""
    if not VM_NAME or not VM_ZONE:
        return "UNCONFIGURED"
    s = get_status()
    if s == "RUNNING":  return "already_running"
    if s == "STAGING":  return "starting"
    try:
        r = _req.post(f"{_base()}/start", headers=_hdr(), timeout=10)
        if r.status_code not in (200, 201):
            return f"fail:{r.status_code} {r.text[:200]}"
        # operation URL 확인 → 실제 리소스 할당 성공 여부 확인
        op_url = r.json().get("selfLink", "")
        if op_url:
            result = _wait_operation(op_url, timeout=120)
            return "ok" if result == "done" else result
        return "ok"  # selfLink 없으면 낙관적으로 처리
    except Exception as e:
        return f"fail:{e}"

def stop_vm() -> str:
    """VM 중지. 반환값: already_stopped / ok / fail:..."""
    if not VM_NAME or not VM_ZONE:
        return "UNCONFIGURED"
    s = get_status()
    if s == "TERMINATED": return "already_stopped"
    try:
        r = _req.post(f"{_base()}/stop", headers=_hdr(), timeout=10)
        return "ok" if r.status_code in (200, 201) else f"fail:{r.status_code}"
    except Exception as e:
        return f"fail:{e}"

def wait_for_status(target: str, timeout: int = 180) -> bool:
    """VM이 target 상태가 될 때까지 대기"""
    for _ in range(timeout // 10):
        if get_status() == target:
            return True
        time.sleep(10)
    return False

def upgrade_to_standard() -> str:
    """Spot → Standard 업그레이드 (VM 중지 후 실행)"""
    if not VM_NAME or not VM_ZONE:
        return "UNCONFIGURED"
    # Spot 인스턴스는 instanceTerminationAction 필드가 있어 별도 클리어 필요
    # 1단계: instanceTerminationAction 제거
    r1 = _req.post(f"{_base()}/setScheduling",
                   headers=_hdr(),
                   json={"instanceTerminationAction": "DELETE"},
                   timeout=10)
    # 2단계: provisioningModel → STANDARD
    body = {
        "onHostMaintenance": "MIGRATE",
        "automaticRestart": True,
        "preemptible": False,
        "provisioningModel": "STANDARD",
    }
    try:
        r = _req.post(f"{_base()}/setScheduling",
                      headers=_hdr(), json=body, timeout=10)
        if r.status_code in (200, 201):
            return "ok"
        # GCP가 기존 VM provisioningModel 변경 불가 시 안내
        err = r.json().get("error", {}).get("message", r.text[:200])
        return f"fail:{r.status_code} {err}"
    except Exception as e:
        return f"fail:{e}"


# ── CLI ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not VM_NAME or not VM_ZONE:
        print("ERROR: .env에 GCP_VM_NAME / GCP_VM_ZONE 미설정")
        print("  예) GCP_VM_NAME=omni-brain")
        print("      GCP_VM_ZONE=asia-northeast3-a")
        sys.exit(1)

    print(f"VM: {VM_NAME} @ {VM_ZONE}  (project: {PROJECT})")
    s = get_status()
    print(f"현재 상태: {s}")

    if "--upgrade" in sys.argv:
        print("\nSpot → Standard 업그레이드 시작")
        if s not in ("TERMINATED", "STOPPING"):
            print("  VM 중지 중...")
            stop_vm()
            if not wait_for_status("TERMINATED", 120):
                print("  VM 중지 타임아웃"); sys.exit(1)
        print("  setScheduling 요청...")
        result = upgrade_to_standard()
        print(f"  결과: {result}")
        if result == "ok":
            print("  VM 재시작 중...")
            start_vm()
            print("  완료! VM이 Standard로 변경됨")
    elif "--start" in sys.argv:
        print(start_vm())
    elif "--stop" in sys.argv:
        print(stop_vm())
