import os
import tempfile
import sys
import time

# Windows cp949 환경에서 이모지/한글 print 충돌 방지 (파이프/리다이렉트 시 필수)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import subprocess
import base64 as _base64
import ollama
import whisper
import qdrant_client
import torch
import logging
import sqlite3
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from PIL import Image, ImageEnhance
import io
from pdf2image import convert_from_path
from llama_index.core import Document, VectorStoreIndex, StorageContext, SimpleDirectoryReader
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
import zlib
import re
import json
from datetime import datetime
import shutil
import httpx

try:
    import olefile
    OLEFILE_AVAILABLE = True
except ImportError:
    OLEFILE_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    print("⚠️ [경고] PyMuPDF가 없습니다. (pip install pymupdf). 모든 PDF가 스캔본으로 간주되어 클라우드 GPU로 전송됩니다.")

import ctypes
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import win32com.client as _win32com
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False

try:
    from googleapiclient.discovery import build as _gdrive_build
    from google.oauth2.service_account import Credentials as _SACredentials
    from googleapiclient.http import MediaIoBaseDownload as _MediaIoBaseDownload
    GDRIVE_API_AVAILABLE = True
except ImportError:
    GDRIVE_API_AVAILABLE = False

# ==========================================
# ⚙️ [설정] 환경변수 기반 (GCP/로컬 공용)
# ==========================================
PROJECT_DIR = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
RAW_DATA_DIR = os.getenv("RAW_DATA_DIR", r"G:\내 드라이브\해외 대학 편입 정리\한성과고")
QDRANT_PATH = os.getenv("QDRANT_PATH", os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL  = os.getenv("QDRANT_URL", "")
STATE_DB_PATH = os.getenv("STATE_DB_PATH", os.path.join(PROJECT_DIR, "processed_files.db"))
POPPLER_PATH = os.getenv("POPPLER_PATH", "")
POPPLER_PATH = POPPLER_PATH if POPPLER_PATH else None
LOG_FILE = os.getenv("LOG_FILE", os.path.join(PROJECT_DIR, "ingestion_log.txt"))
DPI = int(os.getenv("DPI", "150"))

VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:8b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-large")
# 병렬 처리 워커 수
# MAX_CPU_WORKERS : 텍스트/HWP 동시 파일 수 (로컬 CPU)
# MAX_VISION_WORKERS: Cloud GPU API 동시 호출 수 (I/O 바운드, 넉넉히)
# MAX_AUDIO_WORKERS : 로컬 GPU Whisper 동시 파일 수 (항상 1 – VRAM 한계)
MAX_CPU_WORKERS    = int(os.getenv("MAX_CPU_WORKERS",    str(max(1, (os.cpu_count() or 4)))))
MAX_VISION_WORKERS = int(os.getenv("MAX_VISION_WORKERS", "6"))
MAX_AUDIO_WORKERS  = int(os.getenv("MAX_AUDIO_WORKERS",  "1"))
QUEUE_SIZE = int(os.getenv("QUEUE_SIZE", "200"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
PDF_CHUNK_PAGES = int(os.getenv("PDF_CHUNK_PAGES", "30"))  # 이 페이지 수 초과 PDF는 30p 단위 청크로 분할 처리
AUTO_BACKUP_INTERVAL_SEC = int(os.getenv("AUTO_BACKUP_INTERVAL_SEC", "900"))
BACKUP_ROOT = os.getenv("BACKUP_ROOT", os.path.join(PROJECT_DIR, "backups"))

# ABBYY / GCP 추가 풀 / Drive API
ABBYY_ENABLED     = os.getenv("ABBYY_ENABLED", "false").lower() == "true"
MAX_GCP_WORKERS   = int(os.getenv("MAX_GCP_WORKERS",   "4"))
MAX_COORD_WORKERS = int(os.getenv("MAX_COORD_WORKERS", "4"))
GOOGLE_SA_KEY_PATH = os.getenv("GOOGLE_SA_KEY_PATH", "")
GDRIVE_MOUNT      = os.getenv("GDRIVE_MOUNT", r"G:\\")

# pool 참조 (dispatch_cv_combined에서 동적으로 채움)
_cpu_pool_ref:   List = [None]
_gcp_pool_ref:   List = [None]

# ☁️ 클라우드 GPU 설정 (GCP L4 Spot VM)
CLOUD_OLLAMA_URL = os.getenv("CLOUD_OLLAMA_URL", "http://35.211.58.231:11434")
LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434")
# 커넥트 5초, 읽기 15초 타임아웃 — CPU 모드 무한대기 방지 (5회×15s=75s 안에 차단기 동작)
_OLLAMA_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
cloud_client = ollama.Client(host=CLOUD_OLLAMA_URL, timeout=_OLLAMA_TIMEOUT)
local_client  = ollama.Client(host=LOCAL_OLLAMA_URL, timeout=httpx.Timeout(connect=3.0, read=20.0, write=20.0, pool=3.0))

# GCP VM SSH 직접 처리 설정 ─ USE_GCP_SSH_OCR=true 로 활성화
# 스캔PDF/이미지를 로컬에서 변환하지 않고 GCP VM에 원본 파일 전송 → GCP에서 변환+OCR
# G:드라이브 파일이 이미 로컬 캐시에 있으면 SCP 그대로, 없으면 한 번만 다운로드
CLOUD_VM_IP     = os.getenv("CLOUD_VM_IP",     "35.211.58.231")
CLOUD_VM_USER   = os.getenv("CLOUD_VM_USER",   "kjy")
SSH_KEY_PATH    = os.getenv("SSH_KEY_PATH",    r"C:\Users\kjy\.ssh\gcp_key_fixed")
USE_GCP_SSH_OCR = os.getenv("USE_GCP_SSH_OCR", "false").lower() == "true"

# SKIP_LOCAL_OCR=true → 로컬 Ollama OCR을 건너뜀 (텍스트/HWP/오디오만, VM 연결 전 쾌속 모드)
SKIP_LOCAL_OCR = os.getenv("SKIP_LOCAL_OCR", "false").lower() == "true"

# ─── Spot VM 회로차단기 (연속 실패 5회 → 로컬 폴백 자동전환) ─────────────
import threading as _threading
_cloud_fail_count   = 0
_cloud_fail_lock    = _threading.Lock()
_CLOUD_DOWN         = _threading.Event()   # set = 클라우드 다운 상태
_LOCAL_OCR_DOWN     = _threading.Event()   # set = 로컬 Ollama 없음
_CLOUD_FAIL_THRESH  = int(os.getenv("CLOUD_FAIL_THRESH", "5"))

def _cloud_ocr_success():
    global _cloud_fail_count
    with _cloud_fail_lock:
        _cloud_fail_count = 0
    _CLOUD_DOWN.clear()

def _cloud_ocr_failure(err):
    if USE_GCP_SSH_OCR:
        return  # SSH OCR 모드: HTTP 실패가 SSH를 막지 않음
    global _cloud_fail_count
    with _cloud_fail_lock:
        _cloud_fail_count += 1
        tripped = _cloud_fail_count >= _CLOUD_FAIL_THRESH
    if tripped and not _CLOUD_DOWN.is_set():
        _CLOUD_DOWN.set()
        logging.error(f"[SPOT-PREEMPT] Cloud OCR {_CLOUD_FAIL_THRESH}회 연속 실패 → 로컬 OCR 비활성화")
        print(f"\n⚠️  GCP Spot VM 선제 감지 → 로컬 OCR 비활성화 + 긴급 백업 시작 (이후 PDF/이미지는 텍스트 레이어만 추출)")
        threading.Thread(target=backup_vector_db, args=("spot_preempted",), daemon=True).start()

def _check_cloud_on_startup():
    """기동 시 클라우드 연결 및 모델 응답성 확인. 느리거나 없으면 즉시 _CLOUD_DOWN 설정."""
    cloud_ok = False
    try:
        # 1) Ollama 서버 응답 확인
        r = httpx.get(f"{CLOUD_OLLAMA_URL.rstrip('/')}/api/tags",
                      timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0))
        if r.status_code == 200:
            # 2) 모델이 로드되어 있는지 /api/ps 확인 (Ollama 0.1.20+)
            try:
                ps = httpx.get(f"{CLOUD_OLLAMA_URL.rstrip('/')}/api/ps",
                               timeout=httpx.Timeout(connect=5.0, read=5.0, write=3.0, pool=3.0))
                if ps.status_code == 200:
                    running_models = [m.get("name", "") for m in ps.json().get("models", [])]
                    model_ready = any(VISION_MODEL.split(":")[0] in m for m in running_models)
                    if model_ready:
                        print(f"☁️  GCP Vision OCR 준비 완료 — {VISION_MODEL} 로드됨")
                        cloud_ok = True
                    else:
                        print(f"⚠️  GCP Ollama 실행 중이나 {VISION_MODEL} 미로딩 → Cloud OCR 비활성화")
            except Exception:
                # /api/ps 미지원 버전: 서버가 뜨면 OK로 간주
                print(f"☁️  GCP Vision OCR 연결 확인 ({CLOUD_OLLAMA_URL})")
                cloud_ok = True
    except Exception:
        pass

    if not cloud_ok:
        if USE_GCP_SSH_OCR:
            # SSH OCR 모드: Ollama HTTP 불필요, _CLOUD_DOWN 설정 안 함
            print(f"☁️  GCP SSH OCR 활성화 (HTTP Ollama 불필요, SSH 직접 처리)")
        else:
            _CLOUD_DOWN.set()
            print(f"⚠️  GCP Vision OCR 비활성화 ({CLOUD_OLLAMA_URL})")

    # 로컬 Ollama도 없으면 로컬 OCR도 비활성화
    if SKIP_LOCAL_OCR:
        _LOCAL_OCR_DOWN.set()
        print("⏭️  SKIP_LOCAL_OCR=true → 로컬 OCR 건너뜀 (텍스트/HWP/오디오 전용 고속 모드)")
        return
    try:
        r2 = httpx.get(f"{LOCAL_OLLAMA_URL.rstrip('/')}/api/tags",
                       timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0))
        if r2.status_code == 200:
            # 로컬에 모델이 있어도 GPU 없이 CPU 모드면 매우 느림 → 간단한 속도 테스트
            tags = r2.json().get("models", [])
            has_model = any(VISION_MODEL.split(":")[0] in m.get("name", "") for m in tags)
            if has_model and not torch.cuda.is_available():
                _LOCAL_OCR_DOWN.set()
                print("⚠️  로컬 Ollama GPU 없음 → 로컬 OCR 비활성화 (SKIP_LOCAL_OCR=true 권장)")
            elif has_model:
                print(f"🖥️  로컬 Ollama + {VISION_MODEL} 확인 — GPU OCR 활성화")
            else:
                _LOCAL_OCR_DOWN.set()
                print(f"⚠️  로컬 Ollama 실행 중이나 {VISION_MODEL} 미설치 → 로컬 OCR 비활성화")
        else:
            _LOCAL_OCR_DOWN.set()
    except Exception:
        _LOCAL_OCR_DOWN.set()
        if not cloud_ok:
            print("⚠️  로컬 Ollama 없음 → Vision OCR 완전 비활성화 (텍스트/HWP/오디오만 처리)")

# ─── UI 일시정지 이벤트 ─────────────────────────────────────────────────────
INGESTION_PAUSED = _threading.Event()
INGESTION_PAUSED.set()  # 기본: 실행 중

# ─── PageNode: 페이지 단위 요소 노드 ──────────────────────────────────────────
@dataclass
class PageNode:
    page_idx:        int
    total_pages:     int
    element_type:    str            # "text" | "visual" | "audio" | "mixed"
    text:            str
    parent_doc_id:   str            # abs_path of source file
    sibling_page_id: Optional[str] = None   # 같은 페이지의 반대 element_type 노드 ID
    grade:           str = ""
    subject:         str = ""
    semester:        str = ""
    exam_type:       str = ""
    doc_type:        str = ""
    parent_folder:   str = ""

# HWP 처리 설정 (olefile + zlib 바이너리 디코드)
if not OLEFILE_AVAILABLE:
    print("⚠️ [경고] olefile이 없습니다. HWP 텍스트 추출이 제한됩니다. (pip install olefile)")

# 로깅 설정
logging.basicConfig(filename=LOG_FILE, level=logging.ERROR, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

# 기동 시 클라우드 헬스체크 (MAX_VISION_WORKERS > 0 일 때만)
if MAX_VISION_WORKERS > 0:
    _check_cloud_on_startup()

# GPU 가속 설정
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"\n🚀 [시스템] {device.upper()} 가속 모드 가동 시작")

# 엔진 로딩
try:
    print("🧠 AI 엔진(Whisper, Embedding) 로딩 중...")
    whisper_model = whisper.load_model(WHISPER_MODEL, device=device)
    embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME, device=device)
except Exception as e:
    print(f"❌ 엔진 로딩 실패: {e}")
    exit()

# Qdrant DB 초기화
print("🗄️ Qdrant Vector DB 연결 중...")
client = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
vector_store = QdrantVectorStore(client=client, collection_name="omni_persona_v3")
storage_context = StorageContext.from_defaults(vector_store=vector_store)

# 초고속 중복 체크용 SQLite 세팅
state_conn = sqlite3.connect(STATE_DB_PATH, check_same_thread=False)
state_conn.execute("PRAGMA journal_mode=WAL")
state_conn.execute("PRAGMA synchronous=NORMAL")
state_conn.execute("CREATE TABLE IF NOT EXISTS processed (file_path TEXT PRIMARY KEY)")
state_conn.execute("""
    CREATE TABLE IF NOT EXISTS failed_files (
        file_path TEXT PRIMARY KEY,
        error     TEXT,
        attempt_count INTEGER DEFAULT 1,
        last_attempt  TEXT
    )
""")

# 큐(Queue) 세팅 (생산자-소비자 패턴)
result_queue = queue.Queue(maxsize=QUEUE_SIZE) # 추출 완료된 텍스트 대기열
backup_stop_event = threading.Event()


def resolve_input_root():
    """입력 폴더 경로를 CLI 인자 또는 환경변수에서 결정합니다."""
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return RAW_DATA_DIR


def backup_vector_db(reason="periodic"):
    """Qdrant DB와 상태 DB를 프로젝트 내부 백업 경로에 주기적으로 미러링합니다."""
    try:
        os.makedirs(BACKUP_ROOT, exist_ok=True)

        # .lock 등 잠긴 파일은 복사 시 건너뜀
        def _ignore_locks(src, names):
            return [n for n in names if n.endswith('.lock')]

        if os.path.isdir(QDRANT_PATH):
            mirror_dir = os.path.join(BACKUP_ROOT, "qdrant_db_mirror")
            if os.path.exists(mirror_dir):
                shutil.rmtree(mirror_dir, ignore_errors=True)
            try:
                shutil.copytree(QDRANT_PATH, mirror_dir, ignore=_ignore_locks)
            except shutil.Error:
                pass  # Qdrant 실행 중 잠긴 파일 무시 (정상)

        if os.path.isfile(STATE_DB_PATH):
            shutil.copy2(STATE_DB_PATH, os.path.join(BACKUP_ROOT, "processed_files.db"))

        latest_snapshot = os.path.join(BACKUP_ROOT, "snapshot_latest")
        if os.path.exists(latest_snapshot):
            shutil.rmtree(latest_snapshot, ignore_errors=True)
        os.makedirs(latest_snapshot, exist_ok=True)

        if os.path.isdir(QDRANT_PATH):
            try:
                shutil.copytree(QDRANT_PATH, os.path.join(latest_snapshot, "qdrant_db"), ignore=_ignore_locks)
            except shutil.Error:
                pass  # 잠긴 파일 무시
        if os.path.isfile(STATE_DB_PATH):
            shutil.copy2(STATE_DB_PATH, os.path.join(latest_snapshot, "processed_files.db"))
    except Exception as e:
        logging.error(f"[BACKUP] 실패 ({reason}) | {e}")


def backup_worker():
    while not backup_stop_event.wait(AUTO_BACKUP_INTERVAL_SEC):
        backup_vector_db(reason="periodic")


def log_file_failure(file_path, error_msg):
    """실패 파일을 SQLite에 기록합니다. 파이프라인은 절대 멈추지 않습니다."""
    ts = datetime.now().isoformat()
    try:
        state_conn.execute("""
            INSERT INTO failed_files (file_path, error, attempt_count, last_attempt)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                error         = excluded.error,
                attempt_count = attempt_count + 1,
                last_attempt  = excluded.last_attempt
        """, (file_path, str(error_msg)[:2000], ts))
        state_conn.commit()
    except Exception:
        pass  # 로그 기록 실패해도 파이프라인 계속

# ==========================================
# 🛠️ [방어 로직] 안전한 파일 처리 함수
# ==========================================

def _vision_ocr_single_image(image, page_label, use_cloud=True):
    """페이지 단위 Vision OCR. 스팟 VM 다운 감지 시 자동으로 로컬 폴백."""
    # SSH OCR 모드: 파일 단위 SSH 처리, 페이지 단위 HTTP 호출 불필요
    if USE_GCP_SSH_OCR:
        return ""
    # 회로차단기: 클라우드 + 로컬 둘 다 다운이면 즉시 빈 문자열 반환
    if _CLOUD_DOWN.is_set() and _LOCAL_OCR_DOWN.is_set():
        return ""

    prompt = "이 이미지의 모든 한국어/영어 손필기, 수식, 도표, 코드를 완벽한 텍스트와 마크다운 수식으로 추출해. 다른 설명 없이 내용만 정확히 베껴 써."

    # 회로차단기: 클라우드 연속 실패 → 로컬 직행
    if use_cloud and _CLOUD_DOWN.is_set():
        use_cloud = False

    client_to_use = cloud_client if use_cloud else local_client

    with tempfile.TemporaryDirectory() as temp_dir:
        img_path = os.path.join(temp_dir, "page.jpg")
        if not safe_image_save(image, img_path):
            return ""

        try:
            res = client_to_use.chat(
                model=VISION_MODEL,
                messages=[{'role': 'user', 'content': prompt, 'images': [img_path]}],
                keep_alive=0,
                options={"num_ctx": 4096, "temperature": 0},
            )
            if use_cloud:
                _cloud_ocr_success()
            return f"\n\n--- {page_label} ---\n" + res['message']['content']
        except Exception as e:
            if use_cloud:
                _cloud_ocr_failure(e)
                logging.error(f"☁️ Cloud GPU OCR 실패 → 로컬 폴백: {page_label} | {e}")
                if _LOCAL_OCR_DOWN.is_set():
                    return ""
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    res = local_client.chat(
                        model=VISION_MODEL,
                        messages=[{'role': 'user', 'content': prompt, 'images': [img_path]}],
                        keep_alive=0,
                        options={"num_ctx": 4096, "temperature": 0},
                    )
                    return f"\n\n--- {page_label} ---\n" + res['message']['content']
                except Exception as e2:
                    logging.error(f"로컬 폴백 OCR 실패: {page_label} | {e2}")
                    _LOCAL_OCR_DOWN.set()
                    return ""
            else:
                logging.error(f"로컬 OCR 실패: {page_label} | {e}")
                _LOCAL_OCR_DOWN.set()
                return ""

def safe_image_save(image, path):
    try:
        if image.mode in ("RGBA", "P", "LA") or (image.mode == "L" and "transparency" in image.info):
            image = image.convert("RGB")
        image.save(path, 'JPEG', quality=80, optimize=True)
        return True
    except Exception as e:
        logging.error(f"이미지 변환 실패: {path} | 에러: {e}")
        return False


def is_pdf_page_text_based(page):
    """페이지 단위 텍스트 기반 여부 판단."""
    try:
        text = page.get_text("text")
        return len(text.strip()) >= 100
    except Exception:
        return False

def is_text_pdf(file_path):
    """PDF가 텍스트 기반인지 스캔본(CV 필요)인지 판단합니다."""
    if not PYMUPDF_AVAILABLE: return False
    try:
        doc = fitz.open(file_path)
        text_len = sum(len(page.get_text("text")) for page in doc)
        # 페이지당 평균 100자 이상이면 순수 텍스트 기반 논문/문서로 간주 (Cloud GPU 비용 절약)
        return (text_len / max(len(doc), 1)) > 100 
    except:
        return False


def extract_hwp_text_ole(file_path):
    """olefile + zlib 기반 HWP 본문 텍스트 추출."""
    if not OLEFILE_AVAILABLE:
        raise RuntimeError("olefile이 설치되지 않았습니다.")

    texts = []
    try:
        if not olefile.isOleFile(file_path):
            raise RuntimeError("유효한 OLE(HWP) 파일이 아닙니다.")

        with olefile.OleFileIO(file_path) as ole:
            streams = ["/".join(name) for name in ole.listdir() if name and name[0].startswith("BodyText")]
            streams.sort()

            for stream_name in streams:
                raw = ole.openstream(stream_name).read()
                try:
                    data = zlib.decompress(raw, -15)
                except Exception:
                    data = raw

                decoded = data.decode("utf-16", errors="ignore")
                decoded = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", decoded)
                texts.append(decoded)

        return "\n".join(t for t in texts if t.strip())
    except Exception as e:
        logging.error(f"HWP 바이너리 텍스트 추출 실패: {file_path} | {e}")
        return ""


def extract_mixed_pdf(file_path):
    """혼합 PDF: 텍스트 페이지는 CPU, 스캔 페이지는 Cloud GPU OCR을 파일 내 페이지 단위 병렬 처리."""
    if not PYMUPDF_AVAILABLE:
        return task_vision_cloud_gpu(file_path, "pdf")

    merged_text   = {}
    scan_indices  = []
    try:
        doc = fitz.open(file_path)
        for page_index, page in enumerate(doc):
            if is_pdf_page_text_based(page):
                page_text = page.get_text("text")
                if page_text.strip():
                    merged_text[page_index] = f"\n\n--- Page {page_index + 1} (TEXT) ---\n{page_text}"
            else:
                scan_indices.append(page_index)
        doc.close()

        # 스캔/손필기 페이지: Cloud API 호출이므로 I/O 바운드 → 페이지 단위 병렬 처리
        # OCR 완전 비활성화 상태면 convert_from_path 자체를 건너뜀 (CPU/메모리 낭비 방지)
        if scan_indices and not (_CLOUD_DOWN.is_set() and _LOCAL_OCR_DOWN.is_set()):
            # 순수 스캔 PDF (no 텍스트 페이지) + SSH 모드 → GCP에서 파일 전체 처리 (로컀 RAM 절약)
            if USE_GCP_SSH_OCR and not _CLOUD_DOWN.is_set() and not merged_text:
                return task_vision_via_gcp_ssh(file_path, "pdf")

            def _ocr_one_page(page_index):
                try:
                    images = convert_from_path(
                        file_path, dpi=DPI, poppler_path=POPPLER_PATH,
                        first_page=page_index + 1, last_page=page_index + 1,
                        thread_count=1,
                    )
                    if not images:
                        return page_index, ""
                    ocr_text = _vision_ocr_single_image(
                        images[0], f"Page {page_index + 1}", use_cloud=True
                    )
                    images[0].close()
                    del images
                    return page_index, ocr_text
                except Exception as e:
                    logging.error(f"페이지 OCR 실패: {file_path} p{page_index+1} | {e}")
                    return page_index, ""

            n_page_workers = min(MAX_VISION_WORKERS, len(scan_indices))
            with ThreadPoolExecutor(max_workers=n_page_workers) as page_pool:
                futs = {page_pool.submit(_ocr_one_page, idx): idx for idx in scan_indices}
                for fut in as_completed(futs):
                    try:
                        p_idx, text = fut.result()
                        if text:
                            merged_text[p_idx] = text
                    except Exception as e:
                        logging.error(f"페이지 OCR future 오류: {file_path} | {e}")
        elif scan_indices:
            # OCR 비활성화 → 스캔 페이지를 pending stub으로 기록
            for p_idx in scan_indices:
                merged_text[p_idx] = (
                    f"\n\n--- Page {p_idx + 1} (SCAN_PENDING_OCR) ---\n"
                    f"[VISUAL_CONTENT_PENDING_OCR] file_path={os.path.abspath(file_path)} page={p_idx+1} status=PENDING_OCR"
                )

        result = "".join(merged_text[i] for i in sorted(merged_text.keys()))
        return result if result.strip() else _ocr_unavailable_stub(file_path, "텍스트 레이어 없음")
    except Exception as e:
        logging.error(f"혼합 PDF 처리 실패: {file_path} | {e}")
        return task_vision_cloud_gpu(file_path, "pdf")


def _parse_unix_ms(ts):
    try:
        return datetime.utcfromtimestamp(int(ts) / 1000).isoformat() + "Z"
    except Exception:
        return ""


def _parse_iso(ts):
    if not ts:
        return ""
    return str(ts)


def is_google_timeline_file(file_path, ext):
    name = os.path.basename(file_path).lower()
    if ext != "json":
        return False
    markers = ["timeline", "semantic_location_history", "records", "location-history", "location_history"]
    return any(m in name for m in markers)


def extract_google_timeline_text(file_path):
    """Google Timeline JSON을 검색 친화 텍스트로 정규화합니다."""
    lines = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)

        # 1) legacy: locations[]
        locations = data.get("locations", []) if isinstance(data, dict) else []
        for loc in locations:
            ts = _parse_iso(loc.get("timestamp")) or _parse_unix_ms(loc.get("timestampMs"))
            lat = loc.get("latitudeE7")
            lng = loc.get("longitudeE7")
            lat_val = (lat / 1e7) if isinstance(lat, int) else ""
            lng_val = (lng / 1e7) if isinstance(lng, int) else ""
            line = f"TIMELINE_POINT ts={ts} lat={lat_val} lng={lng_val}"
            lines.append(line)

        # 2) semantic timelineObjects[]
        timeline_objects = data.get("timelineObjects", []) if isinstance(data, dict) else []
        for obj in timeline_objects:
            if "placeVisit" in obj:
                pv = obj.get("placeVisit", {})
                loc = pv.get("location", {})
                duration = pv.get("duration", {})
                start_ts = _parse_iso(duration.get("startTimestamp"))
                end_ts = _parse_iso(duration.get("endTimestamp"))
                line = (
                    f"PLACE_VISIT name={loc.get('name', '')} address={loc.get('address', '')} "
                    f"place_id={loc.get('placeId', '')} start={start_ts} end={end_ts}"
                )
                lines.append(line)

            if "activitySegment" in obj:
                seg = obj.get("activitySegment", {})
                duration = seg.get("duration", {})
                start_loc = seg.get("startLocation", {})
                end_loc = seg.get("endLocation", {})
                line = (
                    f"ACTIVITY_SEGMENT type={seg.get('activityType', '')} confidence={seg.get('confidence', '')} "
                    f"distance_m={seg.get('distance', '')} start={_parse_iso(duration.get('startTimestamp'))} "
                    f"end={_parse_iso(duration.get('endTimestamp'))} "
                    f"start_latE7={start_loc.get('latitudeE7', '')} start_lngE7={start_loc.get('longitudeE7', '')} "
                    f"end_latE7={end_loc.get('latitudeE7', '')} end_lngE7={end_loc.get('longitudeE7', '')}"
                )
                lines.append(line)

        text = "\n".join(lines).strip()
        if text:
            return text

        # 파싱 실패/비표준 포맷 대비 fallback
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Google Timeline 파싱 실패: {file_path} | {e}")
        return ""


def chunk_text(content, chunk_size=1200, overlap=200):
    if not content:
        return []

    text = content.strip()
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    step = max(1, chunk_size - overlap)
    text_len = len(text)

    while start < text_len:
        end = min(text_len, start + chunk_size)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start += step

    return chunks

# ─── Google Drive (GDFS) stub 감지 ───────────────────────────────────────────
_ATTR_RECALL_ON_DATA  = 0x00400000   # cloud-only, 접근 시 다운로드
_ATTR_RECALL_ON_OPEN  = 0x00040000   # cloud stub

def is_gdrive_stub(file_path: str) -> bool:
    """G: 경로 파일이 로컬 미다운로드(cloud-only) 상태인지 확인."""
    try:
        if not file_path.upper().startswith(GDRIVE_MOUNT.upper().rstrip("\\") + "\\"):
            return False
        attrs = ctypes.windll.kernel32.GetFileAttributesW(file_path)
        if attrs == 0xFFFFFFFF:
            return True   # INVALID_FILE_ATTRIBUTES → stub 또는 없음
        return bool(attrs & (_ATTR_RECALL_ON_DATA | _ATTR_RECALL_ON_OPEN))
    except Exception:
        try:
            return os.path.getsize(file_path) == 0
        except Exception:
            return True


def wait_for_gdrive_sync(file_path: str, timeout: int = 180) -> bool:
    """G: stub 파일이 로컬 다운로드 완료될 때까지 대기. 성공 True / 타임아웃 False."""
    if not is_gdrive_stub(file_path):
        return True
    # 파일 열기 시도로 GDFS 다운로드 트리거
    try:
        with open(file_path, 'rb') as _f:
            _f.read(1)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_gdrive_stub(file_path):
            return True
        time.sleep(5)
    return False


# ----------------------------------------------------
# 카테고리별 병렬 처리 워커 함수들
# ----------------------------------------------------
def task_text_cpu(file_path):
    """[카테고리 1] 순수 텍스트 / 문서 (로컬 CPU 전담)"""
    ext_lower = file_path.lower().rsplit('.', 1)[-1] if '.' in file_path else ''

    # PPTX: python-pptx 직접 추출 (이미지만 있는 슬라이드를 SimpleDirectoryReader가 빈 텍스트로 반환할 수 있음)
    if ext_lower == 'pptx':
        try:
            from pptx import Presentation
            prs = Presentation(file_path)
            parts = []
            for slide_idx, slide in enumerate(prs.slides, 1):
                slide_texts = []
                for shape in slide.shapes:
                    if hasattr(shape, "text_frame"):
                        for para in shape.text_frame.paragraphs:
                            t = para.text.strip()
                            if t:
                                slide_texts.append(t)
                    elif hasattr(shape, "text") and shape.text.strip():
                        slide_texts.append(shape.text.strip())
                if slide_texts:
                    parts.append(f"--- Slide {slide_idx} ---\n" + "\n".join(slide_texts))
            result = "\n\n".join(parts)
            if result.strip():
                return result
            # 텍스트 없는 PPTX(이미지/차트만) → pending stub
            return _ocr_unavailable_stub(file_path, "PPTX 비주얼 전용 — GCP VM OCR 대기")
        except Exception as e:
            logging.error(f"PPTX 직접 추출 실패: {file_path} | {e}")

    # CSV: SimpleDirectoryReader 대신 직접 텍스트 읽기 (실험 데이터 등)
    if ext_lower == 'csv':
        for enc in ("utf-8", "cp949", "utf-16", "latin-1"):
            try:
                with open(file_path, 'r', encoding=enc, errors='ignore') as f:
                    text = f.read()
                    if text and text.strip():
                        return text
            except Exception:
                continue
        return ""

    try:
        reader = SimpleDirectoryReader(input_files=[file_path])
        docs = reader.load_data()
        return "\n".join([doc.text for doc in docs])
    except Exception:
        for enc in ("utf-8", "cp949", "utf-16", "latin-1"):
            try:
                with open(file_path, 'r', encoding=enc, errors='ignore') as f:
                    text = f.read()
                    if text and text.strip():
                        return text
            except Exception:
                continue
    return ""

def task_media_local_gpu(file_path):
    """[카테고리 2] 오디오/비디오 (로컬 RTX 4060 Whisper 전담). VRAM 부족 시 시스템 RAM 자동 폴백."""
    try:
        result = whisper_model.transcribe(file_path, fp16=(device == "cuda"))["text"]
        if not result or not result.strip():
            # 무음/비주얼 전용 파일 → 메타데이터 stub으로 인덱싱
            return _ocr_unavailable_stub(file_path, "Whisper 트랜스크립션 결과 없음 (무음/비디오 전용)")
        return result
    except RuntimeError as e:
        if "out of memory" in str(e).lower() or "cuda" in str(e).lower():
            logging.warning(f"[Whisper OOM→CPU RAM] {file_path}: {e}")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            # 시스템 RAM으로 폴백 (fp16=False → CPU 연산)
            result = whisper_model.transcribe(file_path, fp16=False)["text"]
            return result if result.strip() else _ocr_unavailable_stub(file_path, "Whisper OOM 폴백 후 결과 없음")
        raise

def _ocr_unavailable_stub(file_path: str, note: str = "") -> str:
    """OCR 불가 시 파일 메타데이터를 텍스트로 저장 → 나중에 cloudVM OCR로 재처리 가능."""
    try:
        stat = os.stat(file_path)
        size_mb = stat.st_size / 1024 / 1024
        mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()
    except Exception:
        size_mb, mtime = 0, ""
    return (
        f"[VISUAL_CONTENT_PENDING_OCR]\n"
        f"file_path={os.path.abspath(file_path)}\n"
        f"file_name={os.path.basename(file_path)}\n"
        f"size_mb={size_mb:.2f}\n"
        f"mtime={mtime}\n"
        f"{('note=' + note) if note else 'note=GCP VM OCR 대기 중'}\n"
        f"status=PENDING_OCR"
    )


# ── GCP VM SSH 직접 OCR ─────────────────────────────────────────────────────────────────────────
# USE_GCP_SSH_OCR=true 시: 스캔PDF/이미지를 GCP VM에 잠접 전송 → GCP에서 변환+OCR
# 로컴 convert_from_path 없음 → RAM 절약 + G:드라이브 파일 한 번만 전송
_GCP_OCR_PY_SRC = """\
# -*- coding: utf-8 -*-
import sys, os, tempfile, ollama
fp    = sys.argv[1]
model = sys.argv[2]
dpi   = int(sys.argv[3]) if len(sys.argv) > 3 else 150
ext   = fp.rsplit('.', 1)[-1].lower()
prompt = "\uc774 \uc774\ubbf8\uc9c0\uc758 \ubaa8\ub4e0 \ud55c\uad6d\uc5b4/\uc601\uc5b4 \uc190\ud544\uae30, \uc218\uc2dd, \ub3c4\ud45c, \ucf54\ub4dc\ub97c \uc644\ubcbd\ud55c \ud14d\uc2a4\ud2b8\uc640 \ub9c8\ud06c\ub2e4\uc6b4 \uc218\uc2dd\uc73c\ub85c \ucd94\ucd9c\ud574. \ub2e4\ub978 \uc124\uba85 \uc5c6\uc774 \ub0b4\uc6a9\ub9cc \uc815\ud655\ud788 \ubca0\uae38 \uc368."
client  = ollama.Client()
results = []
temps   = []
try:
    if ext == 'pdf':
        from pdf2image import convert_from_path
        imgs = convert_from_path(fp, dpi=dpi, thread_count=2)
        for i, img in enumerate(imgs):
            tf = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            img.save(tf.name, 'JPEG', quality=80)
            temps.append(tf.name)
            tf.close()
            r = client.chat(model=model,
                            messages=[{'role': 'user', 'content': prompt, 'images': [tf.name]}],
                            keep_alive=0, options={'num_ctx': 4096, 'temperature': 0})
            results.append('--- Page ' + str(i + 1) + ' ---\\n' + r['message']['content'])
    else:
        r = client.chat(model=model,
                        messages=[{'role': 'user', 'content': prompt, 'images': [fp]}],
                        keep_alive=0, options={'num_ctx': 4096, 'temperature': 0})
        results.append(r['message']['content'])
finally:
    for t in temps:
        if os.path.exists(t):
            os.unlink(t)
    if os.path.exists(fp):
        os.unlink(fp)
print('\\n\\n'.join(results))
"""


def task_vision_via_gcp_ssh(file_path: str, ext: str) -> str:
    """파일을 GCP VM에 직접 전송 → GCP에서 PDF 변환+OCR → 결과 반환.
    로컴 convert_from_path 호출 없음 — RAM/CPU 절약."""
    pid_tag     = f"{os.getpid()}_{int(time.time())}"
    safe_name   = os.path.basename(file_path).replace(' ', '_').replace('(', '').replace(')', '')
    remote_file = f"/tmp/omni_{pid_tag}_{safe_name}"
    remote_py   = f"/tmp/omni_ocr_{pid_tag}.py"
    ssh_opts    = ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]
    ssh_base    = ["ssh", "-i", SSH_KEY_PATH] + ssh_opts + [f"{CLOUD_VM_USER}@{CLOUD_VM_IP}"]
    scp_base    = ["scp", "-i", SSH_KEY_PATH] + ssh_opts
    try:
        # 1) 원본 파일 → GCP (G:드라이브에서 한 번만 다운로드 후 SCP)
        subprocess.check_call(
            scp_base + [file_path, f"{CLOUD_VM_USER}@{CLOUD_VM_IP}:{remote_file}"],
            timeout=180, stderr=subprocess.DEVNULL
        )
        # 2) OCR Python 스크립트 → GCP (base64 인코딩 → 특수문자 문제 없음)
        b64 = _base64.b64encode(_GCP_OCR_PY_SRC.encode("utf-8")).decode("ascii")
        subprocess.check_call(
            ssh_base + [f"echo '{b64}' | base64 -d > {remote_py}"],
            timeout=20, stderr=subprocess.DEVNULL
        )
        # 3) GCP에서 OCR 실행 (PDF 변환 + Ollama 모두 GCP에서)
        out = subprocess.check_output(
            ssh_base + [f"python3 {remote_py} '{remote_file}' {VISION_MODEL} {DPI} 2>/dev/null"],
            timeout=600, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace").strip()
        _cloud_ocr_success()
        return out if out else _ocr_unavailable_stub(file_path, "GCP OCR 결과 없음")
    except subprocess.TimeoutExpired:
        logging.error(f"[GCP-SSH] 타임아웃: {file_path}")
        _cloud_ocr_failure(TimeoutError("SSH OCR timeout"))
        return _ocr_unavailable_stub(file_path, "GCP SSH 타임아웃 — 재실행 필요")
    except Exception as e:
        logging.error(f"[GCP-SSH] 실패: {file_path} | {e}")
        _cloud_ocr_failure(e)
        return _ocr_unavailable_stub(file_path, str(e)[:200])
    finally:
        try:
            subprocess.run(ssh_base + [f"rm -f {remote_file} {remote_py}"],
                           timeout=10, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ─── GCP VM + Google Drive API 직접 OCR ──────────────────────────────────────
# stub 파일(로컬 미다운로드) → Drive file ID 조회 → VM에서 Drive API로 직접 다운 후 OCR
_GCP_DRIVE_OCR_PY_SRC = """\
# -*- coding: utf-8 -*-
import sys, os, tempfile, base64
drive_file_id = sys.argv[1]
sa_key_path   = sys.argv[2]
model         = sys.argv[3]
dpi           = int(sys.argv[4]) if len(sys.argv) > 4 else 150
abbyy_b64     = sys.argv[5] if len(sys.argv) > 5 else ""
abbyy_ctx = base64.b64decode(abbyy_b64).decode("utf-8") if abbyy_b64 else ""

try:
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials
    from googleapiclient.http import MediaIoBaseDownload
    import io
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    creds  = Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    svc    = build("drive", "v3", credentials=creds)
    meta   = svc.files().get(fileId=drive_file_id, fields="name,mimeType").execute()
    name   = meta.get("name", "file")
    ext    = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
    tf = tempfile.NamedTemporaryFile(suffix="." + ext, delete=False)
    tf.close()
    req = svc.files().get_media(fileId=drive_file_id)
    with open(tf.name, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    fp = tf.name
except Exception as e:
    print(f"DRIVE_DOWNLOAD_ERROR: {e}", file=sys.stderr)
    sys.exit(1)

import ollama
if abbyy_ctx:
    prompt = ("ABBYY 추출 텍스트:\\n" + abbyy_ctx[:3000] +
              "\\n\\n위 내용을 참고하여 이 페이지에서 수식, 그래프, 손필기, 도표, 스캔 오류를 보완 추출해. 텍스트만 출력.")
else:
    prompt = "\\uc774 \\uc774\\ubbf8\\uc9c0\\uc758 \\ubaa8\\ub4e0 \\ud55c\\uad6d\\uc5b4/\\uc601\\uc5b4 \\uc190\\ud544\\uae30, \\uc218\\uc2dd, \\ub3c4\\ud45c, \\ucf54\\ub4dc\\ub97c \\uc644\\ubcbd\\ud55c \\ud14d\\uc2a4\\ud2b8\\uc640 \\ub9c8\\ud06c\\ub2e4\\uc6b4 \\uc218\\uc2dd\\uc73c\\ub85c \\ucd94\\ucd9c\\ud574. \\ub2e4\\ub978 \\uc124\\uba85 \\uc5c6\\uc774 \\ub0b4\\uc6a9\\ub9cc \\uc815\\ud655\\ud788 \\ubca0\\uae38 \\uc368."

client  = ollama.Client()
results = []
temps   = [fp]
try:
    if ext == "pdf":
        from pdf2image import convert_from_path
        imgs = convert_from_path(fp, dpi=dpi, thread_count=2)
        for i, img in enumerate(imgs):
            ptf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img.save(ptf.name, "JPEG", quality=80)
            temps.append(ptf.name)
            ptf.close()
            r = client.chat(model=model,
                            messages=[{"role": "user", "content": prompt, "images": [ptf.name]}],
                            keep_alive=0, options={"num_ctx": 4096, "temperature": 0})
            results.append("--- Page " + str(i + 1) + " ---\\n" + r["message"]["content"])
    else:
        r = client.chat(model=model,
                        messages=[{"role": "user", "content": prompt, "images": [fp]}],
                        keep_alive=0, options={"num_ctx": 4096, "temperature": 0})
        results.append(r["message"]["content"])
finally:
    for t in temps:
        try: os.unlink(t)
        except Exception: pass
print("\\n\\n".join(results))
"""


def _get_drive_creds():
    """OAuth token.json → SA key 순으로 Drive API 인증 객체 반환. 없으면 None."""
    if not GDRIVE_API_AVAILABLE:
        return None
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    # 1) OAuth token.json (우선)
    token_path = os.path.join(PROJECT_DIR, "token.json")
    if os.path.exists(token_path):
        try:
            from google.oauth2.credentials import Credentials as _OAuthCreds
            creds = _OAuthCreds.from_authorized_user_file(token_path, SCOPES)
            # 만료 시 자동 갱신
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request as _GReq
                creds.refresh(_GReq())
                with open(token_path, "w") as _tf:
                    _tf.write(creds.to_json())
            return creds
        except Exception as e:
            logging.warning(f"[DRIVE-API] token.json 로드 실패: {e}")
    # 2) SA key (폴백)
    if GOOGLE_SA_KEY_PATH and os.path.exists(GOOGLE_SA_KEY_PATH):
        try:
            return _SACredentials.from_service_account_file(GOOGLE_SA_KEY_PATH, scopes=SCOPES)
        except Exception as e:
            logging.warning(f"[DRIVE-API] SA key 로드 실패: {e}")
    return None


def _get_drive_file_id(file_path: str) -> Optional[str]:
    """로컬 G: 경로에 해당하는 Drive file ID를 Drive API로 조회. 실패 시 None."""
    creds = _get_drive_creds()
    if not creds:
        return None
    try:
        svc  = _gdrive_build("drive", "v3", credentials=creds)
        name = os.path.basename(file_path)
        q    = f"name = '{name.replace(chr(39), chr(8217))}' and trashed = false"
        res  = svc.files().list(q=q, fields="files(id,name,size)", pageSize=10).execute()
        files = res.get("files", [])
        if not files:
            return None
        # 크기가 0이 아닌 첫 번째 결과 선택
        for f in files:
            if int(f.get("size", 1)) > 0:
                return f["id"]
        return files[0]["id"]
    except Exception as e:
        logging.error(f"[DRIVE-API] file ID 조회 실패: {file_path} | {e}")
        return None


def _download_stub_via_drive_api(file_path: str) -> Optional[str]:
    """G: stub 파일을 Drive API로 로컬 임시파일에 다운로드. 반환값: temp 경로 or None.
    호출자가 반드시 temp 파일을 삭제해야 함."""
    creds = _get_drive_creds()
    if not creds:
        return None
    file_id = _get_drive_file_id(file_path)
    if not file_id:
        logging.warning(f"[DRIVE-DL] Drive file ID 없음: {os.path.basename(file_path)}")
        return None
    try:
        import io as _io
        svc = _gdrive_build("drive", "v3", credentials=creds)
        ext = os.path.splitext(file_path)[1] or ".bin"
        import tempfile as _tempfile
        tmp = _tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.close()
        req = svc.files().get_media(fileId=file_id)
        with open(tmp.name, "wb") as fh:
            dl = _MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
        size = os.path.getsize(tmp.name)
        if size == 0:
            os.unlink(tmp.name)
            logging.warning(f"[DRIVE-DL] 다운로드 결과 0바이트: {os.path.basename(file_path)}")
            return None
        logging.info(f"[DRIVE-DL] ✅ 로컬 다운로드 완료: {os.path.basename(file_path)} ({size//1024}KB)")
        return tmp.name
    except Exception as e:
        logging.error(f"[DRIVE-DL] 다운로드 실패: {file_path} | {e}")
        try:
            if 'tmp' in dir() and os.path.exists(tmp.name):
                os.unlink(tmp.name)
        except Exception:
            pass
        return None


def task_vision_via_gcp_ssh_drive(file_path: str, ext: str,
                                   abbyy_pages: Optional[Dict[int, str]] = None) -> str:
    """Drive file ID → GCP VM에서 직접 Drive 다운로드 후 OCR. (G: stub 파일 전용)"""
    drive_file_id = _get_drive_file_id(file_path)
    if not drive_file_id:
        # Drive API 실패 → stub 동기화 대기 후 SCP 방식 폴백
        logging.warning(f"[DRIVE-API] file ID 없음 → wait_for_gdrive_sync 폴백: {file_path}")
        if wait_for_gdrive_sync(file_path, timeout=120):
            return task_vision_via_gcp_ssh(file_path, ext)
        return _ocr_unavailable_stub(file_path, "Drive API + stub 동기화 모두 실패")

    abbyy_combined = "\n".join(abbyy_pages.values()) if abbyy_pages else ""
    abbyy_b64 = _base64.b64encode(abbyy_combined.encode("utf-8")).decode("ascii") if abbyy_combined else ""

    pid_tag   = f"{os.getpid()}_{int(time.time())}"
    remote_py = f"/tmp/omni_drive_ocr_{pid_tag}.py"
    sa_remote = f"/tmp/omni_sa_{pid_tag}.json"
    ssh_opts  = ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]
    ssh_base  = ["ssh", "-i", SSH_KEY_PATH] + ssh_opts + [f"{CLOUD_VM_USER}@{CLOUD_VM_IP}"]
    scp_base  = ["scp", "-i", SSH_KEY_PATH] + ssh_opts
    try:
        # SA 키 → VM (OCR 완료 후 삭제)
        subprocess.check_call(
            scp_base + [GOOGLE_SA_KEY_PATH, f"{CLOUD_VM_USER}@{CLOUD_VM_IP}:{sa_remote}"],
            timeout=30, stderr=subprocess.DEVNULL
        )
        # 스크립트 → VM
        b64 = _base64.b64encode(_GCP_DRIVE_OCR_PY_SRC.encode("utf-8")).decode("ascii")
        subprocess.check_call(
            ssh_base + [f"echo '{b64}' | base64 -d > {remote_py}"],
            timeout=20, stderr=subprocess.DEVNULL
        )
        out = subprocess.check_output(
            ssh_base + [
                f"python3 {remote_py} '{drive_file_id}' '{sa_remote}' {VISION_MODEL} {DPI} '{abbyy_b64}' 2>/dev/null"
            ],
            timeout=600, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace").strip()
        _cloud_ocr_success()
        return out if out else _ocr_unavailable_stub(file_path, "Drive OCR 결과 없음")
    except Exception as e:
        logging.error(f"[GCP-DRIVE-SSH] 실패: {file_path} | {e}")
        _cloud_ocr_failure(e)
        return _ocr_unavailable_stub(file_path, str(e)[:200])
    finally:
        try:
            subprocess.run(ssh_base + [f"rm -f {remote_py} {sa_remote}"],
                           timeout=10, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def task_vision_cloud_gpu(file_path: str, ext: str) -> str:
    """[카테고리 1] PDF/이미지 CV OCR (클라우드 GPU 전담)"""
    # OCR 완전 비활성화 시 메타데이터 stub으로 인덱싱 (GCP VM 켜면 재처리)
    if _CLOUD_DOWN.is_set() and _LOCAL_OCR_DOWN.is_set():
        return _ocr_unavailable_stub(file_path, "GCP VM 연결 후 재처리 예정")

    # GCP SSH 직접 처리 모드: 로컀 PDF 변환 없음 → RAM 절약
    if USE_GCP_SSH_OCR and not _CLOUD_DOWN.is_set():
        return task_vision_via_gcp_ssh(file_path, ext)

    text_content = ""
    try:
        if ext == 'pdf':
            images = convert_from_path(file_path, dpi=DPI, poppler_path=POPPLER_PATH, thread_count=2)
            for i, image in enumerate(images):
                text_content += _vision_ocr_single_image(image, f"Page {i + 1}", use_cloud=True)
                image.close()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            del images
            return text_content or _ocr_unavailable_stub(file_path, "OCR 결과 없음")

        with Image.open(file_path) as image:
            text_content = _vision_ocr_single_image(image, "Image", use_cloud=True)
        return text_content or _ocr_unavailable_stub(file_path, "OCR 결과 없음")
    except Exception as e:
        logging.error(f"Vision OCR 처리 실패: {file_path} | {e}")
        return _ocr_unavailable_stub(file_path, str(e)[:200])


def task_hwp_cpu(file_path):
    """[카테고리 3] HWP 바이너리 디코딩 (CPU 전담)"""
    content = extract_hwp_text_ole(file_path)
    if content.strip():
        return content
    return task_text_cpu(file_path)


# ─── ABBYY FineReader 16 COM 자동화 ─────────────────────────────────────────
def _filter_abbyy_garbage(text: str) -> str:
    """ABBYY 인식 오류/잔재 필터링 (특수문자 과다 줄, 단발 잔재 제거)."""
    if not text:
        return ""
    clean = []
    for line in text.splitlines():
        s = line.strip()
        if len(s) < 2:
            continue
        total = len(s)
        special = sum(1 for c in s if not c.isalnum() and c not in " .,!?:;-_()[]{}'\"/\\+=%<>@#^&*")
        if total > 0 and special / total > 0.45:
            continue
        clean.append(s)
    return "\n".join(clean)


def task_abbyy_ocr_pages(file_path: str) -> Dict[int, str]:
    """ABBYY FineReader 16 COM 자동화로 페이지별 텍스트 추출.
    실패 시 빈 dict 반환 (GCP SSH가 전담)."""
    if not ABBYY_ENABLED or not WIN32COM_AVAILABLE:
        return {}
    try:
        import pythoncom
        pythoncom.CoInitialize()  # ThreadPoolExecutor 워커 스레드에서 COM STA 초기화 필수
        app = _win32com.Dispatch("FineReader.Application.16")
        doc = app.OpenDocument(file_path)
        pages: Dict[int, str] = {}
        for i in range(doc.Pages.Count):
            page = doc.Pages.Item(i)
            page.Recognize()
            raw_text = page.GetText(0)   # 0 = plain text
            clean = _filter_abbyy_garbage(raw_text)
            if clean.strip():
                pages[i] = clean
        doc.Close(0)
        return pages
    except Exception as e:
        logging.error(f"[ABBYY] 실패: {file_path} | {e}")
        return {}
    finally:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass


def task_any_fallback(file_path):
    """알 수 없는 확장자도 최대한 학습 가능한 텍스트로 변환합니다."""
    if not (_CLOUD_DOWN.is_set() and _LOCAL_OCR_DOWN.is_set()):
        try:
            with Image.open(file_path) as img:
                result = _vision_ocr_single_image(img, "Unknown-Image", use_cloud=True)
                if result:
                    return result
        except Exception:
            pass

    text = task_text_cpu(file_path)
    if text.strip():
        return text

    try:
        stat = os.stat(file_path)
        return (
            f"[UNPARSED_BINARY_FILE]\n"
            f"file_path={os.path.abspath(file_path)}\n"
            f"size_bytes={stat.st_size}\n"
            f"mtime={datetime.fromtimestamp(stat.st_mtime).isoformat()}\n"
            f"note=텍스트 추출 불가한 바이너리 파일입니다. 원본 경로 기준으로 후속 멀티모달 검색에 사용됩니다."
        )
    except Exception:
        return f"[UNPARSED_BINARY_FILE] file_path={file_path}"


# ─── 메타데이터 자동 태깅 (OCR 텍스트 기반) ──────────────────────────────────
def extract_doc_metadata(file_path: str, content: str, page_idx: int) -> dict:
    """OCR 결과로 grade/subject/semester/exam_type/doc_type/parent_folder 추출."""
    meta = dict(grade="", subject="", semester="",
                exam_type="", doc_type="", parent_folder="")
    try:
        meta["parent_folder"] = os.path.basename(
            os.path.dirname(os.path.abspath(file_path)))
    except Exception:
        pass
    if not content:
        return meta

    grade_m = re.search(r'([1-3])\s*학년', content)
    if grade_m:
        meta["grade"] = grade_m.group(1) + "학년"

    sem_m = re.search(r'([1-2])\s*학기', content)
    if sem_m:
        meta["semester"] = sem_m.group(1) + "학기"

    for kw in ["중간고사", "기말고사", "모의고사", "수능", "내신", "수시", "정시"]:
        if kw in content:
            meta["exam_type"] = kw
            break

    subjects_map = {
        "수학":   ["수학", "미적분", "확률과통계", "기하", "대수"],
        "물리":   ["물리", "역학", "전자기"],
        "화학":   ["화학", "유기화학", "산염기"],
        "생물":   ["생물", "생태계", "유전", "세포"],
        "지구과학": ["지구과학", "천문"],
        "영어":   ["영어", "English", "Grammar"],
        "국어":   ["국어", "문학", "독서", "화법"],
        "정보":   ["정보", "알고리즘", "파이썬", "Python", "코딩"],
    }
    for subj, keywords in subjects_map.items():
        if any(kw in content for kw in keywords):
            meta["subject"] = subj
            break

    if re.search(r'PENDING_OCR|UNPARSED', content):
        meta["doc_type"] = "pending"
    elif re.search(r'[1-3]학년|학기|시험', content):
        meta["doc_type"] = "exam"
    else:
        meta["doc_type"] = "document"

    return meta


# ─── 연필 필기 보정 전처리 ──────────────────────────────────────────────────────
def _enhance_pencil_page(arr):  # -> np.ndarray
    """numpy grayscale uint8 배열 → 연필 필기 선명화.
    Auto-level(1% 클리핑) + gamma 보정으로 연필 마크(회색)→짙게, 배경(흰)→밝게."""
    import numpy as np
    arr = arr.astype(np.float32)
    lo, hi = float(np.percentile(arr, 1)), float(np.percentile(arr, 99))
    if hi > lo:
        arr = np.clip((arr - lo) / (hi - lo) * 255.0, 0.0, 255.0)
    # gamma 1.8: 어두운 픽셀(연필)은 더 어둡게, 밝은 픽셀(배경)은 더 밝게
    arr = (arr / 255.0) ** 1.8 * 255.0
    return arr.clip(0, 255).astype(np.uint8)


def _page_phash(arr) -> tuple:
    """8x8 평균 해시 (perceptual hash). 중복 페이지 감지용."""
    import numpy as np
    h, w = arr.shape
    # 8x8로 다운샘플 (단순 stride 슬라이싱)
    rh, rw = max(1, h // 8), max(1, w // 8)
    small = arr[::rh, ::rw][:8, :8]
    if small.shape != (8, 8):
        small = np.pad(small, ((0, 8 - small.shape[0]), (0, 8 - small.shape[1])), constant_values=128)
    mean = small.mean()
    return tuple((small > mean).flatten().tolist())


def _hamming(h1: tuple, h2: tuple) -> int:
    return sum(a != b for a, b in zip(h1, h2))


def _make_enhanced_tempfile(file_path: str, ext: str) -> Optional[str]:
    """이미지/스캔PDF → 연필 필기 보정 + 중복 페이지 제거된 임시 파일 반환.
    실패 시 None(원본 사용).
    - 단일 이미지: 보정만
    - PDF: 페이지별 보정 + 시각적 중복(phash 해밍거리<6) 제거
    """
    import numpy as np
    try:
        if ext in ('jpg', 'jpeg', 'png', 'bmp', 'webp', 'tiff', 'tif'):
            with Image.open(file_path) as img:
                gray = np.array(img.convert("L"))
                enhanced_arr = _enhance_pencil_page(gray)
                pil_out = Image.fromarray(enhanced_arr, "L")
                pil_out = ImageEnhance.Sharpness(pil_out).enhance(1.8)
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                pil_out.save(tmp.name, "PNG", dpi=(300, 300))
                tmp.close()
                return tmp.name

        elif ext == 'pdf' and PYMUPDF_AVAILABLE:
            src = fitz.open(file_path)
            dst = fitz.open()
            seen_hashes: list = []   # 이미 포함된 페이지 해시 목록
            skipped = 0

            for page in src:
                mat = fitz.Matrix(2, 2)  # 300 DPI 렌더
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()

                # ── 중복 감지: 기존 페이지와 해밍 거리 < 6이면 중복으로 판단 ──
                ph = _page_phash(arr)
                is_dup = any(_hamming(ph, prev) < 6 for prev in seen_hashes)
                if is_dup:
                    skipped += 1
                    continue
                seen_hashes.append(ph)

                # ── 보정 ──
                enhanced_arr = _enhance_pencil_page(arr)
                pil_out = Image.fromarray(enhanced_arr, "L")
                pil_out = ImageEnhance.Sharpness(pil_out).enhance(1.8)
                new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
                buf = io.BytesIO()
                pil_out.save(buf, "PNG")
                new_page.insert_image(new_page.rect, stream=buf.getvalue())

            src.close()
            if skipped:
                logging.info(f"[ENHANCE] 중복 페이지 {skipped}개 제거: {os.path.basename(file_path)}")
            if dst.page_count == 0:
                dst.close()
                return None  # 전부 중복이면 원본 사용
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            dst.save(tmp.name)
            dst.close()
            tmp.close()
            return tmp.name

    except Exception as _e:
        logging.warning(f"[ENHANCE] 전처리 실패 → 원본 사용: {file_path} | {_e}")
    return None


def _extract_pdf_chunk(src_path: str, start_page: int, end_page: int) -> Optional[str]:
    """src_path의 start_page..end_page-1 페이지를 임시 PDF로 추출. 실패 시 None."""
    if not PYMUPDF_AVAILABLE:
        return None
    try:
        src = fitz.open(src_path)
        dst = fitz.open()
        dst.insert_pdf(src, from_page=start_page, to_page=end_page - 1)
        src.close()
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        dst.save(tmp.name)
        dst.close()
        tmp.close()
        return tmp.name
    except Exception as e:
        logging.warning(f"[SPLIT] PDF 청크 추출 실패: {src_path}[{start_page}:{end_page}] | {e}")
        return None


def _parse_gcp_raw_to_pages(gcp_raw: str, page_offset: int) -> Dict[int, str]:
    """GCP OCR raw 텍스트(--- Page N --- 구분) → Dict[절대페이지인덱스, 텍스트]."""
    pages: Dict[int, str] = {}
    if not gcp_raw or gcp_raw.startswith("[VISUAL_CONTENT_PENDING"):
        return pages
    cur_page, cur_lines = 0, []
    for line in gcp_raw.splitlines():
        m = re.match(r"---\s*[Pp]age\s+(\d+)\s*---", line)
        if m:
            if cur_lines:
                pages[page_offset + cur_page] = "\n".join(cur_lines).strip()
            cur_page = int(m.group(1)) - 1
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_lines:
        pages[page_offset + cur_page] = "\n".join(cur_lines).strip()
    return pages


def _dispatch_cv_chunked(
    file_path: str, ext: str, abs_path: str,
    cpu_pool, gcp_pool, total_raw_pages: int,
) -> List[PageNode]:
    """대용량 PDF를 PDF_CHUNK_PAGES 단위로 분할 → 청크별 병렬 OCR → PageNode 합산.
    각 청크를 모두 미리 제출한 뒤 순서대로 결과를 수집하므로 최대한 병렬로 처리된다."""
    logging.info(
        f"[SPLIT] {os.path.basename(file_path)} {total_raw_pages}p → "
        f"{(total_raw_pages + PDF_CHUNK_PAGES - 1) // PDF_CHUNK_PAGES}개 청크 분할"
    )

    # 1) 청크 추출 + 퓨처 일괄 제출
    chunk_jobs = []  # (chunk_start, chunk_path, enhanced_path, abbyy_fut, gcp_fut)
    for chunk_start in range(0, total_raw_pages, PDF_CHUNK_PAGES):
        chunk_end  = min(chunk_start + PDF_CHUNK_PAGES, total_raw_pages)
        chunk_path = _extract_pdf_chunk(file_path, chunk_start, chunk_end)
        if chunk_path is None:
            continue
        enhanced_path = _make_enhanced_tempfile(chunk_path, "pdf")
        proc_path     = enhanced_path if enhanced_path else chunk_path

        abbyy_fut = cpu_pool.submit(task_abbyy_ocr_pages, proc_path) if cpu_pool else None
        gcp_fut   = None
        if USE_GCP_SSH_OCR and not _CLOUD_DOWN.is_set() and gcp_pool:
            # 청크 파일은 실제 로컬 파일이므로 Drive 경로 불필요
            gcp_fut = gcp_pool.submit(task_vision_via_gcp_ssh, proc_path, "pdf")

        chunk_jobs.append((chunk_start, chunk_path, enhanced_path, abbyy_fut, gcp_fut))
        logging.debug(
            f"[SPLIT]  └ 청크 제출 [{chunk_start}:{chunk_end}]: {os.path.basename(proc_path)}"
        )

    # 2) 결과 수집 (chunk_jobs 순서 = 제출 순서이므로 앞 청크가 먼저 완료됨)
    abbyy_pages: Dict[int, str] = {}
    gcp_pages:   Dict[int, str] = {}
    tmp_files: List[str] = []

    for (chunk_start, chunk_path, enhanced_path, abbyy_fut, gcp_fut) in chunk_jobs:
        tmp_files.append(chunk_path)
        if enhanced_path:
            tmp_files.append(enhanced_path)

        if abbyy_fut is not None:
            try:
                for k, v in abbyy_fut.result(timeout=300).items():
                    abbyy_pages[chunk_start + k] = v
            except Exception as e:
                logging.error(f"[ABBYY-CHUNK] {file_path}[{chunk_start}] | {e}")

        if gcp_fut is not None:
            try:
                gcp_raw = gcp_fut.result(timeout=600)
                gcp_pages.update(_parse_gcp_raw_to_pages(gcp_raw, chunk_start))
            except Exception as e:
                logging.error(f"[GCP-CHUNK] {file_path}[{chunk_start}] | {e}")

    # 3) 임시 파일 정리
    for tp in tmp_files:
        try:
            os.unlink(tp)
        except Exception:
            pass

    total_pages   = max(
        max(abbyy_pages.keys(), default=-1) + 1,
        max(gcp_pages.keys(),   default=-1) + 1,
        total_raw_pages,
    )
    combined_text = "\n".join(list(abbyy_pages.values()) + list(gcp_pages.values()))
    meta          = extract_doc_metadata(file_path, combined_text, 0)

    nodes: List[PageNode] = []
    for p_idx in range(total_pages):
        text_str   = abbyy_pages.get(p_idx, "")
        visual_str = gcp_pages.get(p_idx, "")
        tid = f"{abs_path}::page_{p_idx}::text"
        vid = f"{abs_path}::page_{p_idx}::visual"
        if text_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="text", text=text_str,
                parent_doc_id=abs_path,
                sibling_page_id=vid if visual_str else None,
                **meta,
            ))
        if visual_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="visual", text=visual_str,
                parent_doc_id=abs_path,
                sibling_page_id=tid if text_str else None,
                **meta,
            ))
        if not text_str and not visual_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="mixed",
                text=_ocr_unavailable_stub(file_path, f"page {p_idx+1} OCR 결과 없음"),
                parent_doc_id=abs_path,
                **meta,
            ))
    return nodes


# ─── CV 병렬 조합 (ABBYY CPU + GCP SSH GPU 동시) ─────────────────────────────
def dispatch_cv_combined(file_path: str, ext: str,
                          is_stub: bool = False,
                          doc_id_override: str = None) -> List[PageNode]:
    """ABBYY(cpu_pool) + GCP SSH(gcp_pool) 병렬 실행 → PageNode 리스트.
    doc_id_override: Drive API로 temp 파일 다운받았을 때 원본 경로를 parent_doc_id로 유지."""
    abs_path = os.path.abspath(doc_id_override if doc_id_override else file_path)
    cpu_pool = _cpu_pool_ref[0]
    gcp_pool = _gcp_pool_ref[0]

    # PDF 페이지 수 확인 — PDF_CHUNK_PAGES 초과 시 청크 분할 처리
    if ext == "pdf" and PYMUPDF_AVAILABLE:
        try:
            _probe     = fitz.open(file_path)
            _pdf_pages = _probe.page_count
            _probe.close()
        except Exception:
            _pdf_pages = 0
        if _pdf_pages > PDF_CHUNK_PAGES:
            return _dispatch_cv_chunked(file_path, ext, abs_path, cpu_pool, gcp_pool, _pdf_pages)

    # 연필 필기 보정 전처리 (스캔 이미지/PDF)
    _enhanced_tmp = _make_enhanced_tempfile(file_path, ext)
    _proc_path = _enhanced_tmp if _enhanced_tmp else file_path
    if _enhanced_tmp:
        logging.info(f"[ENHANCE] 필기 보정 적용: {os.path.basename(file_path)}")

    # ABBYY 작업 제출 (CPU) — 보정 파일 사용
    abbyy_fut = cpu_pool.submit(task_abbyy_ocr_pages, _proc_path) if cpu_pool else None

    # GCP SSH 작업 제출 (GPU) — 보정 파일 사용
    gcp_fut = None
    if USE_GCP_SSH_OCR and not _CLOUD_DOWN.is_set() and gcp_pool:
        if is_stub and GDRIVE_API_AVAILABLE and _get_drive_creds() is not None:
            gcp_fut = gcp_pool.submit(task_vision_via_gcp_ssh_drive, _proc_path, ext, None)
        else:
            gcp_fut = gcp_pool.submit(task_vision_via_gcp_ssh, _proc_path, ext)

    # ABBYY 결과 수집
    abbyy_pages: Dict[int, str] = {}
    if abbyy_fut is not None:
        try:
            abbyy_pages = abbyy_fut.result(timeout=300)
        except Exception as e:
            logging.error(f"[ABBYY-FUT] {file_path} | {e}")

    # GCP 결과 수집
    gcp_raw = ""
    if gcp_fut is not None:
        try:
            gcp_raw = gcp_fut.result(timeout=600)
        except Exception as e:
            logging.error(f"[GCP-FUT] {file_path} | {e}")

    # GCP raw → Dict[int, str]
    gcp_pages: Dict[int, str] = _parse_gcp_raw_to_pages(gcp_raw, 0)

    total_pages = max(
        max(abbyy_pages.keys(), default=-1) + 1,
        max(gcp_pages.keys(), default=-1) + 1,
        1,
    )
    combined_text = "\n".join(list(abbyy_pages.values()) + list(gcp_pages.values()))
    meta = extract_doc_metadata(file_path, combined_text, 0)

    # 임시 파일 정리
    if _enhanced_tmp:
        try:
            os.unlink(_enhanced_tmp)
        except Exception:
            pass

    nodes: List[PageNode] = []
    for p_idx in range(total_pages):
        text_str   = abbyy_pages.get(p_idx, "")
        visual_str = gcp_pages.get(p_idx, "")
        tid = f"{abs_path}::page_{p_idx}::text"
        vid = f"{abs_path}::page_{p_idx}::visual"

        if text_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="text", text=text_str,
                parent_doc_id=abs_path,
                sibling_page_id=vid if visual_str else None,
                **meta,
            ))
        if visual_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="visual", text=visual_str,
                parent_doc_id=abs_path,
                sibling_page_id=tid if text_str else None,
                **meta,
            ))
        if not text_str and not visual_str:
            nodes.append(PageNode(
                page_idx=p_idx, total_pages=total_pages,
                element_type="mixed",
                text=_ocr_unavailable_stub(file_path, f"page {p_idx+1} OCR 결과 없음"),
                parent_doc_id=abs_path,
                **meta,
            ))
    return nodes


def _write_page_nodes(file_path: str, file_name: str, ext: str,
                       nodes: List[PageNode], pbar) -> None:
    """PageNode 리스트 → 청킹 → Document → Qdrant 저장."""
    if not nodes:
        pbar.write(f"⚠️  건너뜀(노드 없음): {file_name}")
        pbar.update(1)
        return
    abs_path = os.path.abspath(file_path)
    docs = []
    for node in nodes:
        if not node.text or not node.text.strip():
            continue
        chunks = chunk_text(node.text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        for k, chunk in enumerate(chunks):
            doc_id = f"{abs_path}::page_{node.page_idx}::{node.element_type}::chunk_{k}"
            docs.append(Document(
                text=chunk,
                doc_id=doc_id,
                metadata={
                    "file_name":        file_name,
                    "file_path":        abs_path,
                    "extension":        ext,
                    "chunk_index":      k,
                    "total_chunks":     len(chunks),
                    "page_index":       node.page_idx,
                    "total_pages":      node.total_pages,
                    "element_type":     node.element_type,
                    "parent_doc_id":    node.parent_doc_id,
                    "sibling_page_id":  node.sibling_page_id or "",
                    "grade":            node.grade,
                    "subject":          node.subject,
                    "semester":         node.semester,
                    "exam_type":        node.exam_type,
                    "doc_type":         node.doc_type,
                    "parent_folder":    node.parent_folder,
                },
            ))
    if not docs:
        pbar.write(f"⚠️  건너뜀(추출 결과 없음): {file_name}")
        pbar.update(1)
        return
    _embed = embed_model
    for attempt in range(2):
        try:
            VectorStoreIndex.from_documents(
                docs, storage_context=storage_context, embed_model=_embed
            )
            break
        except RuntimeError as oom:
            if ("out of memory" in str(oom).lower() or "cuda" in str(oom).lower()) and attempt == 0:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                _embed = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME, device="cpu")
            else:
                logging.error(f"[OOM] _write_page_nodes 실패: {file_name} | {oom}")
                log_file_failure(file_path, str(oom))
                pbar.update(1)
                return
    state_conn.execute(
        "INSERT OR REPLACE INTO processed (file_path) VALUES (?)", (file_path,)
    )
    state_conn.commit()
    total_pages = nodes[-1].total_pages if nodes else 0
    pbar.write(f"✅ {file_name} | pages={total_pages} nodes={len(nodes)} docs={len(docs)}")
    pbar.update(1)


# 임베딩 & DB 저장 전담 쓰레드 (로컬 GPU OOM 방지 및 안전성 보장)
def db_writer_worker(pbar):
    while True:
        item = result_queue.get()
        if item is None:
            break  # 종료 시그널

        # PAGE_NODES 형식: (file_path, "PAGE_NODES", file_name, ext, nodes)
        if len(item) == 5 and item[1] == "PAGE_NODES":
            _fp, _, _fn, _ext, _nodes = item
            _write_page_nodes(_fp, _fn, _ext, _nodes, pbar)
            result_queue.task_done()
            continue

        file_path, content, file_name, ext = item

        # 추출 단계 실패 파일: 로그 남기고 즉시 건너뜀
        if not content or content.startswith("❌"):
            err = content or "빈 콘텐츠"
            logging.error(f"[SKIP 추출실패] {file_name} | {err}")
            log_file_failure(file_path, err)
            pbar.write(f"⚠️  건너뜀(추출실패): {file_name}")
            pbar.update(1)
            result_queue.task_done()
            continue

        try:
            abs_path = os.path.abspath(file_path)
            chunks = chunk_text(content, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
            total_chunks = len(chunks)
            docs = [
                Document(
                    text=chunk,
                    doc_id=f"{abs_path}::chunk_{idx}",
                    metadata={
                        "file_name": file_name,
                        "file_path": abs_path,
                        "extension": ext,
                        "chunk_index": idx,
                        "total_chunks": total_chunks,
                    },
                )
                for idx, chunk in enumerate(chunks)
            ]

            # 임베딩: CUDA OOM 발생 시 캐시 비우고 CPU로 자동 폴백
            _embed = embed_model
            for attempt in range(2):
                try:
                    VectorStoreIndex.from_documents(
                        docs, storage_context=storage_context, embed_model=_embed
                    )
                    break
                except RuntimeError as oom:
                    oom_msg = str(oom).lower()
                    if ("out of memory" in oom_msg or "cuda" in oom_msg) and attempt == 0:
                        logging.error(f"[OOM 감지] {file_name} → CPU 폴백 시도")
                        pbar.write(f"🔁 OOM → CPU 폴백: {file_name}")
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                        _embed = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME, device="cpu")
                    else:
                        raise  # 2번째 시도도 실패하면 상위 except로

            state_conn.execute(
                "INSERT OR REPLACE INTO processed (file_path) VALUES (?)", (file_path,)
            )
            state_conn.commit()
            pbar.write(f"✅ {file_name} | chunks={total_chunks}")

        except Exception as e:
            logging.error(f"[SKIP 임베딩실패] {file_name} | {e}")
            pbar.write(f"⚠️  건너뜀(임베딩실패): {file_name} | {e}")
            log_file_failure(file_path, str(e))
            # 실패해도 파이프라인 절대 중단 안 함

        pbar.update(1)
        result_queue.task_done()
    
# ==========================================
# 🚀 [메인] 스마트 라우터 분산 파이프라인
# ==========================================
if __name__ == "__main__":
    if CLOUD_OLLAMA_URL == "http://YOUR_CLOUD_IP:11434":
        print("⚠️ CLOUD_OLLAMA_URL이 기본값입니다. 실제 클라우드 Ollama 주소로 변경하세요.")

    INPUT_ROOT = resolve_input_root()
    if not os.path.isdir(INPUT_ROOT):
        print(f"❌ 입력 폴더를 찾을 수 없습니다: {INPUT_ROOT}")
        print("사용법: python 1_ingest_data.py \"<분석할_폴더_경로>\"")
        exit(1)

    print(f"\n📂 '{INPUT_ROOT}' 스캔 중... 잠시만 기다려주세요.")
    all_files = []
    for root, dirs, files in os.walk(INPUT_ROOT):
        for file in files:
            all_files.append(os.path.join(root, file))

    # 초고속 중복 데이터 로드
    print("⏳ 기존 학습 상태를 로드합니다 (SQLite 캐시)...")
    existing_ids = set(row[0] for row in state_conn.execute("SELECT file_path FROM processed"))

    pending_files = [f for f in all_files if f not in existing_ids]
    print(f"🔍 총 {len(all_files)}개 파일 중, 신규 처리 대상: {len(pending_files)}개")
    print(f"📊 로그 파일 위치: {LOG_FILE}\n")

    pbar = tqdm(total=len(pending_files), desc="Omni-Brain 병렬 학습 진행률", unit="file", dynamic_ncols=True)

    # 임베딩/DB 기록 전담 쓰레드 시작
    db_thread = threading.Thread(target=db_writer_worker, args=(pbar,))
    db_thread.start()

    # 주기적 백업 쓰레드 시작
    backup_thread = threading.Thread(target=backup_worker, daemon=True)
    backup_thread.start()

    # 병렬 처리 4-pool Executor
    # cpu_pool   : 텍스트/HWP/ABBYY
    # gcp_pool   : GCP SSH OCR (I/O bound, 넉넉히)
    # audio_pool : Whisper (VRAM 제한 → 1개)
    # coord_pool : CV 코디네이터 (ABBYY+GCP 동시 제출)
    with ThreadPoolExecutor(max_workers=MAX_CPU_WORKERS)    as cpu_pool, \
         ThreadPoolExecutor(max_workers=MAX_GCP_WORKERS)    as gcp_pool, \
         ThreadPoolExecutor(max_workers=MAX_AUDIO_WORKERS)  as audio_pool, \
         ThreadPoolExecutor(max_workers=MAX_COORD_WORKERS)  as coord_pool:

        _cpu_pool_ref[0] = cpu_pool
        _gcp_pool_ref[0] = gcp_pool

        def dispatch_and_queue(file_path):
            file_name = os.path.basename(file_path)
            ext = file_name.lower().split('.')[-1]
            content = None

            try:
                if not os.path.isfile(file_path):
                    raise FileNotFoundError(f"파일 없음: {file_path}")

                INGESTION_PAUSED.wait()

                stub = is_gdrive_stub(file_path)

                if ext in ['txt', 'md', 'csv', 'py', 'json', 'html', 'xml',
                           'log', 'ini', 'docx', 'pptx', 'xlsx']:
                    # G: stub이면 Drive API 다운로드 → sync 대기 → 포기 순으로 폴백
                    if stub:
                        tmp = _download_stub_via_drive_api(file_path)
                        if tmp:
                            try:
                                content = extract_google_timeline_text(tmp) if is_google_timeline_file(file_path, ext) else task_text_cpu(tmp)
                            finally:
                                try: os.unlink(tmp)
                                except Exception: pass
                            stub = False
                        elif not wait_for_gdrive_sync(file_path, timeout=60):
                            content = _ocr_unavailable_stub(file_path, "G: 텍스트 파일 동기화 타임아웃 + Drive API 실패")
                        else:
                            stub = False
                    if not stub and content is None:
                        if is_google_timeline_file(file_path, ext):
                            content = extract_google_timeline_text(file_path)
                        else:
                            content = task_text_cpu(file_path)

                elif ext in ['m4a', 'mp3', 'mp4', 'wav', 'flac', 'avi', 'mkv']:
                    if stub:
                        tmp = _download_stub_via_drive_api(file_path)
                        if tmp:
                            try:
                                content = task_media_local_gpu(tmp)
                            finally:
                                try: os.unlink(tmp)
                                except Exception: pass
                        elif not wait_for_gdrive_sync(file_path, timeout=60):
                            content = _ocr_unavailable_stub(file_path, "G: 오디오 동기화 타임아웃 + Drive API 실패")
                        else:
                            content = task_media_local_gpu(file_path)
                    else:
                        content = task_media_local_gpu(file_path)

                elif ext == 'pdf':
                    if stub:
                        # stub PDF: Drive API 로컬 다운로드 우선
                        tmp = _download_stub_via_drive_api(file_path)
                        if tmp:
                            try:
                                nodes = dispatch_cv_combined(tmp, ext, is_stub=False, doc_id_override=file_path)
                            finally:
                                try: os.unlink(tmp)
                                except Exception: pass
                        else:
                            nodes = dispatch_cv_combined(file_path, ext, is_stub=True)
                        result_queue.put((file_path, "PAGE_NODES", file_name, ext, nodes))
                        return
                    elif not is_text_pdf(file_path):
                        # 스캔 PDF (로컬 파일) → ABBYY + GCP 병렬
                        nodes = dispatch_cv_combined(file_path, ext, is_stub=False)
                        result_queue.put((file_path, "PAGE_NODES", file_name, ext, nodes))
                        return
                    else:
                        content = task_text_cpu(file_path)

                elif ext in ['jpg', 'jpeg', 'png', 'bmp', 'webp', 'tiff']:
                    if stub:
                        tmp = _download_stub_via_drive_api(file_path)
                        if tmp:
                            try:
                                nodes = dispatch_cv_combined(tmp, ext, is_stub=False, doc_id_override=file_path)
                            finally:
                                try: os.unlink(tmp)
                                except Exception: pass
                        else:
                            nodes = dispatch_cv_combined(file_path, ext, is_stub=True)
                    else:
                        nodes = dispatch_cv_combined(file_path, ext, is_stub=False)
                    result_queue.put((file_path, "PAGE_NODES", file_name, ext, nodes))
                    return

                elif ext in ['hwp', 'hwpx']:
                    if stub:
                        tmp = _download_stub_via_drive_api(file_path)
                        if tmp:
                            try:
                                content = task_hwp_cpu(tmp)
                            finally:
                                try: os.unlink(tmp)
                                except Exception: pass
                        elif not wait_for_gdrive_sync(file_path, timeout=60):
                            content = _ocr_unavailable_stub(file_path, "G: HWP 동기화 타임아웃 + Drive API 실패")
                        else:
                            content = task_hwp_cpu(file_path)
                    else:
                        content = task_hwp_cpu(file_path)

                else:
                    content = task_any_fallback(file_path)

                if not content or not content.strip():
                    logging.error(f"[EMPTY] 추출 결과 없음: {file_path}")
                    content = f"❌ EMPTY: {file_path}"

            except Exception as e:
                logging.error(f"[ERROR] 파일 처리 실패(건너뜀): {file_path} | {e}")
                content = f"❌ ERROR: {e}"

            result_queue.put((file_path, content, file_name, ext))

        # 작업 쏟아붓기 (Non-blocking)
        futures = []
        for file_path in pending_files:
            ext = file_path.lower().split('.')[-1]
            stub = is_gdrive_stub(file_path)
            if ext in ['m4a', 'mp3', 'mp4', 'wav', 'flac', 'avi', 'mkv']:
                futures.append(audio_pool.submit(dispatch_and_queue, file_path))
            elif ext in ['jpg', 'jpeg', 'png', 'bmp', 'webp', 'tiff']:
                # CV 파일 → coord_pool (내부에서 cpu_pool + gcp_pool 동시 submit)
                futures.append(coord_pool.submit(dispatch_and_queue, file_path))
            elif ext == 'pdf':
                if stub:
                    futures.append(coord_pool.submit(dispatch_and_queue, file_path))
                else:
                    futures.append(cpu_pool.submit(dispatch_and_queue, file_path))
            elif ext in ['hwp', 'hwpx']:
                futures.append(cpu_pool.submit(dispatch_and_queue, file_path))
            else:
                futures.append(cpu_pool.submit(dispatch_and_queue, file_path))

    # 모든 작업이 큐에 들어가고 처리 완료될 때까지 대기
    result_queue.join()
    
    # DB 기록 쓰레드에 종료 시그널 전송
    result_queue.put(None)
    db_thread.join()

    backup_stop_event.set()
    backup_vector_db(reason="final")

    pbar.close()
    state_conn.close()
    print(f"\n💾 자동 백업 경로: {BACKUP_ROOT}")
    print("🛑 GCP 무료 크레딧 절약을 위해 작업 종료 후 VM 중지를 권장합니다.")
    print(f"🎉 모든 작업이 완료되었습니다! 이제 2_run_agents.py를 실행하세요.")