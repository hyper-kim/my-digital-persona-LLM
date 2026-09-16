#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYU 포탈 모든 탭 자동 크롤링 (multi-tab + 스트럭처 JSON 출력)."""

import json
import os
import sys
import time as _t
import logging as _log
import base64
from pathlib import Path
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from bs4 import BeautifulSoup
import pyautogui
import pyperclip

_log.basicConfig(level=_log.INFO, format="[%(asctime)s] %(message)s")
log = _log.getLogger(__name__)

CHROME_USER_DATA = r"C:\Users\kjy\AppData\Local\Google\Chrome\User Data"
_t.sleep(0)  # Avoid collision

def fetch_login_state(timeout_sec=120):
    """수동 또는 자동 로그인 대기."""
    log.info("[LOGIN] 로그인 상태 확인: %d초 대기", timeout_sec)
    
    # PyAutoGUI 방식: Ctrl+L → URL 입력 → 엔터
    try:
        pyautogui.hotkey("ctrl", "l")
        _t.sleep(0.3)
        pyautogui.typewrite([],  interval=0.01)
        _t.sleep(0.2)
        pyautogui.hotkey("enter")
        _t.sleep(0.5)
        for _ in range(timeout_sec // 2):
            _t.sleep(2)
            try:
                pyautogui.hotkey("ctrl", "a")
                _t.sleep(0.1)
                pyautogui.hotkey("ctrl", "c")
                _t.sleep(0.1)
                text = pyperclip.paste() or ""
                if "juyoung" in text.lower() or "application" in text.lower():
                    log.info("[LOGIN] ✓ 로그인 성공 감지")
                    return True
            except:
                pass
    except Exception as e:
        log.warning("[LOGIN] PyAutoGUI 실패: %s", e)
    return False

def crawl_nyu_all_tabs(headless=False, timeout=45) -> dict:
    """NYU 포탈 모든 탭 자동 크롤링."""
    
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    
    result = {
        "source_url": "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate",
        "title": "NYU Undergraduate Portal - Full Crawl",
        "timestamp": datetime.now().isoformat(),
        "tabs": {},  # tab_name -> {content}
        "all_tables": [],
        "navigation_links": [],
        "tab_urls": {},
    }
    
    try:
        # ─ Step 1: 포탈로 이동
        portal_url = "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate"
        log.info("[CRAWL] 포탈 접속: %s", portal_url)
        driver.get(portal_url)
        
        # ─ Step 2: 로그인 대기 (타임아웃)
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_all_elements_located((By.TAG_NAME, "body"))
            )
        except:
            pass
        _t.sleep(3)
        
        # 포탈이 뜨지 않으면 수동 대기
        if "connect.nyu.edu" not in driver.current_url:
            log.info("[CRAWL] 포탈 로드 대기 (수동 로그인 가능)...")
            fetch_login_state()
        
        _t.sleep(5)
        
        # ─ Step 3: 탭 찾기 및 크롤링
        soup = BeautifulSoup(driver.page_source, "lxml")
        
        # HTML에서 탭 요소 찾기 (일반적인 NYU 포탈 구조)
        tab_buttons = []
        
        # 탭 버튼 찾기 (다양한 선택자)
        selectors = [
            "nav li a",  # 네비게이션 탭
            "[role='tab']",  # ARIA 탭
            ".tab",  # 클래스 이름
            "ul li a",  # 리스트 탭
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    log.info("[CRAWL] 탭 요소 발견 (%s): %d개", selector, len(elements))
                    for elem in elements[:15]:  # 최대 15개
                        text = elem.text.strip()
                        href = elem.get_attribute("href") or ""
                        if text and (href or elem.get_attribute("data-tab")):
                            tab_buttons.append({
                                "text": text,
                                "href": href,
                                "elem": elem,
                                "selector": selector,
                            })
                    break
            except Exception as e:
                log.debug("[CRAWL] 선택자 %s 실패: %s", selector, e)
        
        if not tab_buttons:
            log.warning("[CRAWL] 탭 요소가 명확하지 않음. 현재 페이지만 크롤링.")
            tab_buttons = [{"text": "Current Page", "href": "", "elem": None, "selector": "manual"}]
        
        # ─ Step 4: 각 탭 방문 및 데이터 추출
        visited_urls = set()
        for i, tab_info in enumerate(tab_buttons):
            tab_name = tab_info["text"] or f"Tab {i+1}"
            tab_href = tab_info["href"]
            
            try:
                if tab_href and not tab_href.startswith("#"):
                    # URL이 있으면 GET
                    if tab_href.startswith("/"):
                        full_url = "https://connect.nyu.edu" + tab_href
                    else:
                        full_url = tab_href
                    
                    if full_url in visited_urls:
                        log.info("[CRAWL] [%d/%d] %s (이미 방문)", i+1, len(tab_buttons), tab_name)
                        continue
                    
                    log.info("[CRAWL] [%d/%d] %s 로드: %s", i+1, len(tab_buttons), tab_name, full_url)
                    driver.get(full_url)
                    visited_urls.add(full_url)
                    result["tab_urls"][tab_name] = full_url
                else:
                    # 버튼 클릭 (JavaScript 탭)
                    log.info("[CRAWL] [%d/%d] %s (클릭 탭)", i+1, len(tab_buttons), tab_name)
                    try:
                        tab_info["elem"].click()
                        _t.sleep(2)
                        current_url = driver.current_url
                        if current_url not in visited_urls:
                            visited_urls.add(current_url)
                            result["tab_urls"][tab_name] = current_url
                    except:
                        log.debug("[CRAWL] 탭 클릭 실패: %s", tab_name)
                
                _t.sleep(2)
                
                # ─ 현재 페이지 콘텐츠 추출
                soup = BeautifulSoup(driver.page_source, "lxml")
                
                # 제목
                title = soup.find("h1") or soup.find("h2")
                title_text = title.get_text(strip=True) if title else tab_name
                
                # 테이블
                tables = []
                for table in soup.find_all("table"):
                    rows = []
                    for tr in table.find_all("tr"):
                        cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                        if cols:
                            rows.append(cols)
                    if rows:
                        tables.append({
                            "title": title_text,
                            "row_count": len(rows),
                            "rows": rows[:50],  # 최대 50행
                        })
                
                # 주요 텍스트 블록
                paragraphs = []
                for p in soup.find_all("p"):
                    text = p.get_text(strip=True)
                    if text and len(text) > 30:
                        paragraphs.append(text[:500])
                
                # 리스트 항목
                list_items = []
                for li in soup.find_all("li"):
                    text = li.get_text(strip=True)
                    if text and len(text) > 5:
                        list_items.append(text[:200])
                
                # 내부 링크
                links = []
                for a in soup.find_all("a"):
                    href = a.get("href", "").strip()
                    text = a.get_text(strip=True)
                    if href and text and len(text) > 2:
                        if href.startswith("/") or "nyu.edu" in href:
                            links.append({"text": text, "url": href})
                
                tab_data = {
                    "title": title_text,
                    "url": driver.current_url,
                    "html_length": len(driver.page_source),
                    "text_length": len(soup.get_text()),
                    "table_count": len(tables),
                    "tables": tables,
                    "paragraph_count": len(paragraphs),
                    "paragraphs": paragraphs[:10],
                    "list_items": list_items[:20],
                    "links": links[:10],
                }
                
                result["tabs"][tab_name] = tab_data
                result["all_tables"].extend(tables)
                result["navigation_links"].extend(links)
                
                log.info("[CRAWL] ✓ %s 저장 (%d tables, %d chars)",
                         tab_name, len(tables), tab_data["text_length"])
            
            except Exception as e:
                log.warning("[CRAWL] [%d/%d] %s 실패: %s", i+1, len(tab_buttons), tab_name, e)
                continue
        
        # ─ Step 5: 메타정보
        result["summary"] = {
            "total_tabs_crawled": len(result["tabs"]),
            "total_tables_found": len(result["all_tables"]),
            "total_links_found": len(result["navigation_links"]),
            "unique_urls_visited": list(visited_urls)[:20],
        }
        
        log.info("[CRAWL] 완료: %d 탭, %d 테이블 추출", 
                 len(result["tabs"]), len(result["all_tables"]))
    
    except Exception as e:
        log.error("[CRAWL] 치명적 오류: %s", e)
        result["error"] = str(e)
    
    finally:
        try:
            driver.quit()
        except:
            pass
    
    return result

if __name__ == "__main__":
    log.info("=== NYU 포탈 전체 탭 크롤링 시작 ===")
    
    # timeout 환경변수에서 읽기 (기본 45초)
    timeout = int(os.getenv("CHROME_MANUAL_WAIT_SEC", "45"))
    
    # headless 모드 확인
    headless = os.getenv("USE_GUI_AUTOMATION", "false").lower() != "true"
    
    result = crawl_nyu_all_tabs(headless=headless, timeout=timeout)
    
    # JSON 저장
    output_dir = Path("_nyu_crawl_results")
    output_dir.mkdir(exist_ok=True)
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"nyu_full_tabs_{ts}.json"
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    log.info("[OUTPUT] 저장: %s (%d bytes)", output_file, output_file.stat().st_size)
    
    # 요약 출력
    print("\n" + "="*60)
    print(f"✓ 크롤링 완료: {result['summary']['total_tabs_crawled']}개 탭")
    print(f"✓ 테이블: {result['summary']['total_tables_found']}개")
    print(f"✓ 파일: {output_file}")
    print("="*60)
