import sqlite3, os

# --- Qdrant에서 한성과고 PDF 소스 목록 확인 ---
QDRANT = "http://localhost:6333"
COLLECTION = "documents"  # 실제 컬렉션명 확인

# 1. 컬렉션 목록
try:
    r = requests.get(f"{QDRANT}/collections", timeout=5)
    cols = r.json().get("result", {}).get("collections", [])
    print("=== Qdrant 컬렉션 ===")
    for c in cols:
        print(f"  {c['name']}")
    if cols:
        COLLECTION = cols[0]["name"]
    print()
except Exception as e:
    print(f"Qdrant 연결 실패: {e}")
    exit(1)

# 2. 컬렉션 포인트 수
r2 = requests.get(f"{QDRANT}/collections/{COLLECTION}", timeout=5)
info = r2.json().get("result", {})
print(f"컬렉션: {COLLECTION}")
print(f"총 포인트: {info.get('points_count', '?')}")
print()

# 3. 한성과고 PDF 소스가 있는지 Scroll로 확인
payload = {
    "filter": {
        "must": [{"key": "source", "match": {"text": "한성과고"}}]
    },
    "with_payload": ["source"],
    "limit": 5
}
r3 = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll",
                   json=payload, timeout=10)
pts = r3.json().get("result", {}).get("points", [])
print(f"=== Qdrant '한성과고' 포함 포인트 샘플 ({len(pts)}개) ===")
for p in pts:
    print(f"  {p.get('payload', {}).get('source', '')[:100]}")

