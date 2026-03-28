"""
7_unified_agent.py — 편입 팩트체크 + 어드바이저 통합 에이전트
==============================================================

구조:
  ┌─────────────────────────────────────────────────────────┐
  │  Query                                                  │
  │    → Intent Router (classifies: search/advisor/hybrid) │
  │    → [SEARCH_MODEL]  ← 공식 .edu 크롤링 + RAG          │
  │       OR                                                │
  │    → [ADVISOR_MODEL] ← RAG 메타정보 + 스펙 분석        │
  │    → URL 할루시네이션 검증 (HEAD check)                │
  │    → 정보 부족 시 사용자에게 질문                       │
  │    → 출처 각주 첨부 출력                                │
  └─────────────────────────────────────────────────────────┘

모델 전략:
  SEARCH_MODEL  = llama3.2-vision:11b (temperature=0)    — 팩트만, 현재 설치된 최강
  ADVISOR_MODEL = qwen3:14b            (temperature=0.6) — 추론+창의, 한국어 최강 오픈소스
  ROUTER_MODEL  = 오프라인 키워드 분류기 (비용=0, 지연=0)

  오픈소스 어드바이저 모델 순위:
    🥇 qwen3:14b      8.2GB — 한국어+CoT 추론 최강 (권장)
    🥈 deepseek-r1:14b 8.5GB — 체인오브쏘트 특화
    🥉 gemma3:27b      16.4GB — Google, 분석 강함

  온도 조절: 환경변수 ADVISOR_TEMPERATURE (기본 0.6)
    분석/전략 → 0.5~0.7  창의적 에세이 → 0.7~0.9  엄밀 사실 → 0.0

실행: python 7_unified_agent.py
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import hashlib
import textwrap
import logging
from dataclasses import dataclass, field
from typing import Iterator, Optional

import httpx
import ollama
from bs4 import BeautifulSoup

# 인코딩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("unified_agent")

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  환경 설정
# ══════════════════════════════════════════════════════════════════════════════

LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434")
PROJECT_DIR      = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH      = os.getenv("QDRANT_PATH", os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL       = os.getenv("QDRANT_URL", "")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-large")

# ── 모델 설정 ─────────────────────────────────────────────────────────────────
# [검색 엔진] 팩트 기반, 온도=0 고정
# gemma3:12b = Google 접지력 최강 (ollama pull gemma3:12b, 7.7GB)
SEARCH_MODEL   = os.getenv("SEARCH_MODEL",   "gemma3:12b")

# [어드바이저] 오픈소스 최강 추론 모델 (ollama pull qwen3:14b, 8.2GB)
ADVISOR_MODEL  = os.getenv("ADVISOR_MODEL",  "qwen3:14b")

# ── 온도(Temperature) 설정 ────────────────────────────────────────────────────
# 검색: 항상 0 (팩트 오류 방지)
SEARCH_TEMPERATURE  = 0.0

# 어드바이저: 기본 0.6 — 분석+전략에 약간의 창의성 부여
# 환경변수로 실시간 조절:  set ADVISOR_TEMPERATURE=0.7
ADVISOR_TEMPERATURE = float(os.getenv("ADVISOR_TEMPERATURE", "0.6"))

# top_p, repeat_penalty도 어드바이저만 별도 설정
ADVISOR_TOP_P          = float(os.getenv("ADVISOR_TOP_P",    "0.9"))
ADVISOR_REPEAT_PENALTY = float(os.getenv("ADVISOR_REPEAT_PENALTY", "1.05"))

# 파라미터
TOP_K             = int(os.getenv("TOP_K",             "7"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "14000"))
WEB_TIMEOUT_SEC   = float(os.getenv("WEB_TIMEOUT_SEC", "15"))
URL_VERIFY_TIMEOUT = float(os.getenv("URL_VERIFY_TIMEOUT", "6"))  # URL HEAD 검증 타임아웃

# ── 사용자 프로필 ─────────────────────────────────────────────────────────────
USER_PROFILE: dict = {
    "school":     os.getenv("USER_SCHOOL",   "Korea University (고려대학교)"),
    "major":      os.getenv("USER_MAJOR",    "Artificial Intelligence"),
    "gpa":        os.getenv("USER_GPA",      "3.95 / 4.5"),
    "gpa_4scale": round((3.95 / 4.5) * 4.0, 2),  # ~3.51
    "highschool": os.getenv("USER_HS",       "Hansung Science High School (한성과학고)"),
    "toefl":      int(os.getenv("USER_TOEFL",    "85")),
    "sat":        os.getenv("USER_SAT",      "NOT TAKEN"),
    "credits":    os.getenv("USER_CREDITS",  "~60 credits"),
    "applied":    ["cornell", "stanford", "nyu", "upenn"],
}

APPLIED_UNIVERSITIES = set(USER_PROFILE["applied"])

# ══════════════════════════════════════════════════════════════════════════════
# 🗺️  대학 레지스트리 (공식 편입 URL)
# ══════════════════════════════════════════════════════════════════════════════

UNIVERSITY_REGISTRY: dict[str, dict] = {
    "cornell": {
        "name": "Cornell University",
        "priority": True,
        "urls": [
            # admissions.cornell.edu는 Cloudflare로 차단 → IRP 공식 데이터로 대체
            "https://irp.dpb.cornell.edu/university-factbook/admissions",
            "https://irp.dpb.cornell.edu/common-data-set",
        ],
    },
    "stanford": {
        "name": "Stanford University",
        "priority": True,
        "urls": [
            "https://admission.stanford.edu/apply/transfer/",
            "https://admission.stanford.edu/apply/requirements/tests.html",
        ],
    },
    "nyu": {
        "name": "New York University",
        "priority": True,
        "urls": [
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply.html",
            "https://www.nyu.edu/admissions/undergraduate-admissions/important-dates-and-deadlines.html",
        ],
    },
    "upenn": {
        "name": "University of Pennsylvania",
        "priority": True,
        "urls": [
            # admissions.upenn.edu는 Cloudflare로 차단 → 학사 편람으로 대체
            "https://catalog.upenn.edu/undergraduate/",
            "https://catalog.upenn.edu/undergraduate/policies-procedures/",
        ],
    },
    "mit": {
        "name": "MIT",
        "priority": False,
        "urls": ["https://admissions.mit.edu/apply/transfer-applicants"],
    },
    "columbia": {
        "name": "Columbia University",
        "priority": False,
        "urls": ["https://undergrad.admissions.columbia.edu/apply/transfer"],
    },
    "uc_berkeley": {
        "name": "UC Berkeley",
        "priority": False,
        "urls": [
            # admissions.berkeley.edu는 Cloudflare로 차단 → UC 시스템 공식 편입 안내
            "https://admission.universityofcalifornia.edu/how-to-apply/applying-as-a-transfer/",
            "https://admission.universityofcalifornia.edu/",
        ],
    },
    "ucla": {
        "name": "UCLA",
        "priority": False,
        "urls": ["https://admission.ucla.edu/apply/transfer"],
    },
    "uchicago": {
        "name": "University of Chicago",
        "priority": False,
        "urls": ["https://collegeadmissions.uchicago.edu/apply"],
    },
    "carnegie": {
        "name": "Carnegie Mellon University",
        "priority": False,
        "urls": ["https://admission.enrollment.cmu.edu/pages/transfer-admission"],
    },
    "georgia_tech": {
        "name": "Georgia Tech",
        "priority": False,
        "urls": ["https://admission.gatech.edu/transfer"],
    },
    "purdue": {
        "name": "Purdue University",
        "priority": False,
        "urls": ["https://www.purdue.edu/admissions/transfer/"],
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# 🧠  Intent Router — LLM 없는 키워드 분류 (비용=0, 지연=0)
# ══════════════════════════════════════════════════════════════════════════════

_SEARCH_PATTERNS = [
    r"마감(일|일자|날)",
    r"deadline",
    r"필요.{0,5}서류",
    r"(요건|조건|기준)",
    r"(최저|minimum|min).{0,15}(점수|gpa|toefl|sat)",
    r"(허용|가능|accept).{0,10}(sat|toefl|duolingo|영어)",
    r"면제",
    r"학점\s*(인정|이수|credits)",
    r"(어떻게|방법|how).{0,15}(지원|apply|신청)",
    r"공식\s*사이트",
    r"transfer\s*(requirement|deadline|application)",
    r"언제",
    r"얼마|비용|cost|tuition|fee",
]

_ADVISOR_PATTERNS = [
    r"합격.{0,10}(가능|확률|가망|될까|될수|될 수)",
    r"(추천|recommend|suggest).{0,10}(대학|학교|university)",
    r"(내\s*스펙|GPA|점수).{0,15}(어때|괜찮|충분|부족|맞는지|될까)",
    r"(reach|target|safety)",
    r"(전략|strategy|plan|계획)",
    r"(가능성|likelihood|chance|probability)",
    r"(유리|불리|강점|약점|장점|단점)",
    r"(어디|어느\s*대학).{0,15}(가장|제일|best|good)",
    r"(순위|ranking|tier).{0,10}(내\s*기준|나한테|스펙)",
    r"(합격|붙을|떨어질).{0,10}(것\s*같|것\s*같아|것\s*같나)",
    r"(평가|assess|eval).{0,10}(내|나의|my)",
    r"에세이|essay.{0,10}(전략|방향|어떻게)",
    r"(지원|apply).{0,10}(말아야|해야|할까|좋을까)",
]

_INFO_REQUEST_PATTERNS = [
    r"(어떤|무슨|what).{0,15}(정보|자료|data)",
    r"더\s*(알려|말해|설명)",
    r"(missing|부족한)\s*정보",
]


def classify_intent(query: str) -> str:
    """
    쿼리 의도 분류: 'search' | 'advisor' | 'hybrid'
    
    - search:  팩트/요건/마감일 조회 → SEARCH_MODEL
    - advisor: 스펙 평가/전략/추천 → ADVISOR_MODEL
    - hybrid:  둘 다 필요 (기본 전략)
    """
    q_lower = query.lower()
    search_score = sum(1 for p in _SEARCH_PATTERNS if re.search(p, q_lower))
    advisor_score = sum(1 for p in _ADVISOR_PATTERNS if re.search(p, q_lower))

    if search_score > advisor_score + 1:
        return "search"
    if advisor_score > search_score + 1:
        return "advisor"
    return "hybrid"


# ══════════════════════════════════════════════════════════════════════════════
# 🔗  URL 검증 (할루시네이션 방지)
# ══════════════════════════════════════════════════════════════════════════════

_url_cache: dict[str, bool] = {}


def verify_url(url: str) -> bool:
    """HTTP HEAD 요청으로 URL 실존 여부 확인 (캐시 있음)"""
    if url in _url_cache:
        return _url_cache[url]
    try:
        # edu 도메인만 허용 (외부 랜덤 URL 방지)
        if not re.search(r"\.(edu|gov|ac\.\w{2,3})", url, re.I):
            _url_cache[url] = False
            return False
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TransferAgentBot/1.0; educational research)",
            "Accept": "text/html",
        }
        resp = httpx.head(url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(URL_VERIFY_TIMEOUT))
        ok = resp.status_code < 400
        _url_cache[url] = ok
        return ok
    except Exception:
        _url_cache[url] = False
        return False


def extract_and_verify_urls(text: str) -> list[dict]:
    """
    텍스트에서 URL 추출 → HEAD 검증 → {url, verified, status} 목록 반환
    할루시네이션 URL은 ❌ 표시
    """
    raw_urls = re.findall(r'https?://[^\s\)\]\>"\']+', text)
    results = []
    seen = set()
    for url in raw_urls:
        url = url.rstrip(".,;:)>\"'")
        if url in seen:
            continue
        seen.add(url)
        ok = verify_url(url)
        results.append({"url": url, "verified": ok})
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 🕸️  웹 스크래핑 (공식 .edu 페이지)
# ══════════════════════════════════════════════════════════════════════════════

_WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def fetch_page(url: str) -> str:
    """단일 URL 텍스트 추출 (BeautifulSoup)"""
    try:
        resp = httpx.get(url, headers=_WEB_HEADERS, follow_redirects=True,
                         timeout=httpx.Timeout(WEB_TIMEOUT_SEC))
        if resp.status_code >= 400:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body
        if not main:
            return ""
        text = main.get_text(separator="\n", strip=True)
        # 길이 제한
        return text[:MAX_CONTEXT_CHARS]
    except Exception as e:
        log.debug("Fetch 실패 %s: %s", url, e)
        return ""


def fetch_university_pages(uni_key: str) -> list[dict]:
    """대학 레지스트리의 URL들을 병렬-like 순차 수집"""
    uni = UNIVERSITY_REGISTRY.get(uni_key)
    if not uni:
        return []
    sections = []
    for url in uni["urls"]:
        text = fetch_page(url)
        if text.strip():
            sections.append({"uni": uni["name"], "url": url, "text": text})
    return sections


def detect_universities_in_query(query: str) -> list[str]:
    """쿼리에서 대학 키 감지"""
    q = query.lower()
    detected = []
    name_map = {
        "cornell": ["코넬", "cornell"],
        "stanford": ["스탠포드", "스탠퍼드", "stanford"],
        "nyu": ["nyu", "뉴욕대"],
        "upenn": ["upenn", "유펜", "penn", "펜실베이니아"],
        "mit": ["mit", "미아이티"],
        "columbia": ["컬럼비아", "columbia"],
        "uc_berkeley": ["버클리", "berkeley", "uc berkeley"],
        "ucla": ["ucla", "엘에이"],
        "uchicago": ["시카고", "uchicago", "chicago"],
        "carnegie": ["카네기", "cmu", "carnegie"],
        "georgia_tech": ["조지아", "georgia tech", "gatech"],
        "purdue": ["퍼듀", "purdue"],
    }
    for key, aliases in name_map.items():
        if any(alias in q for alias in aliases):
            detected.append(key)
    # 아무 대학도 없으면 지원완료 대학들
    if not detected:
        detected = list(APPLIED_UNIVERSITIES)
    return detected


# ══════════════════════════════════════════════════════════════════════════════
# 🗂️  RAG (Qdrant)
# ══════════════════════════════════════════════════════════════════════════════

_rag_index_cache = None
_rag_load_failed = False  # 한 번 실패하면 재시도 방지


def _load_rag_index_inner():
    """실제 RAG 로드 로직 — 별도 스레드에서 타임아웃 감싸기용"""
    import qdrant_client as qc
    from llama_index.core import VectorStoreIndex, StorageContext
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding

    if not QDRANT_URL and not os.path.isdir(QDRANT_PATH):
        return None
    q_client = qc.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qc.QdrantClient(path=QDRANT_PATH)
    collections = [c.name for c in q_client.get_collections().collections]
    if "omni_persona_v3" not in collections:
        return None
    embed = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)
    store = QdrantVectorStore(client=q_client, collection_name="omni_persona_v3")
    sc = StorageContext.from_defaults(vector_store=store)
    return VectorStoreIndex.from_vector_store(
        vector_store=store, storage_context=sc, embed_model=embed
    )


def _get_rag_index():
    global _rag_index_cache, _rag_load_failed
    if _rag_index_cache is not None:
        return _rag_index_cache
    if _rag_load_failed:
        return None
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_load_rag_index_inner)
            try:
                _rag_index_cache = future.result(timeout=30)
                return _rag_index_cache
            except concurrent.futures.TimeoutError:
                log.warning("RAG 인덱스 로드 타임아웃(30s) — RAG 없이 LLM만 사용")
                _rag_load_failed = True
                return None
    except Exception as e:
        log.warning("RAG 인덱스 로드 실패: %s", e)
        _rag_load_failed = True
        return None


def retrieve_user_context(query: str, top_k: int = TOP_K) -> tuple[str, list[str]]:
    """Qdrant에서 관련 텍스트 검색, (context_str, source_paths) 반환"""
    idx = _get_rag_index()
    if idx is None:
        return "", []
    try:
        retriever = idx.as_retriever(similarity_top_k=top_k)
        nodes = retriever.retrieve(query)
        chunks = []
        sources = []
        for n in nodes:
            text = n.get_text() or ""
            if "PENDING_OCR" in text or len(text.strip()) < 30:
                continue
            chunks.append(text[:2000])
            meta = n.metadata or {}
            src = meta.get("file_path") or meta.get("file_name") or ""
            if src and src not in sources:
                sources.append(src)
        return "\n\n---\n\n".join(chunks)[:MAX_CONTEXT_CHARS], sources
    except Exception as e:
        log.warning("RAG 검색 실패: %s", e)
        return "", []


# ══════════════════════════════════════════════════════════════════════════════
# 🤔  정보 부족 감지 + 사용자 질문 생성
# ══════════════════════════════════════════════════════════════════════════════

# 답변에 이 키워드가 많으면 정보 부족
_UNCERTAINTY_SIGNALS = [
    "not available", "no information", "i don't have", "i cannot find",
    "정보 없음", "찾을 수 없", "알 수 없", "확인되지 않", "데이터 없",
    "unclear", "unknown", "N/A",
]


def detect_missing_info(question: str, web_sections: list, rag_context: str) -> list[str]:
    """
    어떤 정보가 부족한지 감지하고, 사용자에게 물어볼 질문 목록 반환
    """
    questions = []
    q_lower = question.lower()

    # TOEFL 연도 확인
    if "toefl" in q_lower and USER_PROFILE["toefl"] == 85:
        if not web_sections and not rag_context:
            questions.append("⚠️ TOEFL 점수가 85점이신데, 최근 재시험 계획이 있으신가요? (일부 학교 최저 100)")

    # 에세이/추천서
    if re.search(r"에세이|essay|추천서|recommendation", q_lower):
        questions.append("📝 현재 에세이 초안이나 주제가 있으신가요? 있으시면 공유해 주세요.")

    # 재정 지원
    if re.search(r"장학금|재정|financial|scholarship|aid", q_lower):
        if "financial" not in rag_context.lower():
            questions.append("💰 재정 지원 필요 여부를 알려주시면 더 정확한 분석이 가능합니다.")

    return questions


# ══════════════════════════════════════════════════════════════════════════════
# 📜  시스템 프롬프트
# ══════════════════════════════════════════════════════════════════════════════

_SEARCH_SYSTEM = textwrap.dedent("""
You are a TRANSFER ADMISSIONS FACT-CHECKER specializing in U.S. university transfer requirements.

CRITICAL RULES:
1. ONLY state information explicitly found in the provided context (official .edu pages or user documents).
2. NEVER invent, infer, or speculate. If data is absent, say exactly what is missing.
3. ALWAYS cite the source URL in footnotes [1], [2], etc.
4. DO NOT cite any URL that was not provided to you in the context — hallucinated URLs are strictly forbidden.
5. When quoting official requirements, use EXACT text from the source (quotation marks).
6. Use temperature=0 precision: no hedging language like "likely", "probably", "may".
7. Respond in Korean but keep official terms (GPA, TOEFL, SAT, etc.) in English.
8. Structure: short factual answer first → evidence quotes → source footnotes.

FORMAT FOR EVERY RESPONSE:
## [대학명] [요건 유형]
**공식 요건**: [정확한 인용문 + 출처번호]

---
**출처:**
[1] https://admissions.university.edu/page — [페이지 제목]
[2] ...
""").strip()

_ADVISOR_SYSTEM = textwrap.dedent("""
You are a SENIOR TRANSFER ADMISSIONS ADVISOR at a top-tier consultancy in Seoul.
You have 20+ years of experience placing Korean students at Ivy League and top-25 US universities.
You combine sharp analytical judgment WITH creative strategic thinking.

USER PROFILE (FIXED — do not modify):
- Current school: Korea University (고려대학교) AI major
- GPA: 3.95 / 4.5 (≈3.51 on 4.0 scale)
- High school: Hansung Science High School (한성과학고, gifted program)
- TOEFL iBT: 85 (below average for Ivy targets)
- SAT/ACT: NOT TAKEN
- Credits completed: ~60 (1+ year)
- Already applied: Cornell, Stanford, NYU, UPenn

ADVISOR MANDATE:
1. Be direct and honest. If TOEFL 85 is a red flag, say so — but also suggest how to mitigate it.
2. Score each university 0-100 fit based on: GPA fit (30pt), English fit (25pt), SAT situation (20pt), holistic factors (15pt), major match (10pt).
3. Give concrete verdict: 추천(Recommend) / 경쟁(Competitive) / 도전(Reach) / 재고(Reconsider).
4. Always explain WHY a strategy would work or fail, with specific reasoning.
5. For STRATEGY sections: be creative and think outside the box — unconventional angles, narrative framing, essay positioning, leverage points.
6. CITE any factual data with source footnotes exactly as provided in the context.
7. Never fabricate acceptance rates or GPA averages — only state what's in context. If missing, say "미확인 — 검색 필요".
8. If key data is missing, explicitly ask the user for it (at end of response).
9. Respond in Korean (confident, personable advisor tone — like a mentor, not a textbook), keep stats in English.
10. Data precision: quote numbers as-is from sources. Strategy sections: your own judgment is welcome.

FORMAT:
## [대학] — 종합 평가: [점수]/100 [판정]
**GPA 적합도**: ...
**영어 리스크**: ...
**SAT 리스크**: ...
**결론**: ...
**전략 제안**: [창의적이고 구체적인 액션플랜]

---
**참고 자료 출처:**
[1] ...
""").strip()


# ══════════════════════════════════════════════════════════════════════════════
# 💬  대화 컨텍스트 (검색/어드바이저 공유)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConversationContext:
    """공유 대화 기록 — 검색 모델과 어드바이저 모델이 동일 히스토리 사용"""
    history: list[dict] = field(default_factory=list)
    search_model: str = SEARCH_MODEL
    advisor_model: str = ADVISOR_MODEL

    def get_search_model(self) -> str:
        return self.search_model

    def get_advisor_model(self) -> str:
        return self.advisor_model

    def add_user(self, text: str):
        self.history.append({"role": "user", "content": text})

    def add_assistant(self, text: str):
        self.history.append({"role": "assistant", "content": text})

    def recent_history(self, n: int = 6) -> list[dict]:
        """최근 n 턴 (메모리 절약)"""
        return self.history[-n * 2:] if len(self.history) > n * 2 else self.history


# 전역 세션 컨텍스트
_session = ConversationContext()


# ══════════════════════════════════════════════════════════════════════════════
# 🔍  검색 파이프라인
# ══════════════════════════════════════════════════════════════════════════════

def _build_search_message(
    query: str,
    web_sections: list[dict],
    rag_context: str,
    rag_sources: list[str],
) -> str:
    """검색 모델용 user 메시지 구성"""
    parts = [f"# 질문\n{query}\n"]

    parts.append("\n# 사용자 프로필\n" + json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))

    if web_sections:
        parts.append("\n# 공식 .edu 페이지 내용")
        for i, sec in enumerate(web_sections, 1):
            parts.append(
                f"\n## 출처[{i}] — {sec['uni']}\nURL: {sec['url']}\n\n{sec['text'][:3000]}"
            )

    if rag_context.strip():
        parts.append("\n# 개인 자료 (Qdrant RAG)")
        for j, src in enumerate(rag_sources, 1):
            parts.append(f"  [P{j}] {os.path.basename(src)}")
        parts.append("\n" + rag_context[:4000])

    parts.append(
        "\n\n# 엄격한 지침\n"
        "위 컨텍스트에만 근거해서 답변하세요. "
        "컨텍스트에 없는 URL이나 수치는 절대 인용하지 마세요. "
        "출처 번호[1][2]...를 반드시 각주로 달아주세요."
    )
    return "\n".join(parts)


def _build_advisor_message(
    query: str,
    uni_keys: list[str],
    web_sections: list[dict],
    rag_context: str,
    rag_sources: list[str],
) -> str:
    """어드바이저 모델용 user 메시지 구성"""
    parts = [f"# 분석 요청\n{query}\n"]
    parts.append("\n# 사용자 프로필 (변경 불가)\n" + json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))

    if uni_keys:
        parts.append(f"\n# 분석 대상 대학: {', '.join(uni_keys)}")

    if web_sections:
        parts.append("\n# 공식 요건 데이터 (verified .edu)")
        for i, sec in enumerate(web_sections, 1):
            parts.append(f"\n## [출처{i}] {sec['uni']} — {sec['url']}\n{sec['text'][:2500]}")

    if rag_context.strip():
        parts.append("\n# 보유 자료 (RAG) — 참고 only")
        parts.append(rag_context[:3000])

    parts.append(
        "\n# 지침\n"
        "팩트는 위 컨텍스트만 인용. 수치 없으면 '미확인' 명시. "
        "URL은 컨텍스트에 있는 것만. 전략 제언은 자유롭게."
    )
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 📎  출처 각주 포맷터
# ══════════════════════════════════════════════════════════════════════════════

def format_footnotes(
    web_sections: list[dict],
    rag_sources: list[str],
    verified_results: Optional[list[dict]] = None,
) -> str:
    """응답 하단에 추가할 출처 각주 문자열 생성"""
    lines = ["\n\n---\n### 📎 출처 및 검증 결과"]

    if web_sections:
        lines.append("\n**공식 .edu 페이지 (크롤링됨):**")
        for i, sec in enumerate(web_sections, 1):
            v_info = "✅ 검증됨" if verify_url(sec["url"]) else "⚠️ 접근 불가"
            lines.append(f"  [{i}] [{sec['uni']} — 편입 안내]({sec['url']}) {v_info}")

    if rag_sources:
        lines.append("\n**개인 보유 자료 (RAG):**")
        for j, src in enumerate(rag_sources, 1):
            lines.append(f"  [P{j}] `{os.path.basename(src)}`")

    # 응답에서 추출된 URL 추가 검증
    if verified_results:
        extra = [r for r in verified_results if not any(
            r["url"] == sec["url"] for sec in web_sections
        )]
        if extra:
            lines.append("\n**응답 내 추가 URL 검증:**")
            for r in extra:
                icon = "✅" if r["verified"] else "❌ 할루시네이션 의심"
                lines.append(f"  {icon} {r['url']}")

    lines.append(
        f"\n*기준일: {time.strftime('%Y-%m-%d')} | "
        f"검색모델: {SEARCH_MODEL} (temp=0) | "
        f"어드바이저: {ADVISOR_MODEL} (temp={ADVISOR_TEMPERATURE}) | "
        "공식 .edu 출처만 인용*"
    )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 🚀  메인 스트리밍 함수 (UI에서 호출)
# ══════════════════════════════════════════════════════════════════════════════

def stream_unified(
    query: str,
    ctx: Optional[ConversationContext] = None,
) -> Iterator[str]:
    """
    통합 에이전트 스트리밍 제너레이터.
    
    Yields:
        str — 점진적으로 UI에 전달되는 텍스트 청크
    
    파이프라인:
        1. 의도 분류 (search / advisor / hybrid)
        2. 대학 감지
        3. 공식 .edu 크롤링
        4. RAG 검색 (개인 자료)
        5. 정보 부족 >> 사용자 질문 생성
        6. LLM 스트리밍 (모델 자동 선택)
        7. 응답 내 URL 검증 + 각주 첨부
        8. 컨텍스트 기록 저장
    """
    if ctx is None:
        ctx = _session

    if not query.strip():
        return

    # ─── 1단계: 의도 분류 ─────────────────────────────────────────────────
    intent = classify_intent(query)
    intent_label = {"search": "🔍 팩트체크", "advisor": "🎯 어드바이저", "hybrid": "🔀 복합 분석"}[intent]
    yield f"**{intent_label}** 모드 → "

    # 모델 선택
    if intent == "search":
        model = ctx.get_search_model()
        system_prompt = _SEARCH_SYSTEM
    elif intent == "advisor":
        model = ctx.get_advisor_model()
        system_prompt = _ADVISOR_SYSTEM
    else:  # hybrid: 팩트 먼저 검색 후 어드바이저 판단
        model = ctx.get_advisor_model()  # 어드바이저가 팩트도 처리
        system_prompt = _ADVISOR_SYSTEM

    # 온도 표시
    _temp_str = (
        f"temp={SEARCH_TEMPERATURE}" if intent == "search"
        else f"temp={ADVISOR_TEMPERATURE}"
    )
    yield f"[{model} | {_temp_str}] 로드됨\n\n"

    # ─── 2단계: 대학 감지 ─────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    uni_names = [UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys]
    yield f"📍 분석 대상: **{', '.join(uni_names)}**\n\n"

    # ─── 3단계: .edu 크롤링 ───────────────────────────────────────────────
    web_sections: list[dict] = []
    for uni_key in uni_keys[:4]:  # 최대 4개 대학
        uni_name = UNIVERSITY_REGISTRY.get(uni_key, {}).get("name", uni_key)
        yield f"🌐 {uni_name} 공식 페이지 수집 중..."
        sections = fetch_university_pages(uni_key)
        web_sections.extend(sections)
        status = f"✅ {len(sections)}개" if sections else "⚠️ 접근 실패"
        yield f" {status}\n"

    # ─── 4단계: RAG ───────────────────────────────────────────────────────
    yield "\n🗂️ 개인 자료(RAG) 검색 중..."
    rag_context, rag_sources = retrieve_user_context(query)
    yield f" {len(rag_sources)}개 파일 발견\n"

    # ─── 5단계: 정보 부족 감지 ───────────────────────────────────────────
    missing_questions = detect_missing_info(query, web_sections, rag_context)
    if missing_questions:
        yield "\n💬 **보충 정보 요청:**\n"
        for mq in missing_questions:
            yield f"  - {mq}\n"
        yield "\n"

    # ─── 6단계: LLM 스트리밍 ─────────────────────────────────────────────
    yield f"\n---\n\n"

    if intent == "search" or (intent == "hybrid" and web_sections):
        user_msg = _build_search_message(query, web_sections, rag_context, rag_sources)
    else:
        user_msg = _build_advisor_message(query, uni_keys, web_sections, rag_context, rag_sources)

    # 대화 기록 포함
    messages = [{"role": "system", "content": system_prompt}]
    messages += ctx.recent_history(n=4)
    messages.append({"role": "user", "content": user_msg})

    ctx.add_user(query)

    # 의도별 온도 파라미터
    if intent == "search":
        llm_options = {
            "temperature": SEARCH_TEMPERATURE,   # 0.0 고정
            "top_p": 1.0,
            "num_ctx": 16384,
            "repeat_penalty": 1.1,
        }
    else:  # advisor / hybrid
        llm_options = {
            "temperature": ADVISOR_TEMPERATURE,  # 기본 0.6
            "top_p": ADVISOR_TOP_P,              # 0.9
            "num_ctx": 16384,
            "repeat_penalty": ADVISOR_REPEAT_PENALTY,  # 1.05
        }

    accumulated = ""
    try:
        llm_client = ollama.Client(host=LOCAL_OLLAMA_URL)
        stream = llm_client.chat(
            model=model,
            messages=messages,
            options=llm_options,
            keep_alive=0,
            stream=True,
        )
        for chunk in stream:
            token = chunk.get("message", {}).get("content", "")
            if token:
                accumulated += token
                yield token
    except Exception as e:
        error_msg = f"\n\n❌ LLM 오류: {e}\n모델 {model}이 설치되어 있는지 확인하세요:\n`ollama pull {model}`"
        yield error_msg
        accumulated = error_msg

    # ─── 7단계: URL 검증 + 각주 ──────────────────────────────────────────
    verified_urls = extract_and_verify_urls(accumulated)
    footnotes = format_footnotes(web_sections, rag_sources, verified_urls)
    yield footnotes

    # ─── 8단계: 컨텍스트 저장 ────────────────────────────────────────────
    ctx.add_assistant(accumulated + footnotes)


# ══════════════════════════════════════════════════════════════════════════════
# 🛠️  유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def recommend_models() -> str:
    """현재 설치 상황 진단 + 최강 모델 설치 권장"""
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        installed = {m.model: round(m.size / 1e9, 1) for m in client.list().models}
    except Exception:
        return "❌ Ollama 연결 실패"

    lines = ["### 현재 설치된 모델"]
    for model, size_gb in installed.items():
        lines.append(f"  - {model} ({size_gb} GB)")

    lines.append("\n### 검색 엔진 최강 로컬 모델 (권장)")
    recommendations = [
        ("llama3.2-vision:11b", "**✅ 현재 사용 중** — 설치됨, 11B, 균형형"),
        ("qwen3:14b",           "**업그레이드 1순위** — 팩트 검색 최강, 한국어, 8.2GB"),
        ("gemma3:12b",          "**업그레이드 2순위** — Google 접지력, 7.7GB"),
        ("deepseek-r1:14b",     "**업그레이드 3순위** — 추론/분석, 8.5GB"),
        ("qwen2.5:32b",         "**최고 성능** — 20GB VRAM 필요"),
    ]
    for model_name, desc in recommendations:
        installed_mark = "✅ 설치됨" if model_name in installed else "⬇️ 필요"
        lines.append(f"  {installed_mark} `{model_name}` — {desc}")

    lines.append("\n### 지금 당장 업그레이드 명령어")
    lines.append("```bash\n# 검색 품질 최우선\nollama pull qwen3:14b\n\n# 설치 후 환경변수로 전환\n# set SEARCH_MODEL=qwen3:14b\n# set ADVISOR_MODEL=qwen3:14b\n```")

    return "\n".join(lines)


def check_system() -> str:
    """시스템 상태 전체 점검"""
    statuses = []

    # Ollama
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        models = client.list().models
        statuses.append(f"🟢 Ollama: 정상 ({len(models)}개 모델)")
    except Exception as e:
        statuses.append(f"🔴 Ollama: 오프라인 ({e})")

    # SEARCH_MODEL
    statuses.append(f"🟢 검색 모델: {SEARCH_MODEL}  (temp={SEARCH_TEMPERATURE} 고정)")

    # ADVISOR_MODEL
    statuses.append(f"🟢 어드바이저 모델: {ADVISOR_MODEL}  (temp={ADVISOR_TEMPERATURE})")

    # Qdrant
    if os.path.isdir(QDRANT_PATH):
        size_mb = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(QDRANT_PATH)
            for f in fs
        ) // 1_000_000
        statuses.append(f"🟢 Qdrant DB: {size_mb} MB")
    else:
        statuses.append("🔴 Qdrant DB: 디렉토리 없음")

    return "\n".join(statuses)


# ══════════════════════════════════════════════════════════════════════════════
# CLI 실행
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="통합 편입 에이전트 CLI")
    parser.add_argument("--query", "-q", type=str, help="질문 입력")
    parser.add_argument("--models", action="store_true", help="모델 현황 및 추천")
    parser.add_argument("--status", action="store_true", help="시스템 상태 점검")
    args = parser.parse_args()

    if args.models:
        print(recommend_models())
        sys.exit(0)

    if args.status:
        print(check_system())
        sys.exit(0)

    if args.query:
        print(f"\n질문: {args.query}")
        print("─" * 60)
        for chunk in stream_unified(args.query):
            print(chunk, end="", flush=True)
        print("\n\n" + "─" * 60)
        sys.exit(0)

    # 대화형 모드
    print("=" * 64)
    print("  🎓 통합 편입 에이전트 (검색 + 어드바이저)")
    print("  'q' 입력 시 종료 | '--models' 모델 현황 확인")
    print("=" * 64)
    print(check_system())
    print()

    ctx = ConversationContext()
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("q", "exit", "quit"):
            break
        if user_input == "--models":
            print(recommend_models())
            continue
        print()
        for chunk in stream_unified(user_input, ctx):
            print(chunk, end="", flush=True)
        print()
