from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
creds = Credentials.from_authorized_user_file("token.json", SCOPES)
svc = build("drive", "v3", credentials=creds)

resp = svc.files().list(
    q="'root' in parents and trashed=false",
    fields="files(id,name,mimeType)",
    pageSize=10,
).execute()

files = resp.get("files", [])
print("root items:", len(files))
for f in files:
    print("-", f.get("name", ""), "|", f.get("mimeType", ""))
