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
| `_vm_manager.py` | GCP VM 관리 (시작/정지/생성/IP조회/.env업데이트) |
| `_vm_monitor.py` | GCP SSH 연속실패 감지 → VM 자동 재시작/재생성 |
| `_retry_empty_onenote.py` | CV EMPTY 실패 파일 재처리 등록 |

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
- **GCS 버킷**: DB 백업 (`GCS_BACKUP_BUCKET` env)

### GCP VM (데이터 수집 파이프라인)
- **인스턴스명**: `mydigitalpersonaembedder`
- **프로젝트 ID**: `project-1ffe4ac8-e493-4a26-ac3`
- **머신타입**: `g2-standard-32` (L4 GPU)
- **존**: `us-east1-c`
- **프로비저닝**: `STANDARD` (Spot → Standard 전환, commit ec4108c)
  - Spot은 Google이 선점 시 인스턴스+디스크 완전 삭제됨 → Standard 사용
- **현재 IP**: `34.24.238.116` (VM 재생성 후 `.env` 자동 업데이트됨)
- **SA 키**: `C:\Users\kjy\.ssh\project-1ffe4ac8-e493-4a26-ac3-9adced7fa7f2.json`
- **SSH 개인키**: `C:\Users\kjy\.ssh\gcp_key_fixed`
- **SSH 공개키**: `C:\Users\kjy\.ssh\gcp_key_fixed.pub`
- **VM 사용자**: `kjy`
- **스타트업 스크립트**: `vm_setup.sh` (Ubuntu 22.04 + CUDA + Ollama + qwen3-vl:8b)
- **디스크**: 100GB pd-balanced, autoDelete=True (재생성 시 신규 이미지)
- **`serviceAccounts` 필드**: `[]` 빈 배열로 설정 (`mydigital-persona-embedder` SA에 serviceAccountUser 권한 없음)
- **자동 복구**: `_vm_monitor.py`가 SSH 연속실패 5회 감지 시
  - `vm_state`에 `"404"` 포함 → `create_vm()` 자동 호출 + `.env` IP 업데이트
  - 그 외 TERMINATED → `start_vm()` 호출

## 주요 환경변수 (.env)
```
UI_HOST=0.0.0.0
UI_PORT=7862
SEARCH_MODEL=gemma3:12b
ADVISOR_MODEL=qwen3:14b
ADVISOR_TEMPERATURE=0.6

# GCP VM
GCP_VM_NAME=mydigitalpersonaembedder
GCP_VM_ZONE=us-east1-c
GCP_PROJECT_ID=project-1ffe4ac8-e493-4a26-ac3
CLOUD_VM_IP=34.24.238.116           # VM 재생성 시 _vm_manager.update_env_ip()로 자동 교체
CLOUD_OLLAMA_URL=http://34.24.238.116:11434
VM_IDLE_TIMEOUT_MIN=10
USE_GCP_SSH_OCR=true
MAX_GCP_WORKERS=8

# 데이터 수집
PDF_CHUNK_PAGES=5                   # 기본값 30 → 5로 축소 (최대 병렬 분할)
```
> `.env`는 `.gitignore`에 포함됨. `python-dotenv`로 로드 (`load_dotenv(override=False)`)

## 알려진 이슈 / 해결됨
- Qdrant 24.8GB 무한 로딩 → `concurrent.futures` 30초 타임아웃으로 해결 (`_rag_load_failed` 플래그)
- 입학처 URL 403/404 → Chrome UA + 대체 URL로 해결 (위 REGISTRY 참고)
- `gemma3:12b` 한국어 약함 → SEARCH_MODEL을 `qwen3:14b`로 변경 검토 중
- **Spot VM 선점 삭제** → STANDARD 프로비저닝으로 전환 (commit ec4108c)
  - Spot 선점 시 인스턴스+디스크 완전 삭제됨 (데이터는 로컬이라 유실 없음)
- **VM 재생성 시 `serviceAccounts` 오류** → `"serviceAccounts": []` 빈 배열로 고정
  - SA(`mydigital-persona-embedder`)에 `serviceAccountUser` 역할 없음
- **PDF 대용량 청크** → `PDF_CHUNK_PAGES=5`, 2페이지 이상 무조건 분할 (commit c0cb494)
- **`_vm_monitor.py` 자동 복구** → 404 감지 시 `create_vm()` + `update_env_ip()` 자동 호출

## 코딩 규칙
- 파이썬 파일은 반드시 `# -X utf8` 인코딩으로 실행
- 환경변수는 `.env`에서 `python-dotenv`로 로드 (`load_dotenv(override=False)`)
- 보안 민감 정보(토큰/키)는 절대 코드에 하드코딩 금지
- 에러 처리: Qdrant/Ollama 연결 실패는 graceful degradation (서비스 중단 없이)
