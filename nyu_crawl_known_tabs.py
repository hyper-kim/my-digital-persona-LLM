#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NYU 포탈 탭 URL 패턴 자동 발견 및 크롤링."""

import json
import importlib
import time as _t
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin
import os

log_prefix = '[NYU-TABS]'

# 알려진 NYU 포탈 탭 패턴
# 학교에 따라 다를 수 있으므로 여러 패턴 시도
KNOWN_TABS = [
    ('Welcome', 'https://connect.nyu.edu/portal/undergraduate?tab=welcome'),
    ('Application Status', 'https://connect.nyu.edu/portal/undergraduate?tab=application-status'),
    ('Financial Aid', 'https://connect.nyu.edu/portal/undergraduate?tab=financial-aid'),
    ('Decisions', 'https://connect.nyu.edu/portal/undergraduate?tab=decisions'),
    ('Important Dates', 'https://connect.nyu.edu/portal/undergraduate?tab=important-dates'),
    ('Program Requirements', 'https://connect.nyu.edu/portal/undergraduate?tab=requirements'),
    ('My Documents', 'https://connect.nyu.edu/portal/undergraduate?tab=documents'),
]

def main():
    print(f"{log_prefix} NYU 포탈 탭 크롤링 시작")
    
    m = importlib.import_module('7_unified_agent')
    
    result = {
        'source_url': 'https://connect.nyu.edu/portal/undergraduate',
        'timestamp': datetime.now().isoformat(),
        'tabs_crawled': {},
        'attempted_tabs': [],
        'successful_tabs': 0,
    }
    
    try:
        # 먼저 기본 welcome 페이지로 로그인
        print(f"{log_prefix} 로그인...")
        welcome_url = 'https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate'
        initial_text = m.fetch_page_chrome(welcome_url, click_button='Login', timeout=45)
        print(f"{log_prefix} ✓ 로그인 완료: {len(initial_text)} chars")
        
        # 각 알려진 탭 시도
        print(f"{log_prefix} 탭 크롤링 시작...")
        for tab_name, tab_url in KNOWN_TABS:
            result['attempted_tabs'].append(tab_name)
            
            try:
                print(f"{log_prefix} [{len(result['tabs_crawled'])+1}] {tab_name}: {tab_url[-40:]}")
                
                # GUI 모드로 탭 페이지 접속
                tab_text = m.fetch_page_chrome(tab_url, timeout=30)
                
                if tab_text and len(tab_text) > 100:
                    result['tabs_crawled'][tab_name] = {
                        'url': tab_url,
                        'text_length': len(tab_text),
                        'snippet': tab_text[:500],
                        'status': 'success',
                    }
                    result['successful_tabs'] += 1
                    print(f"{log_prefix} ✓ {tab_name}: {len(tab_text)} chars")
                else:
                    result['tabs_crawled'][tab_name] = {
                        'url': tab_url,
                        'text_length': len(tab_text) if tab_text else 0,
                        'status': 'empty',
                    }
                    print(f"{log_prefix} ✗ {tab_name}: 빈 응답")
                
                _t.sleep(1)
            
            except Exception as e:
                result['tabs_crawled'][tab_name] = {
                    'url': tab_url,
                    'status': 'error',
                    'error': str(e)[:100],
                }
                print(f"{log_prefix} ✗ {tab_name}: {e}")
                _t.sleep(1)
        
        # 샘플 데이터 추출
        print(f"{log_prefix} 탭 콘텐츠 분석...")
        for tab_name, tab_data in result['tabs_crawled'].items():
            if tab_data['status'] == 'success':
                text = tab_data['snippet']
                
                # 간단한 텍스트 분석
                lines = text.split('\n')[:20]
                result['tabs_crawled'][tab_name]['preview_lines'] = [l.strip() for l in lines if l.strip()][:10]
    
    except Exception as e:
        print(f"{log_prefix} 오류: {e}")
        result['error'] = str(e)
    
    # JSON 저장
    out_dir = Path('_nyu_crawl_results')
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"nyu_tabs_content_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    
    # 결과 출력
    print(f"\n{'='*60}")
    print(f"크롤링 완료:")
    print(f"  총 시도: {len(result['attempted_tabs'])}개 탭")
    print(f"  성공: {result['successful_tabs']}개")
    print(f"  파일: {out_file}")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
