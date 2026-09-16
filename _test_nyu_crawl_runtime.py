import os
import importlib

os.environ.setdefault("CHROME_MANUAL_WAIT_SEC", "15")

u = importlib.import_module("7_unified_agent")

print("[TEST] fetch_page_chrome direct")
url = "https://connect.nyu.edu/portal/undergraduate/tab=welcome"
text = u.fetch_page_chrome(url, click_button="Login", timeout=30)
print("[RESULT] direct_len=", len(text))
print("[RESULT] direct_head=")
print(text[:400].replace("\n", " "))
print("=" * 60)

print("[TEST] fetch_university_pages('nyu')")
sections = u.fetch_university_pages("nyu", query="transfer requirements deadline", official_only=False)
print("[RESULT] sections_count=", len(sections))
for i, s in enumerate(sections[:5], 1):
    src = s.get("source", "")
    body = (s.get("text", "") or "")
    print(f"  [{i}] source={src} len={len(body)} url={s.get('url','')}")
    print("      head:", body[:180].replace("\n", " "))
