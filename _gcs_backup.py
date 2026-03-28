"""
_gcs_backup.py
==============
processed_files.db + email_log.db + kakao_log.db → GCS 버킷 자동 백업

필요 .env 설정:
  GCS_BACKUP_BUCKET=my-digital-persona-backup   # 버킷 이름 (gs:// 제외)
  GOOGLE_SA_KEY_PATH=...                         # 기존 SA 키 그대로 사용

사용:
  python _gcs_backup.py            # 즉시 백업
  python _gcs_backup.py --restore  # 최신 백업 → 로컬 복원 (주의: 덮어씀)
"""
import os, sys, json, time, logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gcs_backup")

PROJECT_DIR = os.getenv("PROJECT_DIR", os.path.dirname(os.path.abspath(__file__)))

# .env 로드
_env_file = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8", errors="ignore") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

SA_KEY  = os.getenv("GOOGLE_SA_KEY_PATH", "")
BUCKET  = os.getenv("GCS_BACKUP_BUCKET", "")
PREFIX  = os.getenv("GCS_BACKUP_PREFIX", "db-backup")

# 백업 대상 파일 목록 (존재하는 것만)
_BACKUP_FILES = [
    os.getenv("STATE_DB_PATH",  os.path.join(PROJECT_DIR, "processed_files.db")),
    os.path.join(PROJECT_DIR, "email_log.db"),
    os.path.join(PROJECT_DIR, "kakao_log.db"),
]


def _get_token() -> str:
    """SA 키로 GCS 액세스 토큰 획득."""
    try:
        import google.auth.transport.requests
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            SA_KEY,
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as e:
        log.error(f"토큰 획득 실패: {e}")
        raise


def _ensure_bucket(token: str) -> bool:
    """버킷이 없으면 생성. SA의 project 정보 활용."""
    import requests
    try:
        with open(SA_KEY) as f:
            sa_data = json.load(f)
        project_id = sa_data.get("project_id", "")
    except Exception:
        project_id = os.getenv("GCP_PROJECT_ID", "")

    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code == 200:
        return True  # 이미 존재
    if r.status_code == 404 and project_id:
        # 버킷 생성 시도
        r2 = requests.post(
            f"https://storage.googleapis.com/storage/v1/b?project={project_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": BUCKET, "storageClass": "STANDARD",
                  "location": "US",
                  "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}}},
            timeout=10,
        )
        if r2.status_code in (200, 201):
            log.info(f"버킷 생성됨: gs://{BUCKET}")
            return True
        log.error(f"버킷 생성 실패: {r2.status_code} {r2.text[:200]}")
    return False


def backup(quiet: bool = False) -> dict:
    """DB 파일들을 GCS에 업로드. 타임스탬프 + latest 두 벌 저장.
    반환: {파일명: "ok"/"skip"/"fail:..."}"""
    if not SA_KEY or not BUCKET:
        log.warning("GCS_BACKUP_BUCKET 또는 GOOGLE_SA_KEY_PATH 미설정 → 백업 건너뜀")
        return {}
    try:
        import requests
    except ImportError:
        log.error("requests 미설치")
        return {}

    try:
        token = _get_token()
    except Exception:
        return {}

    _ensure_bucket(token)

    results = {}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    for local_path in _BACKUP_FILES:
        if not os.path.exists(local_path):
            continue
        fname = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            data = f.read()

        for gcs_name in [f"{PREFIX}/{ts}/{fname}", f"{PREFIX}/latest/{fname}"]:
            url = (f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o"
                   f"?uploadType=media&name={gcs_name}")
            try:
                r = requests.post(
                    url, headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/octet-stream"},
                    data=data, timeout=60,
                )
                if r.status_code not in (200, 201):
                    results[fname] = f"fail:{r.status_code}"
                    if not quiet:
                        log.error(f"업로드 실패 {fname}: {r.status_code} {r.text[:100]}")
                    break
            except Exception as e:
                results[fname] = f"fail:{e}"
                break
        else:
            results[fname] = "ok"
            sz = len(data) / 1024
            if not quiet:
                log.info(f"백업 완료: {fname} ({sz:.0f} KB) → gs://{BUCKET}/{PREFIX}/{ts}/")

    return results


def restore() -> None:
    """GCS latest/ → 로컬 복원 (기존 파일 덮어씀, 주의)."""
    if not SA_KEY or not BUCKET:
        log.error("GCS_BACKUP_BUCKET 미설정")
        return
    try:
        import requests
    except ImportError:
        log.error("requests 미설치")
        return

    token = _get_token()
    for local_path in _BACKUP_FILES:
        fname = os.path.basename(local_path)
        gcs_name = f"{PREFIX}/latest/{fname}"
        url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{gcs_name.replace('/', '%2F')}?alt=media"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if r.status_code == 200:
            with open(local_path, "wb") as f:
                f.write(r.content)
            log.info(f"복원 완료: {local_path} ({len(r.content)/1024:.0f} KB)")
        elif r.status_code == 404:
            log.warning(f"GCS에 없음: {gcs_name}")
        else:
            log.error(f"복원 실패 {fname}: {r.status_code}")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        if not BUCKET:
            print("ERROR: .env에 GCS_BACKUP_BUCKET 설정 필요")
            sys.exit(1)
        print(f"⬇  GCS → 로컬 복원: gs://{BUCKET}/{PREFIX}/latest/")
        restore()
    else:
        if not BUCKET:
            print("ERROR: .env에 GCS_BACKUP_BUCKET 설정 필요")
            print("  예) GCS_BACKUP_BUCKET=my-digital-persona-backup")
            sys.exit(1)
        print(f"⬆  로컬 DB → GCS 백업: gs://{BUCKET}/{PREFIX}/")
        results = backup()
        for k, v in results.items():
            icon = "✅" if v == "ok" else "❌"
            print(f"  {icon} {k}: {v}")
