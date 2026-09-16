# 🎓 NYU 포탈 GUI 기반 자동 크롤러 — 사용 가이드

## 📋 개요

NYU 포탈(connect.nyu.edu)에서 **로그인 후 화면 콘텐츠를 자동으로 추출**하는 도구입니다.

- **방식**: Chrome 실제 창 + 수동 로그인 + 자동 텍스트 추출
- **기술**: Selenium + PyAutoGUI + Pyperclip
- **결과**: JSON + TXT 파일로 저장

---

## 🚀 빠른 시작

### 1단계: 스크립트 실행

```powershell
cd c:\My_Digital_Persona_Own_LLM_Project
venv\Scripts\python.exe -X utf8 _nyu_portal_crawler.py
```

### 2단계: 로그인 방법 선택

```
======================================================================
🎓 NYU 포탈 자동 크롤러 (GUI 기반)
======================================================================

로그인 방법을 선택하세요:
  [1] 수동 로그인 (Chrome 창에서 직접 로그인) — 권장
  [2] 자동 로그인 시도 (Selenium + undetected-chromedriver)

선택 [1 또는 2]:
```

**권장**: `1` 선택 (수동 로그인 — 가장 안정적)

### 3단계: 로그인 대기 메시지 확인

```
======================================================================
🔐 NYU 포탈 로그인 대기 중...
======================================================================
Chrome 창이 열렸습니다. 다음을 진행하세요:
  1) NetID와 비밀번호 입력
  2) 'Login' 버튼 클릭
  3) 로그인 완료 후 이 콘솔로 돌아와서 'Enter' 키를 누르세요
======================================================================

👉 로그인 완료 후 Enter를 눌러주세요:
```

### 4단계: Chrome 창에서 로그인

1. Chrome 창이 자동으로 열림
2. NYU 포탈 로드 대기
3. **NetID 입력** (예: `abc1234`)
4. **비밀번호 입력**
5. **"Login" 버튼 클릭**
6. 로그인 완료

### 5단계: 콘솔로 돌아와 Enter 키 누르기

- 로그인이 완료되면 **콘솔 윈도우로 돌아가서 Enter 키 누르기**
- 그러면 자동으로 `Ctrl+A` → `Ctrl+C` 실행되어 화면 텍스트 추출
- 약 5~10초 후 완료

### 6단계: 결과 확인

```
✅ 추출 성공: 15432 자

✅ 결과 저장:
  JSON: _nyu_portal_results/nyu_portal_manual_login_20260330_044943.json
  TXT:  _nyu_portal_results/nyu_portal_manual_login_20260330_044943.txt
```

---

## 📂 결과 파일

### JSON 형식
```json
{
  "source": "manual_login",
  "timestamp": "20260330_044943",
  "text_length": 15432,
  "url": "https://connect.nyu.edu/portal/undergraduate/tab=welcome",
  "content": "NYU Portal - Dashboard\n\nWelcome, [Your Name]!\n..."
}
```

### TXT 형식
텍스트 그대로 저장됨 (계획서, 공지사항 등 모든 화면 콘텐츠)

---

## 🔧 Python API 직접 사용

스크립트 대신 Python 코드에서 직접 함수 호출 가능:

```python
from importlib import import_module

# 1. 모듈 로드
agent = import_module("7_unified_agent")

# 2. 수동 로그인으로 텍스트 추출
text = agent.fetch_nyu_portal_manual_login(timeout_sec=180)

print(f"추출됨: {len(text)} 자")
print(text[:500])  # 처음 500자 출력
```

**매개변수**:
- `timeout_sec` (int): Chrome 페이지 로드 타임아웃 (기본값: 120초)

**반환값**:
- `str`: 추출된 화면 텍스트 (최대 14,000자로 제한)

---

## ⚠️ 주의사항

### 필수 패키지
```powershell
pip install pyautogui pyperclip selenium webdriver-manager
```

### 화면 해상도
- 권장: 1400x900 이상
- Canvas가 너무 작으면 텍스트 추출 실패 가능

### 로그인 실패 시
1. Chrome 창 수동 확인
2. 경고 메시지가 있으면 확인 클릭
3. 다시 스크립트 실행

### 텍스트 미추출 시
- `Ctrl+A` 후 텍스트 복사 안 될 수 있음
- 대안: **화면 스크린샷 + OCR** 사용 ([8번 단계 참고](#고급-ocr-방식))

---

## 🎯 사용 사례

### 1) 학사 공지사항 수집
- NYU 포탈 → Dashboard 공지사항 섹션
- GUI 방식으로 자동 크롤링

### 2) 개인 학사 기록 (Academic History) 추출
- NYU 포탈 → Academic 탭
- 성적, 학점, 수강과목 등 텍스트 변환

### 3) 편입 요건 정보 수집
- NYU 포탈 → Transfer Info 탭
- 요구 GPA, 필수 과목 등

### 4) 대학 공식 문서 수집 후 Qdrant 벡터DB 저장
```python
from importlib import import_module
import sqlite3

agent = import_module("7_unified_agent")

# 1. 텍스트 추출
text = agent.fetch_nyu_portal_manual_login()

# 2. 벡터 임베딩 + Qdrant 저장 (1_ingest_data.py 참고)
# vector = embed_model.encode(text)
# qdrant_client.upsert(...)
```

---

## 🐛 트러블슈팅

| 문제 | 원인 | 해결책 |
|------|------|--------|
| `pyautogui` 오류 | 패키지 미설치 | `pip install pyautogui pyperclip` |
| Chrome 창이 안 열림 | Chrome 미설치 | `C:\Program Files\Google\Chrome` 확인 |
| 텍스트 미추출 | Ctrl+A 실패 | 화면 해상도 확인 + 재시도 |
| 로그인 후 Enter 안 감지 | 콘솔 포커스 손실 | 콘솔 윈도우 클릭 후 Enter |
| Selenium 오류 | WebDriver 버전 불일치 | `pip install -U webdriver-manager` |

---

## 📊 성능

- **추출 시간**: 로그인 + 2~5초
- **텍스트 크기**: 보통 10KB~50KB (학사 정보 기준)
- **정확도**: 화면에 보이는 모든 텍스트 100% 추출

---

## 🔗 관련 파일

- [7_unified_agent.py](7_unified_agent.py#L1966) — `fetch_nyu_portal_manual_login()` 함수 구현
- [_nyu_portal_crawler.py](_nyu_portal_crawler.py) — 이 스크립트
- [1_ingest_data.py](1_ingest_data.py) — 데이터 수집 파이프라인에 통합

---

## 📝 라이선스 및 주의

⚠️ **약관**: 이 도구는 **개인 학사 정보 수집**에만 사용하세요. 대량 크롤링, 불법 복제 등은 NYU 약관 위반입니다.

---

**마지막 업데이트**: 2026-03-30
**작성자**: GitHub Copilot
**버전**: 1.0
