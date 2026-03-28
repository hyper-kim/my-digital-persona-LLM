import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
]


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    client_secret_path = project_dir / "client_secret.json"
    token_path = project_dir / "token.json"

    if not client_secret_path.exists():
        print("[오류] client_secret.json 파일이 없습니다.")
        print(f"       위치: {client_secret_path}")
        print("       GCP 콘솔에서 OAuth Desktop 앱 JSON을 내려받아 파일명을 client_secret.json으로 두세요.")
        return 1

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as e:
        print("[오류] Google OAuth 패키지가 설치되지 않았습니다.")
        print("       아래 명령으로 설치하세요:")
        print("       venv\\Scripts\\python.exe -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
        print(f"       상세: {e}")
        return 1

    creds = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    print("[완료] OAuth 토큰 발급/갱신 성공")
    print(f"       token.json 저장 위치: {token_path}")

    # 간단한 Drive API 호출 테스트
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds)
        about = service.about().get(fields="user(displayName,emailAddress)").execute()
        user = about.get("user", {})
        print(f"       인증 계정: {user.get('displayName', '')} <{user.get('emailAddress', '')}>")
    except Exception as e:
        print(f"[경고] Drive API 테스트 호출 실패: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
