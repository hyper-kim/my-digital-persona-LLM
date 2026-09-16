#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYU 포탈의 모든 탭 감지 및 크롤링 (GUI + 구조화된)."""

import json
import importlib
import time as _t
import pyautogui
import pyperclip
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import os

log_prefix = '[NYU-FULL]'

def main():
    print(f"{log_prefix} NYU 포탈 전체 크롤링 시작")
    
    # 라이브러리 로드
    m = importlib.import_module('7_unified_agent')
    portal_url = 'https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate'
    
    result = {
        'source_url': portal_url,
        'timestamp': datetime.now().isoformat(),
        'tabs': {},
        'all_tables': [],
        'navigation_elements': [],
    }
    
    try:
        # ─ Step 1: GUI 로그인
        print(f"{log_prefix} Step 1: GUI 모드로 포탈 접속 및 로그인")
        timeout_sec = int(os.getenv('CHROME_MANUAL_WAIT_SEC', '180'))
        
        initial_text = m.fetch_page_chrome(
            portal_url, 
            click_button='Login', 
            timeout=45
        )
        
        print(f"{log_prefix} ✓ 초기 포탈 텍스트: {len(initial_text)} chars")
        
        # ─ Step 2: 현재 페이지 HTML 분석
        print(f"{log_prefix} Step 2: 현재 포탈 HTML 분석")
        pyautogui.hotkey('ctrl', 'a')
        _t.sleep(0.2)
        pyautogui.hotkey('ctrl', 'c')
        _t.sleep(0.5)
        
        current_html_text = (pyperclip.paste() or '').strip()
        soup = BeautifulSoup(current_html_text, 'html.parser')
        
        # 탭 UI 요소 찾기
        tab_elements = []
        
        # 다양한 탭 요소 선택자
        selectors = [
            'nav a',
            '[role="tab"]',
            '.nav-tabs a',
            'ul.nav li a',
            '[data-tab]',
        ]
        
        for selector in selectors:
            elements = soup.select(selector)
            if len(elements) >= 2:  # 최소 2개 이상
                print(f"{log_prefix} {selector} 선택자로 탭 발견: {len(elements)}개")
                for elem in elements[:15]:
                    text_val = elem.get_text(strip=True)
                    href_val = elem.get('href', '')
                    data_tab = elem.get('data-tab', '')
                    
                    if text_val and len(text_val) > 1:
                        tab_elements.append({
                            'label': text_val,
                            'href': href_val,
                            'data_tab': data_tab,
                            'selector': selector,
                        })
                
                if tab_elements:
                    break
        
        print(f"{log_prefix} 감지된 탭: {len(tab_elements)}개")
        for i, tab in enumerate(tab_elements[:10], 1):
            print(f"  {i}. {tab['label']:30} | href={tab['href'][:40] if tab['href'] else 'N/A'}")
        
        result['detected_tabs'] = tab_elements[:15]
        
        # ─ Step 3: 각 탭 방문 및 크롤링
        print(f"{log_prefix} Step 3: 각 탭 방문 및 크롤링")
        visited_urls = set()
        tab_counter = 0
        
        for i, tab in enumerate(tab_elements[:8], 1):  # 최대 8개 탭
            tab_name = tab['label']
            tab_href = tab['href']
            
            # 탭 URL 구성
            if tab_href:
                if tab_href.startswith('/'):
                    tab_url = 'https://connect.nyu.edu' + tab_href
                elif tab_href.startswith('http'):
                    tab_url = tab_href
                elif tab_href.startswith('#'):
                    # JavaScript 탭 처리
                    tab_url = None
                else:
                    tab_url = urljoin('https://connect.nyu.edu/portal/undergraduate/', tab_href)
            else:
                tab_url = None
            
            # 중복 확인
            if tab_url and tab_url in visited_urls:
                print(f"{log_prefix} [{i}] {tab_name} (이미 방문)")
                continue
            
            try:
                if tab_url:
                    print(f"{log_prefix} [{i}] {tab_name} 로드: {tab_url[:60]}...")
                    # GUI 모드로 탭 페이지 접속
                    tab_text = m.fetch_page_chrome(tab_url, timeout=30)
                    visited_urls.add(tab_url)
                    
                    # BeautifulSoup로 파싱
                    try:
                        tab_soup = BeautifulSoup(tab_text[:10000], 'html.parser')
                    except:
                        tab_soup = BeautifulSoup(tab_text[:5000], 'lxml')
                    
                    # 테이블 추출
                    tables = []
                    for table in tab_soup.find_all('table')[:5]:
                        rows = []
                        for tr in table.find_all('tr')[:10]:
                            cols = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                            if cols:
                                rows.append(cols)
                        if rows:
                            tables.append({'rows': rows})
                    
                    # 주요 텍스트
                    paragraphs = []
                    for p in tab_soup.find_all('p')[:5]:
                        text = p.get_text(strip=True)
                        if text and len(text) > 30:
                            paragraphs.append(text[:300])
                    
                    # 헤딩
                    title = ''
                    for h in tab_soup.find_all(['h1', 'h2', 'h3']):
                        title = h.get_text(strip=True)
                        if title:
                            break
                    
                    result['tabs'][tab_name] = {
                        'url': tab_url,
                        'title': title or tab_name,
                        'text_length': len(tab_text),
                        'table_count': len(tables),
                        'tables': tables,
                        'paragraphs': paragraphs[:3],
                    }
                    
                    result['all_tables'].extend(tables)
                    tab_counter += 1
                    print(f"{log_prefix} ✓ {tab_name}: {len(tables)} 테이블, {len(tab_text)} chars")
                    _t.sleep(1.5)
                else:
                    print(f"{log_prefix} [{i}] {tab_name} (JavaScript 탭 - 스킵)")
            
            except Exception as e:
                print(f"{log_prefix} [{i}] {tab_name} 실패: {e}")
                _t.sleep(1)
                continue
        
        # ─ Summary
        result['summary'] = {
            'total_tabs_detected': len(tab_elements),
            'tabs_successfully_crawled': tab_counter,
            'total_tables_extracted': len(result['all_tables']),
            'unique_urls_visited': len(visited_urls),
        }
        
        print(f"{log_prefix} 완료:")
        print(f"  - 감지된 탭: {len(tab_elements)}")
        print(f"  - 크롤링된 탭: {tab_counter}")
        print(f"  - 추출된 테이블: {len(result['all_tables'])}")
    
    except Exception as e:
        print(f"{log_prefix} 오류: {e}")
        result['error'] = str(e)
    
    # ─ JSON 저장
    out_dir = Path('_nyu_crawl_results')
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"nyu_full_tabs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{log_prefix} 저장: {out_file}")

if __name__ == '__main__':
    main()
