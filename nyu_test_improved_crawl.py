#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYU 포탈 개선된 탭 크롤링."""

import json
import time as _t
from pathlib import Path
from datetime import datetime
import importlib

log_prefix = '[NYU-IMPROVED]'

def main():
    print(f"{log_prefix} 개선된 크롤링 함수 테스트")
    
    #라이브러리 로드
    m = importlib.import_module('7_unified_agent')
    print(f"{log_prefix} 포탈 접속 및 로그인...")
    
    text = m.fetch_page_chrome(
        'https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate',
        click_button='Login',
        timeout=45
    )
    
    print(f"{log_prefix} ✓ 로그인 완료: {len(text)} chars")
    
    # 알려진 탭들 크롤링
    known_tabs = [
        'welcome', 'application-status', 'financial-aid', 'decisions',
        'important-dates', 'requirements', 'documents', 'financial-aid-status'
    ]
    
    print(f"{log_prefix} 알려진 탭 크롤링 시작...")
    crawled = {}
    
    for i, tab_name in enumerate(known_tabs[:8], 1):
        url = f'https://connect.nyu.edu/portal/undergraduate?tab={tab_name}'
        
        try:
            print(f"  [{i}] {tab_name} 로드...")
            tab_text = m.fetch_page_chrome(url, timeout=20)
            crawled[tab_name] = {
                'length': len(tab_text),
                'snippet': tab_text[:300] if tab_text else '',
            }
            print(f"    ✓ 수집: {len(tab_text)} chars")
            _t.sleep(1)
        except Exception as e:
            print(f"    ✗ 실패: {e}")
            _t.sleep(1)
    
    # 결과 저장
    result = {
        'timestamp': datetime.now().isoformat(),
        'tabs_crawled': crawled,
        'total_tabs': len(crawled),
        'successful_tabs': sum(1 for v in crawled.values() if v.get('length', 0) > 100),
    }
    
    out_dir = Path('_nyu_crawl_results')
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"nyu_tabs_improved_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    
    print(f"\n{log_prefix} 완료:")
    print(f"  총 시도: {len(known_tabs)}")
    print(f"  성공: {result['successful_tabs']}")
    print(f"  파일: {out_file}")

if __name__ == '__main__':
    main()
