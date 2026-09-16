#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nyu_portal_structured_crawl.py — NYU 포탈 구조화된 크롤링

사용법:
  1) 터미널 실행: python nyu_portal_structured_crawl.py
  2) Chrome 창 로그인 (NetID + 비밀번호 + Login 클릭)
  3) 콘솔에서 "시작" 입력 후 Enter
  4) 자동으로 NYU 포탈 모든 페이지 탐색 + 데이터 추출
  5) 결과를 JSON 파일로 저장
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=False)

# 임포트
try:
    import importlib
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError as e:
    print(f"❌ 필수 패키지 미설치: {e}")
    sys.exit(1)

# 로컬 모듈
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

try:
    unified_agent = importlib.import_module("7_unified_agent")
except Exception as e:
    print(f"❌ 7_unified_agent 임포트 실패: {e}")
    sys.exit(1)

# 결과 저장 경로
RESULTS_DIR = PROJECT_DIR / "_nyu_crawl_results"
RESULTS_DIR.mkdir(exist_ok=True)


def _get_chrome_binary_path() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    return ""


def _click_login_button(driver) -> bool:
    """저장된 비밀번호가 자동입력된 상태에서 Login/Sign in 버튼 클릭 시도."""
    from selenium.webdriver.common.by import By
    labels = ["login", "sign in", "log in", "continue"]
    for label in labels:
        try:
            xpath = (
                "//*[contains(translate(normalize-space(text()),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                + repr(label) + ")]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.tag_name.lower() in ("a", "button", "input"):
                    el.click()
                    return True
        except Exception:
            continue
    return False


def main():
    """메인 엔트리 포인트."""
    print("\n" + "=" * 80)
    print("🎓 NYU 포탈 구조화된 크롤링 (자동 메뉴 탐색)")
    print("=" * 80)
    
    print("\n[Step 1] Chrome 창 열기...")
    
    # Chrome 옵션 (정식 Chrome + 사용자 프로필 + 창 유지)
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    opts.add_experimental_option("detach", True)  # 스크립트 종료 후 창 유지
    chrome_bin = _get_chrome_binary_path()
    if chrome_bin:
        opts.binary_location = chrome_bin

    # 저장된 비밀번호/세션을 그대로 쓰기 위해 기본 사용자 프로필 사용
    user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    if Path(user_data).is_dir():
        opts.add_argument(f"--user-data-dir={user_data}")
        opts.add_argument("--profile-directory=Default")

    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    
    driver = None
    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
        driver.set_page_load_timeout(30)
        
        # NYU 포탈 로드 (요청하신 링크)
        nyu_url = "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate"
        print(f"\n[Step 2] NYU 포탈 로드: {nyu_url}")
        driver.get(nyu_url)
        time.sleep(2)
        
        # 사용자 로그인 대기
        print("\n" + "=" * 80)
        print("🔐 로그인 대기 중...")
        print("=" * 80)
        print("Chrome 창에서 다음을 진행하세요:")
        print("  1) NetID 입력 (예: abc1234)")
        print("  2) 비밀번호 입력")
        print("  3) 첫 1회는 'Login' 클릭 후 비밀번호 저장")
        print("  4) 다음부터는 저장된 비밀번호 자동입력 + 버튼 자동클릭")
        print("  4) 로그인 완료 후 이 콘솔로 돌아와서 아래 메시지를 따르세요")
        print("=" * 80 + "\n")

        # 자동 로그인 시도: 저장된 비밀번호가 자동입력돼 있으면 버튼 자동 클릭
        auto_login = os.getenv("NYU_AUTO_LOGIN", "true").lower() == "true"
        if auto_login:
            clicked = _click_login_button(driver)
            if clicked:
                print("🤖 자동 로그인 시도: Login 버튼 클릭 완료")
                time.sleep(2)
        
        # 사용자 입력 대기
        start_cmd = input("👉 로그인 완료 후 'y' 또는 'yes'를 입력하세요: ").strip().lower()
        
        if start_cmd not in ["y", "yes"]:
            print("❌ 사용자 취소")
            return
        
        time.sleep(1)
        
        # ─ 로그인 완료 감지 ────────────────────────────────────────────────
        print("\n[Step 2.5] 로그인 완료 대기 중... (최대 60초)")
        login_complete = False
        for attempt in range(60):
            try:
                current_url = driver.current_url
                host = (urlparse(current_url).hostname or "").lower()
                # 로그인 완료 감지: nyu.edu 하위 도메인 + 로그인 엔드포인트 탈출
                if host.endswith(".nyu.edu") and "/manage/login" not in current_url and "/account/login" not in current_url:
                    login_complete = True
                    print(f"✅ 로그인 완료 감지! URL: {current_url}")
                    time.sleep(2)  # 페이지 로드 완료 대기
                    break
                else:
                    if (attempt + 1) % 5 == 0:
                        print(f"  [{attempt+1}/60] 현재 URL: {current_url}")
                    time.sleep(1)
            except Exception:
                pass
        
        if not login_complete:
            print("⚠️ 로그인 완료가 감지되지 않았습니다. 진행합니다...")
        
        time.sleep(1)
        
        # ─ 크롤링 시작 ────────────────────────────────────────────────────
        print("\n[Step 3] 구조화된 크롤링 시작...")
        print("=" * 80)
        
        # 2개 이상의 Chrome 탭/창이 활성화되지 않도록 하기 위해
        # 그냥 현재 드라이버로 진행
        
        crawl_data = unified_agent.crawl_nyu_portal_structured(driver, timeout=30)
        
        print("=" * 80)
        print("✅ 크롤링 완료!")
        print("=" * 80)
        
        # ─ 결과 저장 ──────────────────────────────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_json = RESULTS_DIR / f"nyu_crawl_{timestamp}.json"
        
        with open(result_json, "w", encoding="utf-8") as f:
            json.dump(crawl_data, f, ensure_ascii=False, indent=2)
        
        print(f"\n✅ 결과 저장됨: {result_json}")
        print(f"\n📊 수집 통계:")
        print(f"   - 페이지 수: {crawl_data['transfer_info']['total_pages']}")
        print(f"   - 테이블 수: {crawl_data['transfer_info']['total_tables']}")
        print(f"   - 방문 링크: {crawl_data['transfer_info']['total_links_visited']}")
        print(f"   - 데이터 소스: {', '.join(crawl_data['transfer_info']['data_sources'][:3])}")
        
        # JSON 미리보기
        print(f"\n📄 결과 미리보기 (JSON 구조):")
        print("=" * 80)
        preview_json = {
            "title": crawl_data.get("title", ""),
            "pages": len(crawl_data.get("pages", [])),
            "tables": len(crawl_data.get("tables", [])),
            "first_page": crawl_data.get("pages", [{}])[0].get("title", "N/A") if crawl_data.get("pages") else "N/A",
        }
        print(json.dumps(preview_json, ensure_ascii=False, indent=2))
        print("=" * 80)
        
        print(f"\n💾 전체 결과는 JSON 파일로 저장되었습니다:")
        print(f"   {result_json}")
        
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        keep_open = os.getenv("KEEP_CHROME_OPEN", "true").lower() == "true"
        if driver and not keep_open:
            try:
                print("\n[Cleanup] Chrome 창 종료...")
                driver.quit()
            except Exception:
                pass
        elif keep_open:
            print("\n[Hold] KEEP_CHROME_OPEN=true 이므로 Chrome 창을 닫지 않고 유지합니다.")


if __name__ == "__main__":
    main()
