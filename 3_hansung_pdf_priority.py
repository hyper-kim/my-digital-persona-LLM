# -*- coding: utf-8 -*-
"""
3_hansung_pdf_priority.py
━━━━━━━━━━━━━━━━━━━━━━━━
한성과고 스캔 PDF 우선 재처리 스크립트.

동작:
  1. 한성과고 하위 모든 PDF 수집 (is_text_pdf 제외)
  2. SQLite processed 테이블에서 해당 경로 삭제
  3. Qdrant에서 해당 file_path 보유 벡터 삭제 (parent_doc_id 또는 file_path 매칭)
  4. ABBYY(로컬 CPU) + GCP SSH(GPU) 병렬 → PageNode → Qdrant + SQLite 저장

실행:
  python -X utf8 3_hansung_pdf_priority.py
  python -X utf8 3_hansung_pdf_priority.py "G:\\내 드라이브\\특정폴더"
"""

import os
import sys
import time
import sqlite3
import logging
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from dataclasses import dataclass
import ctypes

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 환경 로드 ──────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PROJECT_DIR  = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH  = os.getenv("QDRANT_PATH",  os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL   = os.getenv("QDRANT_URL", "")
STATE_DB     = os.getenv("STATE_DB_PATH", os.path.join(PROJECT_DIR, "processed_files.db"))
LOG_FILE     = os.getenv("LOG_FILE",     os.path.join(PROJECT_DIR, "ingestion_log.txt"))

# 기본 한성과고 루트 (CLI 인자로 덮어쓰기 가능)
DEFAULT_ROOT = os.getenv("RAW_DATA_DIR", r"G:\내 드라이브") + r"\한성과고"
INPUT_ROOT   = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_ROOT

COLLECTION   = "omni_persona_v3"
EMBED_MODEL  = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-small")
CHUNK_SIZE   = int(os.getenv("CHUNK_SIZE",   "1200"))
CHUNK_OVERLAP= int(os.getenv("CHUNK_OVERLAP","200"))
DPI          = int(os.getenv("DPI", "150"))
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:8b")
ABBYY_ENABLED = os.getenv("ABBYY_ENABLED", "false").lower() == "true"
MAX_GCP_WORKERS   = int(os.getenv("MAX_GCP_WORKERS",   "4"))
MAX_COORD_WORKERS = int(os.getenv("MAX_COORD_WORKERS", "4"))
MAX_CPU_WORKERS   = int(os.getenv("MAX_CPU_WORKERS",   str(max(1, (os.cpu_count() or 4)))))
GOOGLE_SA_KEY_PATH = os.getenv("GOOGLE_SA_KEY_PATH", "")
GDRIVE_MOUNT  = os.getenv("GDRIVE_MOUNT", r"G:\\")
CLOUD_VM_IP   = os.getenv("CLOUD_VM_IP",   "35.211.58.231")
CLOUD_VM_USER = os.getenv("CLOUD_VM_USER", "kjy")
SSH_KEY_PATH  = os.getenv("SSH_KEY_PATH",  r"C:\Users\kjy\.ssh\gcp_key_fixed")
USE_GCP_SSH_OCR = os.getenv("USE_GCP_SSH_OCR", "false").lower() == "true"
POPPLER_PATH = os.getenv("POPPLER_PATH", "") or None

logging.basicConfig(filename=LOG_FILE, level=logging.ERROR,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# ── 필수 라이브러리 임포트 ──────────────────────────────────────────────────────
import torch
import qdrant_client
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchText
from llama_index.core import Document, VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
import base64 as _base64
import subprocess
import tempfile
import httpx

try:
    import fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    import win32com.client as _win32com
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False

try:
    from googleapiclient.discovery import build as _gdrive_build
    from google.oauth2.service_account import Credentials as _SACredentials
    GDRIVE_API_AVAILABLE = True
except ImportError:
    GDRIVE_API_AVAILABLE = False

# ── 공통 유틸 임포트 (1_ingest_data 재사용) ───────────────────────────────────
# 1_ingest_data의 함수들을 직접 임포트해서 사용 (중복 구현 방지)
sys.path.insert(0, PROJECT_DIR)
from importlib import import_module

# 엔진 초기화
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[시스템] {device.upper()} 가속 활성화")
print("AI 임베딩 모델 로딩 중...")
embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL, device=device)

print("Qdrant 연결 중...")
qclient = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
vector_store = QdrantVectorStore(client=qclient, collection_name=COLLECTION)
storage_context = StorageContext.from_defaults(vector_store=vector_store)

state_conn = sqlite3.connect(STATE_DB, check_same_thread=False)
state_conn.execute("PRAGMA journal_mode=WAL")
state_conn.execute("PRAGMA synchronous=NORMAL")

# pool 참조
_cpu_pool_ref: List = [None]
_gcp_pool_ref: List = [None]
_CLOUD_DOWN = threading.Event()


# ── 유틸 함수 (1_ingest_data에서 가져온 것들) ──────────────────────────────────
def chunk_text(content, chunk_size=1200, overlap=200):
    if not content:
        return []
    text = content.strip()
    if len(text) <= chunk_size:
        return [text]
    chunks, start = [], 0
    step = max(1, chunk_size - overlap)
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += step
    return chunks


def is_text_pdf(file_path):
    if not PYMUPDF_AVAILABLE:
        return False
    try:
        doc = fitz.open(file_path)
        text_len = sum(len(p.get_text("text")) for p in doc)
        return (text_len / max(len(doc), 1)) > 100
    except Exception:
        return False


_ATTR_RECALL_ON_DATA = 0x00400000
_ATTR_RECALL_ON_OPEN = 0x00040000

def is_gdrive_stub(file_path: str) -> bool:
    try:
        if not file_path.upper().startswith(GDRIVE_MOUNT.upper().rstrip("\\") + "\\"):
            return False
        attrs = ctypes.windll.kernel32.GetFileAttributesW(file_path)
        if attrs == 0xFFFFFFFF:
            return True
        return bool(attrs & (_ATTR_RECALL_ON_DATA | _ATTR_RECALL_ON_OPEN))
    except Exception:
        try:
            return os.path.getsize(file_path) == 0
        except Exception:
            return True


def _ocr_unavailable_stub(file_path, note=""):
    try:
        stat = os.stat(file_path)
        size_mb = stat.st_size / 1024 / 1024
        mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))
    except Exception:
        size_mb, mtime = 0, ""
    return (
        f"[VISUAL_CONTENT_PENDING_OCR]\n"
        f"file_path={os.path.abspath(file_path)}\n"
        f"file_name={os.path.basename(file_path)}\n"
        f"size_mb={size_mb:.2f}\n"
        f"mtime={mtime}\n"
        f"note={note or 'GCP VM OCR 대기 중'}\n"
        f"status=PENDING_OCR"
    )


def _filter_abbyy_garbage(text: str) -> str:
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
    if not ABBYY_ENABLED or not WIN32COM_AVAILABLE:
        return {}
    if os.path.splitext(file_path)[1].lower() != ".pdf":
        return {}
    try:
        app = _win32com.Dispatch("FineReader.Application.16")
        doc = app.OpenDocument(file_path)
        pages: Dict[int, str] = {}
        for i in range(doc.Pages.Count):
            page = doc.Pages.Item(i)
            page.Recognize()
            raw = page.GetText(0)
            clean = _filter_abbyy_garbage(raw)
            if clean.strip():
                pages[i] = clean
        doc.Close(0)
        return pages
    except Exception as e:
        logging.error(f"[ABBYY] {file_path} | {e}")
        return {}


# ── GCP SSH OCR 인라인 스크립트 재사용 ────────────────────────────────────────
# 1_ingest_data 와 동일한 스크립트
_GCP_OCR_PY_SRC = """\
# -*- coding: utf-8 -*-
import sys, os, tempfile, ollama
fp    = sys.argv[1]
model = sys.argv[2]
dpi   = int(sys.argv[3]) if len(sys.argv) > 3 else 150
ext   = fp.rsplit('.', 1)[-1].lower()
prompt = "\\uc774 \\uc774\\ubbf8\\uc9c0\\uc758 \\ubaa8\\ub4e0 \\ud55c\\uad6d\\uc5b4/\\uc601\\uc5b4 \\uc190\\ud544\\uae30, \\uc218\\uc2dd, \\ub3c4\\ud45c, \\ucf54\\ub4dc\\ub97c \\uc644\\ubcbd\\ud55c \\ud14d\\uc2a4\\ud2b8\\uc640 \\ub9c8\\ud06c\\ub2e4\\uc6b4 \\uc218\\uc2dd\\uc73c\\ub85c \\ucd94\\ucd9c\\ud574. \\ub2e4\\ub978 \\uc124\\uba85 \\uc5c6\\uc774 \\ub0b4\\uc6a9\\ub9cc \\uc815\\ud655\\ud788 \\ubca0\\uae38 \\uc368."
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
        if os.path.exists(t): os.unlink(t)
    if os.path.exists(fp):    os.unlink(fp)
print('\\n\\n'.join(results))
"""

def task_gcp_ssh_ocr(file_path: str) -> str:
    pid_tag   = f"{os.getpid()}_{int(time.time())}"
    safe_name = os.path.basename(file_path).replace(' ', '_').replace('(', '').replace(')', '')
    remote_file = f"/tmp/omni_{pid_tag}_{safe_name}"
    remote_py   = f"/tmp/omni_ocr_{pid_tag}.py"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"]
    ssh_base = ["ssh", "-i", SSH_KEY_PATH] + ssh_opts + [f"{CLOUD_VM_USER}@{CLOUD_VM_IP}"]
    scp_base = ["scp", "-i", SSH_KEY_PATH] + ssh_opts
    try:
        subprocess.check_call(
            scp_base + [file_path, f"{CLOUD_VM_USER}@{CLOUD_VM_IP}:{remote_file}"],
            timeout=180, stderr=subprocess.DEVNULL)
        b64 = _base64.b64encode(_GCP_OCR_PY_SRC.encode("utf-8")).decode("ascii")
        subprocess.check_call(
            ssh_base + [f"echo '{b64}' | base64 -d > {remote_py}"],
            timeout=20, stderr=subprocess.DEVNULL)
        out = subprocess.check_output(
            ssh_base + [f"python3 {remote_py} '{remote_file}' {VISION_MODEL} {DPI} 2>/dev/null"],
            timeout=600, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace").strip()
        return out if out else _ocr_unavailable_stub(file_path, "GCP OCR 결과 없음")
    except Exception as e:
        logging.error(f"[GCP-SSH] {file_path} | {e}")
        return _ocr_unavailable_stub(file_path, str(e)[:200])
    finally:
        try:
            subprocess.run(ssh_base + [f"rm -f {remote_file} {remote_py}"],
                           timeout=10, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def extract_doc_metadata(file_path: str, content: str) -> dict:
    meta = dict(grade="", subject="", semester="", exam_type="", doc_type="", parent_folder="")
    try:
        meta["parent_folder"] = os.path.basename(os.path.dirname(os.path.abspath(file_path)))
    except Exception:
        pass
    if not content:
        return meta
    if m := re.search(r'([1-3])\s*학년', content):
        meta["grade"] = m.group(1) + "학년"
    if m := re.search(r'([1-2])\s*학기', content):
        meta["semester"] = m.group(1) + "학기"
    for kw in ["중간고사", "기말고사", "모의고사", "수능", "내신"]:
        if kw in content:
            meta["exam_type"] = kw
            break
    subjects_map = {
        "수학": ["수학", "미적분", "확률과통계", "기하"],
        "물리": ["물리", "역학", "전자기"],
        "화학": ["화학", "유기화학"],
        "생물": ["생물", "유전", "세포"],
        "지구과학": ["지구과학", "천문"],
        "영어": ["영어", "English"],
        "국어": ["국어", "문학", "독서"],
        "정보": ["정보", "알고리즘", "Python", "코딩"],
    }
    for subj, kws in subjects_map.items():
        if any(kw in content for kw in kws):
            meta["subject"] = subj
            break
    meta["doc_type"] = "pending" if "PENDING_OCR" in content else "exam"
    return meta


@dataclass
class PageNode:
    page_idx: int
    total_pages: int
    element_type: str
    text: str
    parent_doc_id: str
    sibling_page_id: Optional[str] = None
    grade: str = ""
    subject: str = ""
    semester: str = ""
    exam_type: str = ""
    doc_type: str = ""
    parent_folder: str = ""


def _parse_gcp_pages(raw: str) -> Dict[int, str]:
    pages: Dict[int, str] = {}
    if not raw or raw.startswith("[VISUAL_CONTENT_PENDING"):
        return pages
    cur_page, cur_lines = 0, []
    for line in raw.splitlines():
        m = re.match(r"---\s*[Pp]age\s+(\d+)\s*---", line)
        if m:
            if cur_lines:
                pages[cur_page] = "\n".join(cur_lines).strip()
            cur_page = int(m.group(1)) - 1
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_lines:
        pages[cur_page] = "\n".join(cur_lines).strip()
    return pages


def process_scan_pdf(file_path: str, cpu_pool, gcp_pool) -> List[PageNode]:
    """ABBYY + GCP SSH 병렬 → PageNode 리스트."""
    abs_path = os.path.abspath(file_path)
    stub = is_gdrive_stub(file_path)

    abbyy_fut = (
        cpu_pool.submit(task_abbyy_ocr_pages, file_path)
        if str(file_path).lower().endswith(".pdf")
        else None
    )
    gcp_fut   = gcp_pool.submit(task_gcp_ssh_ocr, file_path) if USE_GCP_SSH_OCR and not _CLOUD_DOWN.is_set() else None

    abbyy_pages: Dict[int, str] = {}
    if abbyy_fut is not None:
        try:
            abbyy_pages = abbyy_fut.result(timeout=300)
        except Exception as e:
            logging.error(f"[ABBYY-FUT] {file_path} | {e}")

    gcp_raw = ""
    if gcp_fut:
        try:
            gcp_raw = gcp_fut.result(timeout=600)
        except Exception as e:
            logging.error(f"[GCP-FUT] {file_path} | {e}")

    gcp_pages = _parse_gcp_pages(gcp_raw)
    total_pages = max(
        max(abbyy_pages.keys(), default=-1) + 1,
        max(gcp_pages.keys(), default=-1) + 1,
        1,
    )
    combined = "\n".join(list(abbyy_pages.values()) + list(gcp_pages.values()))
    meta = extract_doc_metadata(file_path, combined)

    nodes: List[PageNode] = []
    for p_idx in range(total_pages):
        t = abbyy_pages.get(p_idx, "")
        v = gcp_pages.get(p_idx, "")
        tid = f"{abs_path}::page_{p_idx}::text"
        vid = f"{abs_path}::page_{p_idx}::visual"
        if t:
            nodes.append(PageNode(p_idx, total_pages, "text", t, abs_path,
                                  vid if v else None, **meta))
        if v:
            nodes.append(PageNode(p_idx, total_pages, "visual", v, abs_path,
                                  tid if t else None, **meta))
        if not t and not v:
            nodes.append(PageNode(p_idx, total_pages, "mixed",
                                  _ocr_unavailable_stub(file_path, f"page {p_idx+1}"),
                                  abs_path, **meta))
    return nodes


def save_nodes(file_path: str, file_name: str, nodes: List[PageNode]):
    """PageNode → Qdrant + SQLite 저장."""
    abs_path = os.path.abspath(file_path)
    docs = []
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    for node in nodes:
        if not node.text or not node.text.strip():
            continue
        chunks = chunk_text(node.text, CHUNK_SIZE, CHUNK_OVERLAP)
        for k, chunk in enumerate(chunks):
            docs.append(Document(
                text=chunk,
                doc_id=f"{abs_path}::page_{node.page_idx}::{node.element_type}::chunk_{k}",
                metadata={
                    "file_name": file_name, "file_path": abs_path, "extension": ext,
                    "chunk_index": k, "total_chunks": len(chunks),
                    "page_index": node.page_idx, "total_pages": node.total_pages,
                    "element_type": node.element_type, "parent_doc_id": node.parent_doc_id,
                    "sibling_page_id": node.sibling_page_id or "",
                    "grade": node.grade, "subject": node.subject,
                    "semester": node.semester, "exam_type": node.exam_type,
                    "doc_type": node.doc_type, "parent_folder": node.parent_folder,
                },
            ))
    if not docs:
        print(f"  ⚠️  저장할 청크 없음: {file_name}")
        return

    _embed = embed_model
    for attempt in range(2):
        try:
            VectorStoreIndex.from_documents(docs, storage_context=storage_context, embed_model=_embed)
            break
        except RuntimeError as oom:
            if ("out of memory" in str(oom).lower() or "cuda" in str(oom).lower()) and attempt == 0:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                _embed = HuggingFaceEmbedding(model_name=EMBED_MODEL, device="cpu")
            else:
                logging.error(f"[OOM] {file_name} | {oom}")
                return

    state_conn.execute("INSERT OR REPLACE INTO processed (file_path) VALUES (?)", (file_path,))
    state_conn.commit()
    print(f"  ✅ {file_name} | nodes={len(nodes)} docs={len(docs)}")


def clean_qdrant_for_file(file_path: str):
    """기존 Qdrant 벡터 삭제 (file_path 기반)."""
    abs_path = os.path.abspath(file_path)
    try:
        # LlamaIndex 저장 방식: payload 안에 _node_content JSON에 file_path 포함
        # parent_doc_id 필드로 직접 삭제 시도
        qclient.delete(
            collection_name=COLLECTION,
            points_selector=Filter(must=[
                FieldCondition(key="file_path", match=MatchValue(value=abs_path))
            ]),
        )
    except Exception as e:
        logging.warning(f"[QDRANT-DEL] file_path 필드 삭제 실패: {abs_path} | {e}")
    try:
        qclient.delete(
            collection_name=COLLECTION,
            points_selector=Filter(must=[
                FieldCondition(key="parent_doc_id", match=MatchValue(value=abs_path))
            ]),
        )
    except Exception as e:
        logging.warning(f"[QDRANT-DEL] parent_doc_id 필드 삭제 실패: {abs_path} | {e}")


# ── 메인 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not os.path.isdir(INPUT_ROOT):
        print(f"❌ 폴더를 찾을 수 없습니다: {INPUT_ROOT}")
        sys.exit(1)

    print(f"\n📂 스캔 PDF 수집 중: {INPUT_ROOT}")
    all_pdfs = []
    for root, _, files in os.walk(INPUT_ROOT):
        for f in files:
            if f.lower().endswith(".pdf"):
                all_pdfs.append(os.path.join(root, f))

    # 스캔 PDF만 필터 (텍스트 PDF 제외) — stub은 무조건 스캔으로 간주
    scan_pdfs = []
    for fp in all_pdfs:
        if is_gdrive_stub(fp) or not is_text_pdf(fp):
            scan_pdfs.append(fp)

    # 이미 처리된 것 제외
    existing = set(r[0] for r in state_conn.execute("SELECT file_path FROM processed"))
    pending = [fp for fp in scan_pdfs if fp not in existing]

    print(f"  전체 PDF : {len(all_pdfs)}개")
    print(f"  스캔 PDF : {len(scan_pdfs)}개")
    print(f"  미처리   : {len(pending)}개\n")

    if not pending:
        print("처리할 파일이 없습니다.")
        sys.exit(0)

    with ThreadPoolExecutor(max_workers=MAX_CPU_WORKERS)    as cpu_pool, \
         ThreadPoolExecutor(max_workers=MAX_GCP_WORKERS)    as gcp_pool, \
         ThreadPoolExecutor(max_workers=MAX_COORD_WORKERS)  as coord_pool:

        def process_one(fp):
            fn = os.path.basename(fp)
            print(f"  🔄 처리 중: {fn}")
            # 기존 벡터 삭제 (재처리)
            clean_qdrant_for_file(fp)
            state_conn.execute("DELETE FROM processed WHERE file_path = ?", (fp,))
            state_conn.commit()
            # ABBYY + GCP 병렬
            nodes = process_scan_pdf(fp, cpu_pool, gcp_pool)
            save_nodes(fp, fn, nodes)

        futs = {coord_pool.submit(process_one, fp): fp for fp in pending}
        done_count = 0
        for fut in as_completed(futs):
            fp = futs[fut]
            done_count += 1
            try:
                fut.result()
            except Exception as e:
                print(f"  ❌ 실패: {os.path.basename(fp)} | {e}")
                logging.error(f"[PRIORITY] {fp} | {e}")
            print(f"  [{done_count}/{len(pending)}] 완료")

    state_conn.close()
    print(f"\n🎉 한성과고 스캔 PDF 재처리 완료! ({done_count}개)")
