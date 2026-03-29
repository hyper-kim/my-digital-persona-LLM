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

def create_vm(provisioning_model: str = "STANDARD", disk_gb: int = 100) -> dict:
    """VM 인스턴스를 새로 생성한다. 디스크 없음 → Ubuntu 22.04 + 스타트업 스크립트.
    반환: {"status": "ok"/"fail:...", "ip": "외부IP 또는 ''"} """
    if not VM_NAME or not VM_ZONE:
        return {"status": "UNCONFIGURED", "ip": ""}

    # 이미 있으면 스킵
    s = get_status()
    if s not in ("ERROR:404", "UNCONFIGURED", "UNKNOWN"):
        return {"status": f"already_exists:{s}", "ip": get_external_ip()}

    # SSH 공개키 로드
    _here = os.path.dirname(os.path.abspath(__file__))
    ssh_key_path = os.getenv("SSH_KEY_PATH",
                             r"C:\Users\kjy\.ssh\gcp_key_fixed")
    pub_key_path = ssh_key_path + ".pub"
    try:
        with open(pub_key_path, "r") as f:
            pub_key = f.read().strip()
    except Exception:
        return {"status": "fail:pub_key_not_found", "ip": ""}
    ssh_user = os.getenv("CLOUD_VM_USER", "kjy")
    ssh_meta = f"{ssh_user}:{pub_key}"  # GCP metadata 형식

    # 스타트업 스크립트 (vm_setup.sh)
    setup_sh = os.path.join(_here, "vm_setup.sh")
    try:
        with open(setup_sh, "r", encoding="utf-8") as f:
            startup_script = f.read()
    except Exception:
        startup_script = "#!/bin/bash\necho 'vm_setup.sh not found'"

    machine_url = (
        f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"
        f"/zones/{VM_ZONE}/machineTypes/g2-standard-32"
    )
    image_url = (
        "https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud"
        "/global/images/family/ubuntu-2204-lts"
    )
    body = {
        "name": VM_NAME,
        "machineType": machine_url,
        "scheduling": {
            "onHostMaintenance": "TERMINATE",
            "automaticRestart": False,
            "provisioningModel": provisioning_model,   # "STANDARD" or "SPOT"
            **({"instanceTerminationAction": "DELETE"} if provisioning_model == "SPOT" else {}),
        },
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {
                "sourceImage": image_url,
                "diskSizeGb": str(disk_gb),
                "diskType": (
                    f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"
                    f"/zones/{VM_ZONE}/diskTypes/pd-balanced"
                ),
            },
        }],
        "networkInterfaces": [{
            "network": f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/networks/default",
            "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
        }],
        "tags": {"items": ["http-server", "https-server"]},
        "metadata": {
            "items": [
                {"key": "ssh-keys",        "value": ssh_meta},
                {"key": "startup-script",  "value": startup_script},
            ]
        },
        "serviceAccounts": [],   # SA 없음 (mydigital-persona-embedder에 serviceAccountUser 권한 없음)
    }

    try:
        url = (f"https://compute.googleapis.com/compute/v1"
               f"/projects/{PROJECT}/zones/{VM_ZONE}/instances")
        r = _req.post(url, headers=_hdr(), json=body, timeout=30)
        if r.status_code not in (200, 201):
            return {"status": f"fail:{r.status_code} {r.text[:300]}", "ip": ""}
        op_url = r.json().get("selfLink", "")
        result = _wait_operation(op_url, timeout=180) if op_url else "ok"
        if result not in ("done", "ok"):
            return {"status": result, "ip": ""}
        # IP 확인 (VM 기동 후 할당)
        for _ in range(12):
            time.sleep(10)
            ip = get_external_ip()
            if ip:
                return {"status": "ok", "ip": ip}
        return {"status": "ok_no_ip", "ip": ""}
    except Exception as e:
        return {"status": f"fail:{e}", "ip": ""}


def get_external_ip() -> str:
    """현재 VM의 외부 IP 반환. 없으면 ''"""
    if not VM_NAME or not VM_ZONE:
        return ""
    try:
        r = _req.get(_base(), headers=_hdr(), timeout=10)
        if r.status_code != 200:
            return ""
        ifaces = r.json().get("networkInterfaces", [])
        for iface in ifaces:
            for ac in iface.get("accessConfigs", []):
                ip = ac.get("natIP", "")
                if ip:
                    return ip
    except Exception:
        pass
    return ""


def update_env_ip(new_ip: str, env_path: str = None) -> bool:
    """`.env`의 CLOUD_VM_IP 와 CLOUD_OLLAMA_URL을 새 IP로 업데이트."""
    if not new_ip:
        return False
    _here = os.path.dirname(os.path.abspath(__file__))
    env_path = env_path or os.path.join(_here, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        import re
        # CLOUD_VM_IP=...
        content = re.sub(r"(?m)^CLOUD_VM_IP=.*$", f"CLOUD_VM_IP={new_ip}", content)
        # CLOUD_OLLAMA_URL=http://기존IP:11434
        content = re.sub(
            r"(?m)^CLOUD_OLLAMA_URL=http://[\d.]+:(\d+)",
            f"CLOUD_OLLAMA_URL=http://{new_ip}:\\1",
            content,
        )
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logging.error(f"[ENV] IP 업데이트 실패: {e}")
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
