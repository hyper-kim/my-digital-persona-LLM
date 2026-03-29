# 프로젝트 컨텍스트: My Digital Persona — 편입 AI 상담 시스템

## 프로젝트 개요
미국 대학 편입(Transfer Admission) 상담을 위한 로컬 멀티에이전트 AI 파이프라인.  
Google Drive(OneNote/Drive 파일) → 수집 → Qdrant 벡터DB → 편입 상담 UI

## 사용자 프로필 (상담 대상)
- 재학교: 고려대학교 (Korea University), 인공지능학과
- 출신고: 한성과학고 (Hansung Science High School)
- GPA: 3.95 / 4.5 (4.0 환산 ≈ 3.51)
- TOEFL: 85
- SAT: 미응시
- 이수 학점: ~60학점
- 지원 예정: Cornell, Stanford, NYU, UPenn (이미 등록)

## 핵심 파일 구조
| 파일 | 역할 |
|---|---|
| `1_ingest_data.py` | Google Drive/OneNote → Qdrant 벡터 수집 |
| `2_run_agents.py` | 에이전트 파이프라인 실행 |
| `4_transfer_agent.py` | 편입 팩트 검색 에이전트 |
| `5_transfer_ui.py` | Gradio UI (포트 7862, server_name=0.0.0.0) |
| `6_advisor_agent.py` | 스펙 분석/전략 어드바이저 |
| `7_unified_agent.py` | **메인 통합 에이전트** (search + advisor + RAG + 웹크롤) |
| `8_essay_agent.py` | 에세이 작성 에이전트 |
| `9_email_agent.py` | 이메일 발송 (Gmail/Naver/NaverWorks 3종) |
| `10_kakao_agent.py` | 카카오톡 알림 에이전트 |
| `sequential_run.py` | 전체 파이프라인 순차 실행 |
| `run_watchdog.py` | 프로세스 감시/재시작 |
| `_gcs_backup.py` | GCS 자동 백업 (DB 파일들) |
| `_monitor.py` | 파이프라인 상태 모니터 (60초 주기) |

## 기술 스택
- **LLM**: Ollama 로컬 실행
  - SEARCH_MODEL: `gemma3:12b` (env로 변경 가능)
  - ADVISOR_MODEL: `qwen3:14b` (한국어 최강)
  - 설치된 모델: `qwen3:14b`, `gemma3:12b`, `llama3.2-vision:11b`, `qwen3-vl:8b`
- **벡터DB**: Qdrant (로컬 SQLite, 컬렉션명: `omni_persona_v3`)
  - 경로: `qdrant_db/` — 24.8GB SQLite, 로드 30초 타임아웃 적용
- **임베딩**: `intfloat/multilingual-e5-large`
- **UI**: Gradio, 포트 7862, 테마 커스텀
- **웹크롤**: httpx + BeautifulSoup, Chrome UA 사용
- **Python 환경**: `venv\Scripts\python.exe -X utf8`

## UNIVERSITY_REGISTRY (크롤링 대상 URL — 모두 200 OK 확인됨)
- Cornell → `irp.dpb.cornell.edu/university-factbook/admissions` + `common-data-set`
- Stanford → `admission.stanford.edu/apply/transfer/`
- NYU → `www.nyu.edu/admissions/undergraduate-admissions/how-to-apply.html`
- UPenn → `catalog.upenn.edu/undergraduate/`
- MIT → `admissions.mit.edu/apply/transfer-applicants`
- Columbia → `undergrad.admissions.columbia.edu/apply/transfer`
- UC Berkeley → `admission.universityofcalifornia.edu/how-to-apply/applying-as-a-transfer/`
- UCLA → `admission.ucla.edu/apply/transfer`
- UChicago → `collegeadmissions.uchicago.edu/apply`
- Carnegie Mellon → `admission.enrollment.cmu.edu/pages/transfer-admission`
- Georgia Tech → `admission.gatech.edu/transfer`
- Purdue → `www.purdue.edu/admissions/transfer/`

> 주의: `admissions.cornell.edu`, `admissions.upenn.edu`, `admissions.berkeley.edu`는 Cloudflare 403 차단

## 인프라
- **로컬 PC**: Windows, Tailscale IP `100.90.121.102` (고정)
- **외부 접속**: `http://100.90.121.102:7862` (Tailscale 동일 계정 기기에서)
- **자동시작**: Windows 시작 프로그램에 `5_transfer_ui.py` 등록
- **방화벽**: 포트 7862 인바운드 허용 (`Transfer Chatbot UI 7862` 규칙)
- **GCP VM**: 데이터 수집 파이프라인 실행용 (별도 VM)
- **GCS 버킷**: DB 백업 (`GCS_BACKUP_BUCKET` env)

## 주요 환경변수 (.env)
```
UI_HOST=0.0.0.0
UI_PORT=7862
SEARCH_MODEL=gemma3:12b
ADVISOR_MODEL=qwen3:14b
ADVISOR_TEMPERATURE=0.6
```

## 알려진 이슈 / 해결됨
- Qdrant 24.8GB 무한 로딩 → `concurrent.futures` 30초 타임아웃으로 해결 (`_rag_load_failed` 플래그)
- 입학처 URL 403/404 → Chrome UA + 대체 URL로 해결 (위 REGISTRY 참고)
- `gemma3:12b` 한국어 약함 → SEARCH_MODEL을 `qwen3:14b`로 변경 검토 중

## 코딩 규칙
- 파이썬 파일은 반드시 `# -X utf8` 인코딩으로 실행
- 환경변수는 `.env`에서 `python-dotenv`로 로드 (`load_dotenv(override=False)`)
- 보안 민감 정보(토큰/키)는 절대 코드에 하드코딩 금지
- 에러 처리: Qdrant/Ollama 연결 실패는 graceful degradation (서비스 중단 없이)
