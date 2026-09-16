#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_crawl_nyu.py — 크롤링 함수 테스트 (Mock 드라이버)
"""

import importlib

# 모의 드라이버 생성
class MockDriver:
    def __init__(self):
        self.page_source = """
        <html>
            <head><title>NYU Transfer Info</title></head>
            <body>
                <h1>Transfer Requirements</h1>
                <table>
                    <tr><th>Requirement</th><th>Value</th></tr>
                    <tr><td>Minimum GPA</td><td>3.0</td></tr>
                </table>
                <a href="/transfer-faq">Transfer FAQ</a>
                <a href="/academic-requirements">Academic Requirements</a>
                <p>International students must provide TOEFL scores.</p>
            </body>
        </html>
        """
        self.current_url = "https://connect.nyu.edu/portal/transfer"
        self.title = "NYU Transfer"
    
    def set_page_load_timeout(self, timeout):
        pass
    
    def get(self, url):
        self.current_url = url
        print(f"[MOCK] GET {url}")

# 테스트
m = importlib.import_module('7_unified_agent')
driver = MockDriver()

print("테스트 시작: crawl_nyu_portal_structured()")
print("=" * 70)

try:
    result = m.crawl_nyu_portal_structured(driver, timeout=5)
    
    print(f"\n✅ 함수 실행 성공!")
    print(f"\n결과 구조:")
    print(f"  - title: {result.get('title')}")
    print(f"  - pages: {len(result.get('pages', []))} 개")
    print(f"  - tables: {len(result.get('tables', []))} 개")
    print(f"  - links: {len(result.get('links', []))} 개")
    print(f"  - transfer_info: {result.get('transfer_info', {})}")
    
    if result.get('pages'):
        page = result['pages'][0]
        print(f"\n첫 페이지:")
        print(f"  - title: {page.get('title')}")
        print(f"  - tables: {len(page.get('tables', []))}")
        print(f"  - list_items: {len(page.get('list_items', []))}")
        print(f"  - paragraphs: {len(page.get('paragraphs', []))}")
    
    if result.get('tables'):
        table = result['tables'][0]
        print(f"\n첫 테이블:")
        print(f"  - title: {table.get('title')}")
        print(f"  - rows: {len(table.get('rows', []))}")
    
    print("\n" + "=" * 70)
    print("✅ 모든 테스트 통과!")
        
except Exception as e:
    print(f"\n❌ 오류: {e}")
    import traceback
    traceback.print_exc()
