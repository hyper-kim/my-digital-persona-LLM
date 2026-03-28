"""
4_transfer_agent.py — 미국 명문대 편입 전문 팩트체크 에이전트
=============================================================
역할: 대학 공식 .edu 페이지 + 사용자 개인 자료(Qdrant RAG)를 결합하여
      100% 팩트 기반 편입 정보를 제공한다.

실행: python 4_transfer_agent.py
"""

import os
import sys
import re
import time
import logging
import textwrap

import httpx
from bs4 import BeautifulSoup
import ollama
import qdrant_client
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ─── 인코딩 안전 설정 ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  환경 설정
# ══════════════════════════════════════════════════════════════════════════════
PROJECT_DIR       = os.getenv("PROJECT_DIR",       r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH       = os.getenv("QDRANT_PATH",       os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL        = os.getenv("QDRANT_URL", "")
LOCAL_OLLAMA_URL  = os.getenv("LOCAL_OLLAMA_URL",  "http://localhost:11434")
EMBED_MODEL_NAME  = os.getenv("EMBED_MODEL_NAME",  "intfloat/multilingual-e5-large")
# 팩트체크 모델: 정확한 인용·instruction-following 특화
# 권장: gemma3:12b (VRAM ~7GB, Google — grounding 최강)
#       qwen3:14b (VRAM ~8-9GB, 한국어 추론 우수)
CHAT_MODEL        = os.getenv("TRANSFER_CHAT_MODEL", "qwen3:14b")

# 어드바이저 모델: 다단계 추론·전략 분석 특화
# 권장: qwen3:14b (CoT + 한국어 이해 우수)
#       gemma3:27b @ GCP if VRAM 부족 시
ADVISOR_MODEL     = os.getenv("TRANSFER_ADVISOR_MODEL", "qwen3:14b")

TOP_K             = int(os.getenv("TOP_K",             "6"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))
WEB_TIMEOUT_SEC   = float(os.getenv("WEB_TIMEOUT_SEC", "15"))

# ── 사용자 프로필 ─────────────────────────────────────────────────────────────
USER_PROFILE: dict[str, str] = {
    "school":     os.getenv("USER_SCHOOL",   "Korea University (고려대학교)"),
    "major":      os.getenv("USER_MAJOR",    "Artificial Intelligence"),
    "gpa":        os.getenv("USER_GPA",      "3.95 / 4.5"),
    "highschool": os.getenv("USER_HS",       "Hansung Science High School (한성과학고)"),
    "toefl":      os.getenv("USER_TOEFL",    "85"),
    "duolingo":   os.getenv("USER_DUOLINGO", "unknown"),
    "sat":        os.getenv("USER_SAT",      "NOT TAKEN"),
    "credits":    os.getenv("USER_CREDITS",  "~60 credits (1+ year completed)"),
}

APPLIED_UNIVERSITIES = {"cornell", "stanford", "nyu", "upenn"}  # 이미 지원 완료
TOP_PRIORITY = "cornell"

# ══════════════════════════════════════════════════════════════════════════════
# 🗺️  대학별 공식 편입 URL 레지스트리
#     priority=True → 이미 지원 완료 → 마감일·서류 최우선 체크
# ══════════════════════════════════════════════════════════════════════════════
UNIVERSITY_REGISTRY: dict[str, dict] = {
    "cornell": {
        "name":     "Cornell University",
        "priority": True,
        "urls": [
            "https://admissions.cornell.edu/apply/transfer-applicants",
            "https://admissions.cornell.edu/apply/transfer-applicants/checklist",
        ],
        "keywords": ["cornell", "ivy"],
    },
    "stanford": {
        "name":     "Stanford University",
        "priority": True,
        "urls": [
            "https://admission.stanford.edu/apply/transfer/",
            "https://admission.stanford.edu/apply/transfer/faq.html",
        ],
        "keywords": ["stanford"],
    },
    "nyu": {
        "name":     "New York University (NYU)",
        "priority": True,
        "urls": [
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply/transfer.html",
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply/transfer/"
            "international-transfer-students.html",
        ],
        "keywords": ["nyu", "new york university"],
    },
    "upenn": {
        "name":     "University of Pennsylvania (UPenn)",
        "priority": True,
        "urls": [
            "https://admissions.upenn.edu/admissions-and-financial-aid/transfer-applicants",
        ],
        "keywords": ["upenn", "penn", "university of pennsylvania"],
    },
    "gatech": {
        "name":     "Georgia Institute of Technology",
        "priority": False,
        "urls": [
            "https://admission.gatech.edu/transfer/",
            "https://admission.gatech.edu/transfer/international-transfer",
        ],
        "keywords": ["georgia tech", "gatech", "gt"],
    },
    "umich": {
        "name":     "University of Michigan",
        "priority": False,
        "urls": [
            "https://admissions.umich.edu/apply/transfer",
        ],
        "keywords": ["umich", "michigan", "university of michigan"],
    },
    "uiuc": {
        "name":     "University of Illinois Urbana-Champaign (UIUC)",
        "priority": False,
        "urls": [
            "https://admissions.illinois.edu/apply/transfer",
        ],
        "keywords": ["uiuc", "illinois", "urbana"],
    },
    "ucsd": {
        "name":     "UC San Diego (UCSD)",
        "priority": False,
        "urls": [
            "https://admissions.ucsd.edu/transfer/index.html",
        ],
        "keywords": ["ucsd", "uc san diego", "san diego"],
    },
    "usc": {
        "name":     "University of Southern California (USC)",
        "priority": False,
        "urls": [
            "https://admission.usc.edu/transfer/",
        ],
        "keywords": ["usc", "southern california"],
    },
    "bu": {
        "name":     "Boston University (BU)",
        "priority": False,
        "urls": [
            "https://www.bu.edu/admissions/apply/transfer/",
        ],
        "keywords": ["bu", "boston university"],
    },
    "northeastern": {
        "name":     "Northeastern University",
        "priority": False,
        "urls": [
            "https://admissions.northeastern.edu/apply/transfer-students/",
        ],
        "keywords": ["northeastern"],
    },
    "purdue": {
        "name":     "Purdue University",
        "priority": False,
        "urls": [
            "https://www.admissions.purdue.edu/transfer/index.php",
        ],
        "keywords": ["purdue"],
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# 🌐  웹 스크래퍼 — 공식 .edu 페이지만 신뢰
# ══════════════════════════════════════════════════════════════════════════════
_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# 공식 페이지당 최대 글자 수 (LLM 컨텍스트 윈도우 보호)
_MAX_PAGE_CHARS = 8000


def fetch_url(url: str, timeout: float = WEB_TIMEOUT_SEC) -> tuple[str, str]:
    """URL을 GET하여 (정제된 텍스트, 최종_URL)을 반환. 실패 시 ("", url)."""
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=8.0, read=timeout, write=8.0, pool=5.0),
            headers=_WEB_HEADERS,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            final_url = str(r.url)
            raw_html  = r.text

        soup = BeautifulSoup(raw_html, "lxml")

        # 노이즈 제거 (스크립트·스타일·네비·푸터)
        for tag in soup(["script", "style", "nav", "footer",
                          "header", "noscript", "aside", "iframe"]):
            tag.decompose()

        # 주요 컨텐츠 영역 우선 탐색
        main_area = (
            soup.find("main")
            or soup.find(id=re.compile(r"(main|content|primary)", re.I))
            or soup.find("article")
            or (soup.body or soup)
        )
        text = main_area.get_text(separator="\n", strip=True)

        # 연속된 빈 줄·공백 정제
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)

        return text.strip()[:_MAX_PAGE_CHARS], final_url

    except httpx.HTTPStatusError as e:
        logging.warning(f"[WEB] HTTP {e.response.status_code} → {url}")
        return "", url
    except Exception as e:
        logging.warning(f"[WEB] 접근 실패 → {url} | {e}")
        return "", url


def fetch_university_pages(uni_key: str) -> list[dict]:
    """레지스트리에서 해당 대학의 모든 공식 URL을 스크래핑하여 결과 리스트 반환."""
    meta = UNIVERSITY_REGISTRY.get(uni_key, {})
    if not meta:
        return []

    results = []
    for url in meta.get("urls", []):
        text, final_url = fetch_url(url)
        if text.strip():
            results.append({
                "university": meta["name"],
                "url":        final_url,
                "text":       text,
            })
        time.sleep(0.4)  # 서버 부하 방지

    return results


def detect_universities_in_query(query: str) -> list[str]:
    """쿼리에서 언급된 대학 키 목록을 반환한다.
    특정 대학이 없으면 이미 지원 완료한 대학 전체를 기본 반환.
    Cornell은 최우선(첫 번째)으로 배치한다."""
    q = query.lower()
    found = [
        key for key, meta in UNIVERSITY_REGISTRY.items()
        if any(kw in q for kw in meta["keywords"])
    ]
    if not found:
        found = list(APPLIED_UNIVERSITIES)

    # cornell 최우선 배치
    if TOP_PRIORITY in found:
        found.remove(TOP_PRIORITY)
        found.insert(0, TOP_PRIORITY)

    return found


# ══════════════════════════════════════════════════════════════════════════════
# 🗄️  RAG 검색 (Qdrant — 사용자 개인 자료)
# ══════════════════════════════════════════════════════════════════════════════
def _build_rag_index() -> VectorStoreIndex:
    qclient       = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
    vector_store  = QdrantVectorStore(client=qclient, collection_name="omni_persona_v3")
    storage_ctx   = StorageContext.from_defaults(vector_store=vector_store)
    embed_model   = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)
    return VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        storage_context=storage_ctx,
        embed_model=embed_model,
    )


def retrieve_user_context(index: VectorStoreIndex, query: str) -> tuple[str, list[str]]:
    """사용자 개인 자료에서 관련 청크를 검색한다."""
    retriever = index.as_retriever(similarity_top_k=TOP_K)
    try:
        nodes = retriever.retrieve(query)
    except Exception as e:
        logging.warning(f"[RAG] 검색 오류: {e}")
        return "", []

    texts, paths = [], []
    for node in nodes:
        t = node.get_content()
        p = (node.metadata or {}).get("file_path", "")
        if t: texts.append(t)
        if p: paths.append(os.path.abspath(p))

    merged = "\n\n".join(texts)[:MAX_CONTEXT_CHARS]
    return merged, sorted(set(paths))


# ══════════════════════════════════════════════════════════════════════════════
# 🧠  프롬프트 빌더
# ══════════════════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = textwrap.dedent("""
You are a **US University Transfer Admission Fact-Check Agent**.
Your sole mission: deliver 100% verified facts about transfer requirements.
Zero hallucination. Zero guessing. Every claim must be backed by a direct quote.

## Absolute Rules (never violate)
1. **Transfer ≠ Freshman**: Reference ONLY "Transfer (International)" pages.
   Citing freshman data as transfer data is a critical error — mark it explicitly.
2. **Official Source Only**: Trust ONLY content served from official university domains
   (e.g., admissions.cornell.edu, admission.stanford.edu, admissions.upenn.edu).
   Blog, Reddit, or consulting site data must be cross-checked with official sources.
3. **Mandatory Quotation**: For every requirement, paste the EXACT sentence(s) from
   the official page inside `>` blockquotes before interpreting them.
4. **No Speculation**: If data is absent or ambiguous, write:
   > "Data unavailable — please confirm directly with the admissions office."
   Never write "it is likely", "probably", or "should be".
5. **Precision Mode**: Think step-by-step. Temperature = 0, Top-p = maximum precision.

## Student Profile (always check against this)
- Current school : Korea University (고려대학교), Artificial Intelligence major
- High school    : Hansung Science High School (한성과학고) — elite STEM school
- GPA            : 3.95 / 4.5 (≈ 3.56 / 4.0 on US scale)
- TOEFL iBT      : 85
- Duolingo (DET) : not yet taken / unknown
- SAT / ACT      : NOT TAKEN
- Credits earned : ~60 (completed 1+ year — typically qualifies for SAT waiver)
- Applied already: Cornell (TOP PRIORITY ★), Stanford, NYU, UPenn

## Checklist — run for EVERY university
1. **SAT/ACT Requirement**
   - Is it waived for transfer students with 30+ credit hours (1+ year)?
   - Quote the exact waiver policy sentence.
2. **English Proficiency**
   - Does TOEFL iBT 85 meet the minimum score requirement?
   - Is Duolingo (DET) accepted? If yes, what is the minimum DET score?
   - If TOEFL 85 is BELOW minimum, flag with ❌ WARNING.
3. **Application Deadline (Fall 2026)**
   - What is the exact Fall 2026 transfer deadline?
   - If already passed, is Late Application / Spring 2027 transfer available?
4. **Holistic Review Policy**
   - Does the admissions process explicitly allow a strong STEM academic background
     (science high school + top Korean university AI program) to offset lower English scores?
   - Quote any flexibility language.
5. **Required Documents**
   - List every document required for international transfer applicants.

## Output Format (strict — always use this structure)

### [University Name] — Transfer Fact Sheet
**결론 (Conclusion)**: `Yes — eligible` / `No — ineligible` / `Late Only` / `Deadline Passed / Unknown`

| 항목 | 내용 |
|------|------|
| SAT/ACT 필요 여부 | Required / Waived for 30+ credits / Test-Optional |
| TOEFL 85 충족 여부 | ✅ Meets minimum / ❌ Below minimum (required: XX) |
| 듀오링고(DET) 허용 | Yes (min: XX) / No / Unknown |
| 마감일 (Fall 2026) | YYYY-MM-DD or Passed / Unknown |
| 필요 서류 | Transcripts, Essay, Recommendations, ... |

**공식 문구 인용 (Official Quotes)**:
> (exact sentence from official .edu page)
— *Source: [URL]*

**⚠️ 경고 (Warnings)**:
(List every conflict between the student profile and the university's requirements,
 each prefixed with 🔴)

---
""").strip()


def _build_user_message(
    query:        str,
    web_sections: list[dict],
    rag_context:  str,
    source_paths: list[str],
) -> str:
    parts = [f"## 사용자 질문\n{query}\n"]

    if web_sections:
        parts.append("## 공식 대학 웹사이트 데이터 (Official .edu Sources)")
        for section in web_sections:
            parts.append(
                f"### {section['university']}\n"
                f"**출처 URL**: {section['url']}\n\n"
                f"{section['text']}\n"
            )

    if rag_context.strip():
        parts.append("## 사용자 개인 자료 (Personal Documents via RAG)")
        parts.append(rag_context)
        if source_paths:
            parts.append(
                "\n**개인 자료 파일 경로:**\n"
                + "\n".join(f"- {p}" for p in source_paths)
            )

    if not web_sections and not rag_context.strip():
        parts.append(
            "⚠️ 수집된 데이터 없음: 웹 접근에 실패했거나 개인 자료 인덱스가 비어 있습니다.\n"
            "입학처에 직접 문의하세요."
        )

    parts.append(
        "\n## 지시사항\n"
        "위 데이터를 기반으로 체크리스트 5개 항목을 모두 분석하고 "
        "정해진 Fact Sheet 형식으로 답변하세요.\n"
        "- 모든 요건은 공식 인용구(>blockquote)와 함께 제시하세요.\n"
        "- 학생 프로필(TOEFL 85, SAT 없음)과 충돌하는 요건은 반드시 🔴 경고로 표시하세요.\n"
        "- 데이터가 없는 항목은 '데이터 없음 — 입학처 확인 필요'라고 명시하세요."
    )

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 🎯  어드바이저 프롬프트 & 메시지 빌더
# ══════════════════════════════════════════════════════════════════════════════
_ADVISOR_SYSTEM_PROMPT = textwrap.dedent("""
You are **Alex**, a senior US transfer admissions consultant with 15 years of experience
placing 1,000+ international students at Ivy League and top-25 universities.
You speak Korean fluently and always respond in **Korean**.

## Your Expertise
- Deep knowledge of actual (unofficial) admission patterns, not just written policies
- Understanding of how Korean elite academic backgrounds are perceived by US admissions offices
- Holistic evaluation: GPA trajectory, major fit, narrative strength, extracurriculars
- Strategic advice: where to apply, when to apply, how to frame weaknesses

## Core Principles
1. **Be a real advisor, not a robot**: Give candid, experience-based opinions.
   If TOEFL 85 is genuinely risky, say so directly — but also explain WHY and what to do.
2. **Data-grounded analysis**: Base probability estimates on the provided fact sheet data
   and the student's actual profile. Never fabricate statistics.
3. **Strengths before weaknesses**: Always identify what makes this student competitive.
4. **Actionable recommendations**: Every opinion must end with a concrete next step.
5. **Acknowledge uncertainty**: If admission data is unavailable, say so,
   but still give strategic context based on comparable schools.

## Student Profile (memorize this)
- 현재 학교  : 고려대학교 인공지능학과 (Korea University, AI major)
  - 고려대 AI학과는 국내 최상위 CS 프로그램 중 하나, 미국 대학원 진학률 높음
- 출신 고교  : 한성과학고등학교 (전국 최상위 과학고 — 수학·과학 경쟁력 증명됨)
- GPA       : 3.95 / 4.5 (미국 환산 ≈ 3.56 / 4.0 WES 기준)
  - ⚠️ 4.5 만점 스케일은 미국 4.0 기준보다 높아 보임 → 반드시 WES 환산 명시
- TOEFL iBT : 85점 (많은 명문대 최저 100점 — 핵심 약점)
- SAT/ACT   : 미응시 (1년 이상 이수 → 대부분 편입생 면제 대상)
- 이수 학점  : ~60학점 (1년 이상) → SAT 면제 대부분 충족
- 지원 완료  : Cornell ★최우선, Stanford, NYU, UPenn

## Analysis Framework (항상 이 순서로 분석)
1. **이 학생의 강점 (Strengths)**: 한성과고 + 고대AI + 고GPA의 경쟁력
2. **핵심 리스크 (Critical Risks)**: TOEFL 85의 실질적 영향
3. **합격 가능성 평가 (Realistic Assessment)**: Reach / Target / Safety 분류
   - 수치 제시 시 반드시 근거 명시 (예: 평균 합격 TOEFL, 합격자 GPA 범위)
4. **전략적 제안 (Strategic Advice)**: 구체적 액션 플랜
5. **주의사항 (Watch-outs)**: 놓치기 쉬운 함정

## Output Format
답변은 한국어로, 아래 구조를 따르되 자연스러운 어드바이저 말투로 작성.

### 🎯 [대학명] — 어드바이저 전략 분석

**한 줄 요약**: (이 학생에게 이 대학이 어떤 의미인지 한 문장)

#### ✅ 이 학생의 강점
(구체적으로 — 한성과고, 고대AI, GPA, 전공 관련성)

#### 🔴 핵심 리스크 & 현실적 평가
(TOEFL 부족, 실제 합격 가능성, Reach/Target/Safety)

#### 💡 전략적 제안
(지금 당장 할 수 있는 구체적 액션 3가지)

#### ⚠️ 주의사항
(놓치면 안 되는 것들)
---
""").strip()


def _build_advisor_message(
    query:        str,
    web_sections: list[dict],
    rag_context:  str,
    source_paths: list[str],
) -> str:
    """어드바이저 모드용 프롬프트 — 팩트 데이터를 전략 분석 재료로 활용."""
    parts = [f"## 학생 질문\n{query}\n"]

    if web_sections:
        parts.append("## 공식 요건 데이터 (팩트체크 에이전트가 수집한 원본 데이터)")
        for section in web_sections:
            parts.append(
                f"### {section['university']}\n"
                f"출처: {section['url']}\n\n"
                f"{section['text']}\n"
            )

    if rag_context.strip():
        parts.append("## 학생 개인 자료 (RAG — 준비 현황, 스펙, 메모 등)")
        parts.append(rag_context)
    
    if not web_sections and not rag_context.strip():
        parts.append(
            "⚠️ 수집된 데이터 없음 — 아래 프로필 정보만으로 전략 분석을 진행합니다.\n"
            "(웹 데이터 없이 분석하므로 요건 수치는 반드시 입학처에서 재확인하세요)"
        )

    parts.append(
        "\n## 분석 지시사항\n"
        "위 데이터와 학생 프로필을 바탕으로:\n"
        "1) 이 학생이 해당 대학(들)에 얼마나 경쟁력 있는지 솔직하게 평가하라.\n"
        "2) TOEFL 85의 실질적 영향을 명확히 분석하라 (합격자 평균과 비교).\n"
        "3) 한성과고 + 고대AI + 3.95GPA 조합이 어떤 서사(narrative)를 만드는지 설명하라.\n"
        "4) Reach/Target/Safety 분류를 Fact 데이터 근거와 함께 제시하라.\n"
        "5) 지금 당장 실행 가능한 전략 3가지를 구체적으로 제안하라."
    )

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 🚀  핵심 파이프라인
# ══════════════════════════════════════════════════════════════════════════════
_rag_index_cache: VectorStoreIndex | None = None


def _get_rag_index() -> "VectorStoreIndex | None":
    global _rag_index_cache
    if _rag_index_cache is None:
        try:
            print("🗄️  Qdrant RAG 인덱스 로딩 중...", flush=True)
            _rag_index_cache = _build_rag_index()
            print("✅ RAG 인덱스 준비 완료.", flush=True)
        except Exception as e:
            logging.warning(f"[RAG] 인덱스 로딩 실패 (계속 진행): {e}")
    return _rag_index_cache


def run_transfer_query(query: str, verbose: bool = True) -> str:
    """편입 팩트체크 파이프라인을 실행하고 마크다운 결과 문자열을 반환한다."""

    # ── 1) 대학 감지 ─────────────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    if verbose:
        names = ", ".join(
            UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys
        )
        print(f"\n🔍 검색 대상 대학: {names}", flush=True)

    # ── 2) 공식 .edu 스크래핑 ────────────────────────────────────────────────
    web_sections: list[dict] = []
    for key in uni_keys:
        uni_name = UNIVERSITY_REGISTRY.get(key, {}).get("name", key)
        if verbose:
            print(f"   🌐 {uni_name} 공식 페이지 접근 중...", flush=True)
        web_sections.extend(fetch_university_pages(key))

    if verbose:
        print(f"   ✅ 웹 수집 완료: {len(web_sections)}개 페이지", flush=True)

    # ── 3) RAG 검색 (사용자 개인 자료) ──────────────────────────────────────
    rag_context, source_paths = "", []
    idx = _get_rag_index()
    if idx is not None:
        if verbose:
            print("   🗂️  개인 자료 RAG 검색 중...", flush=True)
        rag_context, source_paths = retrieve_user_context(idx, query)
        if verbose:
            print(f"   ✅ RAG 완료: {len(source_paths)}개 파일 참조", flush=True)

    # ── 4) 프롬프트 조립 ─────────────────────────────────────────────────────
    user_msg = _build_user_message(query, web_sections, rag_context, source_paths)

    # ── 5) LLM 추론 (temperature=0, top_p=1 — 정확도 최우선) ────────────────
    if verbose:
        print(f"\n🧠  [{CHAT_MODEL}] 팩트체크 분석 중...", flush=True)

    try:
        llm_client = ollama.Client(host=LOCAL_OLLAMA_URL)
        res = llm_client.chat(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": 0,
                "top_p":       1,
                "num_ctx":     16384,
            },
            keep_alive=0,
        )
        answer: str = res.get("message", {}).get("content", "").strip()
        if not answer:
            answer = "❌ LLM이 응답을 반환하지 않았습니다. 모델 상태를 확인하세요."
    except Exception as e:
        answer = (
            f"❌ LLM 추론 실패: {e}\n\n"
            "입학처에 직접 이메일로 문의하세요."
        )

    # ── 6) 메타 푸터 ─────────────────────────────────────────────────────────
    footer_lines = [
        "\n\n---",
        f"**검색 대학**: {', '.join(UNIVERSITY_REGISTRY.get(k, {}).get('name', k) for k in uni_keys)}",
        f"**웹 수집 페이지 수**: {len(web_sections)}",
        f"**RAG 참조 파일 수**: {len(source_paths)}",
    ]
    if source_paths:
        footer_lines.append("\n**개인 자료 경로 (상위 5개)**:")
        footer_lines.extend(f"  - {p}" for p in source_paths[:5])

    return answer + "\n".join(footer_lines)


def run_advisor_query(query: str, verbose: bool = True) -> str:
    """어드바이저 파이프라인: 팩트 수집 후 전략적 합격 분석을 반환한다."""

    # ── 1) 대학 감지 ─────────────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    if verbose:
        names = ", ".join(UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys)
        print(f"\n🎯 어드바이저 분석 대상: {names}", flush=True)

    # ── 2) 공식 .edu 스크래핑 ────────────────────────────────────────────────
    web_sections: list[dict] = []
    for key in uni_keys:
        uni_name = UNIVERSITY_REGISTRY.get(key, {}).get("name", key)
        if verbose:
            print(f"   🌐 {uni_name} 요건 수집 중...", flush=True)
        web_sections.extend(fetch_university_pages(key))
    if verbose:
        print(f"   ✅ 웹 수집 완료: {len(web_sections)}개 페이지", flush=True)

    # ── 3) RAG 검색 (개인 자료 — 활동, 수상, 에세이 소재 등) ────────────────
    rag_context, source_paths = "", []
    idx = _get_rag_index()
    if idx is not None:
        if verbose:
            print("   🗂️  개인 자료 RAG 검색 중 (활동 내역, 수상 등)...", flush=True)
        # 어드바이저 모드는 학생의 강점 자료가 중요 → 더 많이 검색
        retriever = idx.as_retriever(similarity_top_k=min(TOP_K * 2, 12))
        try:
            nodes = retriever.retrieve(query)
            texts, paths = [], []
            for node in nodes:
                t = node.get_content()
                p = (node.metadata or {}).get("file_path", "")
                if t: texts.append(t)
                if p: paths.append(os.path.abspath(p))
            rag_context = "\n\n".join(texts)[:MAX_CONTEXT_CHARS]
            source_paths = sorted(set(paths))
        except Exception as e:
            logging.warning(f"[RAG-advisor] {e}")
        if verbose:
            print(f"   ✅ RAG 완료: {len(source_paths)}개 파일 참조", flush=True)

    # ── 4) 프롬프트 조립 (어드바이저 전용) ──────────────────────────────────
    user_msg = _build_advisor_message(query, web_sections, rag_context, source_paths)

    # ── 5) LLM 추론 (temperature=0.15 — 약간의 자연스러움 허용) ─────────────
    if verbose:
        print(f"\n🎯  [{ADVISOR_MODEL}] 어드바이저 분석 중...", flush=True)

    try:
        llm_client = ollama.Client(host=LOCAL_OLLAMA_URL)
        res = llm_client.chat(
            model=ADVISOR_MODEL,
            messages=[
                {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": 0.15,   # 팩트체크와 달리 약간의 유연성 허용
                "top_p":       0.9,
                "num_ctx":     16384,
            },
            keep_alive=0,
        )
        answer: str = res.get("message", {}).get("content", "").strip()
        if not answer:
            answer = "❌ 어드바이저 응답 없음 — 모델 상태를 확인하세요."
    except Exception as e:
        answer = f"❌ 어드바이저 LLM 오류: {e}"

    # ── 6) 메타 푸터 ─────────────────────────────────────────────────────────
    footer_lines = [
        "\n\n---",
        f"**분석 모델**: `{ADVISOR_MODEL}` (temperature=0.15)",
        f"**분석 대학**: {', '.join(UNIVERSITY_REGISTRY.get(k, {}).get('name', k) for k in uni_keys)}",
        f"**웹 수집**: {len(web_sections)}페이지  |  **RAG 참조**: {len(source_paths)}파일",
        "> ⚠️ 어드바이저 분석은 전략적 의견입니다. 공식 요건은 팩트체크 탭에서 확인하세요.",
    ]
    return answer + "\n".join(footer_lines)


# ══════════════════════════════════════════════════════════════════════════════
# 🖥️  CLI 인터페이스
# ══════════════════════════════════════════════════════════════════════════════
def _print_banner() -> None:
    sep = "=" * 72
    print(sep)
    print("  🎓  미국 명문대 편입 팩트체크 에이전트  v1.0")
    print(f"  ★ 최우선: Cornell  |  지원 완료: Stanford · NYU · UPenn")
    print(f"  학생 프로필: {USER_PROFILE['school']} | GPA {USER_PROFILE['gpa']}")
    print(f"               TOEFL iBT {USER_PROFILE['toefl']}  |  SAT {USER_PROFILE['sat']}")
    print(sep)
    print("  질문 예시:")
    print("    코넬 편입 마감일과 필요 서류 알려줘")
    print("    SAT 없이 지원 가능한 학교 리스트")
    print("    토플 85로 스탠포드 편입 가능해?")
    print("    듀오링고 허용하는 상위권 대학은?")
    print("    Georgia Tech 국제 편입 요건")
    print("  종료: exit")
    print(sep)


if __name__ == "__main__":
    _print_banner()

    while True:
        try:
            query = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not query:
            continue
        if query.lower() in ("exit", "quit", "종료", "q"):
            print("종료합니다.")
            break

        result = run_transfer_query(query, verbose=True)
        print("\n" + result + "\n")
