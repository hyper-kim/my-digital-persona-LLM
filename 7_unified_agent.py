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
  SEARCH_MODEL  = qwen3:14b           (temperature=0)    — 팩트+한국어 최강
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
import importlib
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import ollama
from bs4 import BeautifulSoup
from dotenv import dotenv_values, load_dotenv

# 인코딩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=False)
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
for _env_key in [
    "SEARCH_MODEL",
    "ADVISOR_MODEL",
    "ADVISOR_TEMPERATURE",
    "ADVISOR_TOP_P",
    "ADVISOR_REPEAT_PENALTY",
]:
    _env_value = dotenv_values(_ENV_PATH).get(_env_key)
    if _env_value:
        os.environ[_env_key] = _env_value

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("unified_agent")

try:
    email_agent = importlib.import_module("9_email_agent")
except Exception:
    email_agent = None

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
# qwen3:14b = 한국어+팩트 모두 최강 (ollama pull qwen3:14b, 8.2GB)
SEARCH_MODEL   = os.getenv("SEARCH_MODEL",   "qwen3:14b")

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
ALLOW_TRUSTED_FALLBACK = os.getenv("ALLOW_TRUSTED_FALLBACK", "true").lower() == "true"
ALLOW_COMMUNITY_SOURCES = os.getenv("ALLOW_COMMUNITY_SOURCES", "true").lower() == "true"
COMMUNITY_MAX_RESULTS = int(os.getenv("COMMUNITY_MAX_RESULTS", "3"))
KR_COMMUNITY_MAX_RESULTS = int(os.getenv("KR_COMMUNITY_MAX_RESULTS", "3"))
ENABLE_EMAIL_INBOX_CONTEXT = os.getenv("ENABLE_EMAIL_INBOX_CONTEXT", "true").lower() == "true"
EMAIL_INBOX_PROVIDER = os.getenv("EMAIL_INBOX_PROVIDER", "auto")
EMAIL_INBOX_COUNT = int(os.getenv("EMAIL_INBOX_COUNT", "30"))
AUTO_SEND_CRITICAL_EMAIL = os.getenv("AUTO_SEND_CRITICAL_EMAIL", "false").lower() == "true"
USE_LLM_FOR_INQUIRY_DRAFT = os.getenv("USE_LLM_FOR_INQUIRY_DRAFT", "false").lower() == "true"

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
    "applied":    [
        "cornell", "stanford", "nyu", "upenn", "northwestern", "uiuc",
        "purdue", "uchicago",
        # 엑셀 후보군 전체
        "harvard", "princeton", "yale", "mit", "caltech",
        "duke", "brown", "johns_hopkins", "columbia", "carnegie",
        "washu", "uc_berkeley", "usc", "ucla", "ut_austin",
        "georgia_tech", "michigan", "unc", "bu", "uw",
        "uc_san_diego", "wisconsin", "penn_state", "umd", "osu",
        "rutgers", "umass",
    ],
}

APPLIED_UNIVERSITIES = set(USER_PROFILE["applied"])
PRIMARY_UNIVERSITY_KEYS = USER_PROFILE["applied"][:8]  # 직접 지원 8개

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
        "portals": [
            {"url": "https://apply.cornell.edu/apply/", "use_chrome": True, "click_button": "Sign In"},
        ],
    },
    "stanford": {
        "name": "Stanford University",
        "priority": True,
        "urls": [
            "https://admission.stanford.edu/apply/transfer/",
            "https://admission.stanford.edu/apply/requirements/tests.html",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "nyu": {
        "name": "New York University",
        "priority": True,
        "urls": [
            "https://www.nyu.edu/admissions/undergraduate-admissions/how-to-apply.html",
            "https://www.nyu.edu/admissions/undergraduate-admissions/important-dates-and-deadlines.html",
        ],
        "portals": [
            {"url": "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate", "use_chrome": True, "click_button": "Login"},
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
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "mit": {
        "name": "MIT",
        "priority": False,
        "urls": ["https://admissions.mit.edu/apply/transfer-applicants"],
        "portals": [
            {"url": "https://apply.mitadmissions.org/apply/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "columbia": {
        "name": "Columbia University",
        "priority": False,
        "urls": ["https://undergrad.admissions.columbia.edu/apply/transfer"],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "uc_berkeley": {
        "name": "UC Berkeley",
        "priority": False,
        "urls": [
            # admissions.berkeley.edu는 Cloudflare로 차단 → UC 시스템 공식 편입 안내
            "https://admission.universityofcalifornia.edu/how-to-apply/applying-as-a-transfer/",
            "https://admission.universityofcalifornia.edu/",
        ],
        "portals": [
            {"url": "https://apply.universityofcalifornia.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "ucla": {
        "name": "UCLA",
        "priority": False,
        "urls": ["https://admission.ucla.edu/apply/transfer"],
        "portals": [
            {"url": "https://apply.universityofcalifornia.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "uchicago": {
        "name": "University of Chicago (UChicago)",
        "priority": True,
        "urls": [
            "https://collegeadmissions.uchicago.edu/apply/transfer-students",
            "https://collegeadmissions.uchicago.edu/apply",
            "https://collegeadmissions.uchicago.edu/apply/requirements",
        ],
        "portals": [
            {"url": "https://applyingtomaroon.uchicago.edu/", "use_chrome": True, "click_button": "Transfer"},
        ],
    },
    "carnegie": {
        "name": "Carnegie Mellon University",
        "priority": False,
        "urls": ["https://admission.enrollment.cmu.edu/pages/transfer-admission"],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "georgia_tech": {
        "name": "Georgia Tech",
        "priority": False,
        "urls": ["https://admission.gatech.edu/transfer"],
        "portals": [
            {"url": "https://admission.gatech.edu/apply", "use_chrome": True, "click_button": "Apply"},
        ],
    },
    "purdue": {
        "name": "Purdue University",
        "priority": True,
        "urls": [
            "https://www.admissions.purdue.edu/transfer/index.php",
            "https://www.admissions.purdue.edu/transfer/international-transfer-students.php",
            "https://www.admissions.purdue.edu/transfer/transfer-faq.php",
        ],
        "portals": [
            {"url": "https://admissions.purdue.edu/apply/transfer/", "use_chrome": True, "click_button": "Apply"},
        ],
    },
    "northwestern": {
        "name": "Northwestern University",
        "priority": True,
        "urls": [
            "https://admissions.northwestern.edu/apply/identities/transfer.html",
            "https://admissions.northwestern.edu/faqs/transferring-to-northwestern/index.html",
            "https://admissions.northwestern.edu/apply/requirements/index.html",
        ],
        "portals": [
            {"url": "https://apply.northwestern.edu/", "use_chrome": True, "click_button": "Transfer"},
        ],
    },
    "uiuc": {
        "name": "University of Illinois Urbana-Champaign (UIUC)",
        "priority": True,
        "urls": [
            "https://admissions.illinois.edu/Apply/Transfer",
            "https://admissions.illinois.edu/Requirements/Transfer",
            "https://cs.illinois.edu/admissions/transfer/transfer-applicants",
        ],
        "portals": [
            {"url": "https://myillini.illinois.edu/", "use_chrome": True, "click_button": "Login"},
        ],
    },
    # ── 엑셀 후보군 추가 ────────────────────────────────────────────────────
    "harvard": {
        "name": "Harvard University",
        "priority": False,
        "urls": [
            "https://college.harvard.edu/admissions/apply/transfer-applicants",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "princeton": {
        "name": "Princeton University",
        "priority": False,
        "urls": [
            "https://admission.princeton.edu/apply/transfer-applicants",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "yale": {
        "name": "Yale University",
        "priority": False,
        "urls": [
            "https://admissions.yale.edu/transfer",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "caltech": {
        "name": "California Institute of Technology",
        "priority": False,
        "urls": [
            "https://admissions.caltech.edu/apply/transfer",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "duke": {
        "name": "Duke University",
        "priority": False,
        "urls": [
            "https://admissions.duke.edu/apply/transfer-applicants",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "brown": {
        "name": "Brown University",
        "priority": False,
        "urls": [
            "https://admission.brown.edu/apply/transfer",
        ],
        "portals": [
            {"url": "https://applyonline.brown.edu/register/transfer", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "johns_hopkins": {
        "name": "Johns Hopkins University",
        "priority": False,
        "urls": [
            "https://apply.jhu.edu/apply/transfer-students/",
        ],
        "portals": [
            {"url": "https://apply.jhu.edu/", "use_chrome": True, "click_button": "Sign In"},
        ],
    },
    "washu": {
        "name": "Washington University in St. Louis",
        "priority": False,
        "urls": [
            "https://admissions.wustl.edu/apply/transfer/",
        ],
        "portals": [
            {"url": "https://apply.wustl.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "usc": {
        "name": "University of Southern California",
        "priority": False,
        "urls": [
            "https://admission.usc.edu/apply/transfer-students/",
        ],
        "portals": [
            {"url": "https://apply.usc.edu/", "use_chrome": True, "click_button": "Sign In"},
        ],
    },
    "ut_austin": {
        "name": "University of Texas at Austin",
        "priority": False,
        "urls": [
            "https://admissions.utexas.edu/apply/transfer-students",
        ],
        "portals": [
            {"url": "https://goapplytexas.org/", "use_chrome": True, "click_button": "Login"},
        ],
    },
    "michigan": {
        "name": "University of Michigan",
        "priority": False,
        "urls": [
            "https://admissions.umich.edu/apply-now/applying-transfer-students",
        ],
        "portals": [
            {"url": "https://apply.umich.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "unc": {
        "name": "UNC Chapel Hill",
        "priority": False,
        "urls": [
            "https://admissions.unc.edu/apply/transfer-students/",
        ],
        "portals": [
            {"url": "https://apply.unc.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "bu": {
        "name": "Boston University",
        "priority": False,
        "urls": [
            "https://www.bu.edu/admissions/apply/transfer/",
        ],
        "portals": [
            {"url": "https://apply.commonapp.org/apply", "use_chrome": True, "click_button": "Sign in"},
        ],
    },
    "uw": {
        "name": "University of Washington",
        "priority": False,
        "urls": [
            "https://admit.uw.edu/apply/transfer/",
        ],
        "portals": [
            {"url": "https://apply.uw.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "uc_san_diego": {
        "name": "UC San Diego",
        "priority": False,
        "urls": [
            "https://admissions.ucsd.edu/transfer/",
        ],
        "portals": [
            {"url": "https://apply.universityofcalifornia.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "wisconsin": {
        "name": "University of Wisconsin-Madison",
        "priority": False,
        "urls": [
            "https://admissions.wisc.edu/transfer/",
        ],
        "portals": [
            {"url": "https://apply.wisc.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "penn_state": {
        "name": "Penn State University",
        "priority": False,
        "urls": [
            "https://admissions.psu.edu/apply/transfer/",
        ],
        "portals": [
            {"url": "https://admissions.psu.edu/apply/", "use_chrome": True, "click_button": "Apply"},
        ],
    },
    "umd": {
        "name": "University of Maryland",
        "priority": False,
        "urls": [
            "https://admissions.umd.edu/apply/transfer-students",
        ],
        "portals": [
            {"url": "https://apply.umd.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "osu": {
        "name": "Ohio State University",
        "priority": False,
        "urls": [
            "https://undergrad.osu.edu/apply/transfer",
        ],
        "portals": [
            {"url": "https://undergrad.osu.edu/apply", "use_chrome": True, "click_button": "Login"},
        ],
    },
    "rutgers": {
        "name": "Rutgers University",
        "priority": False,
        "urls": [
            "https://admissions.rutgers.edu/apply/transfer-students",
        ],
        "portals": [
            {"url": "https://apply.rutgers.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
    "umass": {
        "name": "UMass Amherst",
        "priority": False,
        "urls": [
            "https://www.umass.edu/admissions/apply/transfer",
        ],
        "portals": [
            {"url": "https://apply.massachusetts.edu/", "use_chrome": True, "click_button": "Log in"},
        ],
    },
}

UNIVERSITY_ALIASES: dict[str, list[str]] = {
    "cornell": ["cornell", "코넬"],
    "stanford": ["stanford", "스탠포드", "스탠퍼드"],
    "nyu": ["nyu", "new york university", "뉴욕대", "뉴욕대학교"],
    "upenn": ["upenn", "university of pennsylvania", "유펜"],
    "mit": ["mit", "massachusetts institute of technology", "매사추세츠 공대"],
    "columbia": ["columbia", "컬럼비아"],
    "uc_berkeley": ["uc berkeley", "berkeley", "버클리", "uc버클리", "유씨버클리"],
    "ucla": ["ucla", "유씨엘에이"],
    "uchicago": ["uchicago", "university of chicago", "시카고대", "시카고 대학"],
    "carnegie": ["carnegie mellon", "cmu", "카네기 멜런", "카네기멜런", "카네기"],
    "georgia_tech": ["georgia tech", "georgia institute of technology", "gatech", "gt", "조지아텍", "조지아 텍"],
    "purdue": ["purdue", "퍼듀"],
    "northwestern": ["northwestern", "노스웨스턴", "노스웨스턴대"],
    "uiuc": ["uiuc", "university of illinois urbana champaign", "illinois", "urbana", "일리노이", "어바나샴페인"],
    "harvard": ["harvard", "하버드"],
    "princeton": ["princeton", "프린스턴"],
    "yale": ["yale", "예일"],
    "caltech": ["caltech", "california institute of technology", "칼텍", "캘텍"],
    "duke": ["duke", "듀크"],
    "brown": ["brown", "브라운"],
    "johns_hopkins": ["johns hopkins", "jhu", "존스 홉킨스", "존스홉킨스"],
    "washu": ["washu", "washington university in st. louis", "washington university st louis", "워싱턴대 세인트루이스", "워싱턴대 세인트 루이스", "워슈"],
    "usc": ["usc", "university of southern california", "남가주대", "서던캘리포니아", "유에스씨"],
    "ut_austin": ["ut austin", "utexas", "university of texas at austin", "텍사스 오스틴", "ut오스틴"],
    "michigan": ["university of michigan", "umich", "michigan", "미시간대", "미시간"],
    "unc": ["unc", "unc chapel hill", "university of north carolina chapel hill", "unc 채플힐", "채플힐"],
    "bu": ["boston university", "bu", "보스턴대", "보스턴대학교"],
    "uw": ["university of washington", "uw", "워싱턴대", "워싱턴대학"],
    "uc_san_diego": ["uc san diego", "ucsd", "샌디에이고", "uc 샌디에이고", "유씨샌디에이고"],
    "wisconsin": ["wisconsin", "uw madison", "university of wisconsin madison", "위스콘신대", "위스콘신", "매디슨"],
    "penn_state": ["penn state", "펜스테이트", "펜실베이니아 주립", "펜실베니아 주립"],
    "umd": ["umd", "university of maryland", "메릴랜드대", "메릴랜드"],
    "osu": ["osu", "ohio state", "the ohio state university", "오하이오 주립", "오하이오주립"],
    "rutgers": ["rutgers", "럿거스", "러트거스"],
    "umass": ["umass", "umass amherst", "university of massachusetts amherst", "유매스", "매사추세츠 애머스트"],
}

# ══════════════════════════════════════════════════════════════════════════════
# �  어드바이저 전용 참고 사이트 레지스트리
#     일반 편입 전략·통계·요건 항목을 담은 정보성 사이트.
#     공식 대학 입학처 데이터와 함께 어드바이저 컨텍스트로 사용됨.
# ══════════════════════════════════════════════════════════════════════════════

ADVISOR_REFERENCE_SITES: list[dict] = [
    # ── 종합 편입 전략 가이드 ──────────────────────────────────────────────
    {
        "label": "PrepScholar — 편입 완전 가이드",
        "url":   "https://blog.prepscholar.com/how-to-transfer-colleges",
        "tags":  ["strategy", "general", "timeline"],
    },
    {
        "label": "PrepScholar — 아이비 편입 GPA 기준",
        "url":   "https://blog.prepscholar.com/transfer-to-ivy-league",
        "tags":  ["gpa", "ivy", "stats"],
    },
    {
        "label": "CollegeVine — 편입 어려운가?",
        "url":   "https://www.collegevine.com/faq/22/is-transferring-colleges-hard",
        "tags":  ["strategy", "acceptance_rate"],
    },
    {
        "label": "CollegeVine — 아이비 편입에 필요한 GPA",
        "url":   "https://www.collegevine.com/faq/1074/what-gpa-do-you-need-to-transfer-to-an-ivy-league-school",
        "tags":  ["gpa", "ivy", "stats"],
    },
    {
        "label": "CollegeVine — 국제 편입생 가이드",
        "url":   "https://www.collegevine.com/faq/20701/how-do-international-students-transfer-to-us-colleges",
        "tags":  ["international", "toefl", "strategy"],
    },
    # ── TOEFL / 영어 요건 ─────────────────────────────────────────────────
    {
        "label": "PrepScholar — TOEFL 편입 요건 총정리",
        "url":   "https://blog.prepscholar.com/toefl-scores-for-transfer-students",
        "tags":  ["toefl", "english", "requirements"],
    },
    # ── 에세이·LOR·추천서 전략 ────────────────────────────────────────────
    {
        "label": "CollegeVine — 편입 에세이 가이드",
        "url":   "https://www.collegevine.com/faq/4139/how-to-write-a-transfer-essay",
        "tags":  ["essay", "strategy", "writing"],
    },
    {
        "label": "PrepScholar — 편입 추천서 전략",
        "url":   "https://blog.prepscholar.com/transfer-letter-of-recommendation",
        "tags":  ["recommendation", "lor", "strategy"],
    },
    # ── 한국/아시아 국제학생 편입 특이사항 ───────────────────────────────
    {
        "label": "US News — 국제 편입생 팁",
        "url":   "https://www.usnews.com/education/best-colleges/articles/tips-for-transfer-students",
        "tags":  ["international", "tips", "general"],
    },
    # ── CommonApp 편입 절차 ───────────────────────────────────────────────
    {
        "label": "CommonApp — 편입생 지원 안내",
        "url":   "https://www.commonapp.org/apply/transfer-students",
        "tags":  ["application", "process", "deadline"],
    },
]

# 세션 캐시 (URL → 본문 텍스트) — 매 세션 한 번만 크롤
_advisor_ref_cache: dict[str, str] = {}


def _fetch_advisor_reference_context(query: str, max_sites: int = 4) -> str:
    """
    ADVISOR_REFERENCE_SITES에서 query 관련 사이트를 골라 크롤하고
    통합된 참고 컨텍스트 문자열을 반환한다.
    결과는 세션 캐시에 저장돼 동일 URL은 재요청하지 않는다.
    """
    q = query.lower()

    # 쿼리와 관련성 높은 태그 파악
    tag_hits: dict[str, int] = {}
    for site in ADVISOR_REFERENCE_SITES:
        score = sum(1 for tag in site["tags"] if tag in q)
        # 전략/일반 태그는 항상 가산점
        if "strategy" in site["tags"] or "general" in site["tags"]:
            score += 1
        tag_hits[site["url"]] = score

    # 점수 높은 순으로 정렬, 최대 max_sites
    sorted_sites = sorted(ADVISOR_REFERENCE_SITES, key=lambda s: tag_hits[s["url"]], reverse=True)
    selected = sorted_sites[:max_sites]

    parts: list[str] = []
    for site in selected:
        url = site["url"]
        if url not in _advisor_ref_cache:
            text = fetch_page(url, min_content_score=0, query=query)
            _advisor_ref_cache[url] = text or ""
        cached = _advisor_ref_cache.get(url, "")
        if cached.strip():
            parts.append(
                f"### [{site['label']}]\nURL: {url}\n{cached[:2500]}"
            )

    if not parts:
        return ""
    return "# 편입 전략 참고 자료\n" + "\n\n---\n".join(parts)

# ══════════════════════════════════════════════════════════════════════════════
# �📚  비공식 소스 레지스트리 (공식 사이트 차단 시 fallback)
# 모두 직접 200 OK 확인된 URL들 (JS 없이 정적 HTML 제공)
# ══════════════════════════════════════════════════════════════════════════════

UNOFFICIAL_REGISTRY: dict[str, list[str]] = {
    "cornell": [
        "https://www.collegedata.com/college/Cornell-University",
        "https://niche.com/colleges/cornell-university/",
        "https://www.commonapp.org/explore/cornell-university",
    ],
    "stanford": [
        "https://www.collegedata.com/college/Stanford-University",
        "https://niche.com/colleges/stanford-university/",
    ],
    "nyu": [
        "https://www.collegedata.com/college/New-York-University",
        "https://niche.com/colleges/new-york-university/",
    ],
    "upenn": [
        "https://www.collegedata.com/college/University-of-Pennsylvania",
        "https://niche.com/colleges/university-of-pennsylvania/",
    ],
    "mit": [
        "https://www.collegedata.com/college/Massachusetts-Institute-of-Technology",
        "https://niche.com/colleges/mit/",
    ],
    "columbia": [
        "https://www.collegedata.com/college/Columbia-University",
        "https://niche.com/colleges/columbia-university/",
    ],
    "uc_berkeley": [
        "https://www.collegedata.com/college/University-of-California-Berkeley",
        "https://niche.com/colleges/university-of-california-berkeley/",
    ],
    "ucla": [
        "https://www.collegedata.com/college/University-of-California-Los-Angeles",
        "https://niche.com/colleges/university-of-california-los-angeles/",
    ],
    "uchicago": [
        "https://www.collegedata.com/college/University-of-Chicago",
        "https://niche.com/colleges/university-of-chicago/",
    ],
    "carnegie": [
        "https://www.collegedata.com/college/Carnegie-Mellon-University",
        "https://niche.com/colleges/carnegie-mellon-university/",
    ],
    "georgia_tech": [
        "https://www.collegedata.com/college/Georgia-Institute-of-Technology",
        "https://niche.com/colleges/georgia-institute-of-technology/",
    ],
    "purdue": [
        "https://www.collegedata.com/college/Purdue-University",
        "https://niche.com/colleges/purdue-university/",
    ],
    "northwestern": [
        "https://www.collegedata.com/college/Northwestern-University",
        "https://niche.com/colleges/northwestern-university/",
        "https://www.commonapp.org/explore/northwestern-university",
    ],
    "uiuc": [
        "https://www.collegedata.com/college/University-of-Illinois-Urbana-Champaign",
        "https://niche.com/colleges/university-of-illinois-urbana-champaign/",
        "https://www.commonapp.org/explore/university-of-illinois-urbana-champaign",
    ],
    "harvard": [
        "https://www.collegedata.com/college/Harvard-University",
        "https://niche.com/colleges/harvard-university/",
        "https://www.commonapp.org/explore/harvard-university",
    ],
    "princeton": [
        "https://www.collegedata.com/college/Princeton-University",
        "https://niche.com/colleges/princeton-university/",
    ],
    "yale": [
        "https://www.collegedata.com/college/Yale-University",
        "https://niche.com/colleges/yale-university/",
        "https://www.commonapp.org/explore/yale-university",
    ],
    "caltech": [
        "https://www.collegedata.com/college/California-Institute-of-Technology",
        "https://niche.com/colleges/caltech/",
    ],
    "duke": [
        "https://www.collegedata.com/college/Duke-University",
        "https://niche.com/colleges/duke-university/",
        "https://www.commonapp.org/explore/duke-university",
    ],
    "brown": [
        "https://www.collegedata.com/college/Brown-University",
        "https://niche.com/colleges/brown-university/",
        "https://www.commonapp.org/explore/brown-university",
    ],
    "johns_hopkins": [
        "https://www.collegedata.com/college/Johns-Hopkins-University",
        "https://niche.com/colleges/johns-hopkins-university/",
        "https://www.commonapp.org/explore/johns-hopkins-university",
    ],
    "washu": [
        "https://www.collegedata.com/college/Washington-University-in-St-Louis",
        "https://niche.com/colleges/washington-university-in-st-louis/",
        "https://www.commonapp.org/explore/washington-university-in-st-louis",
    ],
    "usc": [
        "https://www.collegedata.com/college/University-of-Southern-California",
        "https://niche.com/colleges/university-of-southern-california/",
        "https://www.commonapp.org/explore/university-of-southern-california",
    ],
    "ut_austin": [
        "https://www.collegedata.com/college/University-of-Texas-Austin",
        "https://niche.com/colleges/university-of-texas-at-austin/",
    ],
    "michigan": [
        "https://www.collegedata.com/college/University-of-Michigan-Ann-Arbor",
        "https://niche.com/colleges/university-of-michigan/",
        "https://www.commonapp.org/explore/university-of-michigan",
    ],
    "unc": [
        "https://www.collegedata.com/college/University-of-North-Carolina-Chapel-Hill",
        "https://niche.com/colleges/university-of-north-carolina-at-chapel-hill/",
    ],
    "bu": [
        "https://www.collegedata.com/college/Boston-University",
        "https://niche.com/colleges/boston-university/",
        "https://www.commonapp.org/explore/boston-university",
    ],
    "uw": [
        "https://www.collegedata.com/college/University-of-Washington-Seattle",
        "https://niche.com/colleges/university-of-washington/",
    ],
    "uc_san_diego": [
        "https://www.collegedata.com/college/University-of-California-San-Diego",
        "https://niche.com/colleges/university-of-california-san-diego/",
    ],
    "wisconsin": [
        "https://www.collegedata.com/college/University-of-Wisconsin-Madison",
        "https://niche.com/colleges/university-of-wisconsin-madison/",
    ],
    "penn_state": [
        "https://www.collegedata.com/college/Pennsylvania-State-University",
        "https://niche.com/colleges/penn-state-university/",
    ],
    "umd": [
        "https://www.collegedata.com/college/University-of-Maryland-College-Park",
        "https://niche.com/colleges/university-of-maryland/",
    ],
    "osu": [
        "https://www.collegedata.com/college/Ohio-State-University",
        "https://niche.com/colleges/the-ohio-state-university/",
    ],
    "rutgers": [
        "https://www.collegedata.com/college/Rutgers-University-New-Brunswick",
        "https://niche.com/colleges/rutgers-university/",
    ],
    "umass": [
        "https://www.collegedata.com/college/University-of-Massachusetts-Amherst",
        "https://niche.com/colleges/university-of-massachusetts/",
    ],
}


def _split_csv_env(value: str) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _extract_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().strip()
    except Exception:
        return ""


def _normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if d.startswith("http://") or d.startswith("https://"):
        d = _extract_host(d)
    return d


def _host_in_allowed_domains(host: str, allowed_domains: set[str]) -> bool:
    if not host:
        return False
    return any(host == dom or host.endswith(f".{dom}") for dom in allowed_domains)


def _default_allowed_domains(uni_key: str) -> set[str]:
    domains = set()
    for u in UNIVERSITY_REGISTRY.get(uni_key, {}).get("urls", []):
        host = _extract_host(u)
        if host:
            domains.add(host)
    return domains


def _get_allowed_domains(uni_key: str) -> set[str]:
    meta = UNIVERSITY_REGISTRY.get(uni_key, {})
    from_meta = {
        _normalize_domain(d)
        for d in meta.get("allowed_domains", [])
        if _normalize_domain(d)
    }
    if from_meta:
        return from_meta
    return _default_allowed_domains(uni_key)


def _apply_official_source_overrides_from_env() -> None:
    """.env의 OFFICIAL_SOURCE_*_URLS/DOMAINS로 대학별 공식 소스를 덮어쓴다."""
    for uni_key, meta in UNIVERSITY_REGISTRY.items():
        env_prefix = f"OFFICIAL_SOURCE_{uni_key.upper()}"
        urls_env = os.getenv(f"{env_prefix}_URLS", "")
        domains_env = os.getenv(f"{env_prefix}_DOMAINS", "")

        override_urls = _split_csv_env(urls_env)
        override_domains = [_normalize_domain(d) for d in _split_csv_env(domains_env)]
        override_domains = [d for d in override_domains if d]

        if override_urls:
            meta["urls"] = override_urls
        if override_domains:
            meta["allowed_domains"] = override_domains
        elif "allowed_domains" not in meta:
            meta["allowed_domains"] = sorted(_default_allowed_domains(uni_key))


_apply_official_source_overrides_from_env()

# ── DuckDuckGo HTML 검색 fallback ────────────────────────────────────────────

_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_TRANSFER_KEYWORDS = {
    "deadline", "transfer", "requirement", "gpa", "toefl", "credit",
    "admission", "apply", "application", "transcript", "coursework",
    "마감", "편입", "요건", "서류",
}

_SCOPE_LABEL = "Fall 2026 international transfer"
_SCOPE_TERMS = ["fall 2026", "international transfer", "transfer applicants", "international students"]
_OFFICIAL_SEARCH_CACHE: dict[str, list[str]] = {}

_OFFICIAL_FACT_PATTERNS = [
    r"마감(일|일자|날)?",
    r"deadline",
    r"필요.{0,5}서류",
    r"(요건|조건|기준)",
    r"requirement",
    r"required\s*(document|documents|material|materials)",
    r"application\s*(checklist|requirements?)",
    r"recommendation",
    r"transcript",
    r"essay|personal statement",
    r"toefl|ielts|duolingo|sat|act|gpa|credit",
]

_FOCUS_TERM_GROUPS = [
    (
        ["마감", "deadline", "언제", "일정", "봄학기", "가을학기", "rolling"],
        [
            "deadline", "deadlines", "date", "dates", "due", "application deadline",
            "priority deadline", "fall", "spring", "rolling", "decision", "march",
            "april", "january", "february", "november", "december",
        ],
    ),
    (
        ["서류", "required", "필요", "체크리스트", "checklist", "materials", "documents"],
        [
            "required", "requirements", "application checklist", "materials", "document",
            "documents", "transcript", "official transcript", "college report",
            "mid-term report", "recommendation", "recommendations", "essay",
            "personal statement", "test scores", "application fee",
        ],
    ),
    (
        ["toefl", "ielts", "duolingo", "sat", "act", "gpa", "credit", "학점"],
        [
            "toefl", "ielts", "duolingo", "sat", "act", "gpa", "credit",
            "credits", "english proficiency", "test score",
        ],
    ),
]


def _content_score(text: str) -> int:
    """텍스트의 편입 관련 정보 밀도 점수 (높을수록 유용)"""
    t = text.lower()
    return sum(1 for kw in _TRANSFER_KEYWORDS if kw in t)


def _needs_official_precision(query: str) -> bool:
    q = query.lower()
    return any(re.search(pattern, q) for pattern in _OFFICIAL_FACT_PATTERNS)


def _query_focus_terms(query: str) -> list[str]:
    q = query.lower()
    terms = set()
    for triggers, group_terms in _FOCUS_TERM_GROUPS:
        if any(trigger in q for trigger in triggers):
            terms.update(group_terms)
    if not terms and _needs_official_precision(query):
        terms.update(["deadline", "requirements", "required", "document", "transcript"])
    return sorted(terms)


def _extract_query_focused_text(text: str, query: str) -> str:
    focus_terms = _query_focus_terms(query)
    if not focus_terms:
        return text[:MAX_CONTEXT_CHARS]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    keep_indices = set()
    for idx, line in enumerate(lines):
        line_lower = line.lower()
        if any(term in line_lower for term in focus_terms):
            start = max(0, idx - 1)
            end = min(len(lines), idx + 3)
            keep_indices.update(range(start, end))

    if not keep_indices:
        return text[:MAX_CONTEXT_CHARS]

    focused_text = "\n".join(lines[idx] for idx in sorted(keep_indices))
    if len(focused_text) < 200:
        return text[:MAX_CONTEXT_CHARS]
    return focused_text[:MAX_CONTEXT_CHARS]


def _scoped_query(query: str) -> str:
    q = query.strip()
    q_lower = q.lower()
    missing = [term for term in _SCOPE_TERMS if term not in q_lower]
    if not missing:
        return q
    return f"{q} | {_SCOPE_LABEL}"


def _alias_matches_query(query: str, alias: str) -> bool:
    alias = alias.lower().strip()
    if not alias:
        return False
    if re.search(r"[가-힣]", alias):
        return alias in query
    if re.fullmatch(r"[a-z0-9.+-]{1,4}", alias):
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", query) is not None
    return alias in query


def _wants_detailed_citations(query: str) -> bool:
    q = query.lower()
    return any(k in q for k in ["출처", "각주", "source", "citation", "url", "링크", "근거"])


def _wants_email_send(query: str) -> bool:
    q = query.lower()
    return any(k in q for k in ["메일 보내", "이메일 보내", "발송", "send email", "email it", "문의 보내"])


def _is_official_source(src: str) -> bool:
    return src in {"official", "official_registry", "official_search"}


def _collect_inbox_context(uni_keys: list[str]) -> str:
    if not ENABLE_EMAIL_INBOX_CONTEXT or email_agent is None:
        return ""
    try:
        summary = email_agent.summarize_important_inbox(n=EMAIL_INBOX_COUNT, provider=EMAIL_INBOX_PROVIDER)
    except Exception as e:
        log.warning("수신함 요약 실패: %s", e)
        return ""

    if not summary or "중요 메일 없음" in summary or "조회 실패" in summary:
        return ""

    uni_names = [UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys]
    header = "# 대학 수신 메일 요약 (입학처/지원 관련)\n"
    return header + f"대상: {', '.join(uni_names)}\n\n" + summary[:3000]


def _detect_critical_unknowns(query: str, uni_keys: list[str], web_sections: list[dict]) -> list[dict]:
    if not _needs_official_precision(query):
        return []
    q = query.lower()
    if "마감" in q or "deadline" in q:
        topic = "Fall 2026 transfer deadline"
    elif "서류" in q or "document" in q or "checklist" in q:
        topic = "required documents checklist"
    else:
        topic = "transfer requirement details"

    unknowns = []
    by_uni = {}
    for sec in web_sections:
        by_uni.setdefault(sec.get("uni", ""), []).append(sec)

    for key in uni_keys[:4]:
        uni_name = UNIVERSITY_REGISTRY.get(key, {}).get("name", key)
        secs = by_uni.get(uni_name, [])
        official_secs = [s for s in secs if _is_official_source(s.get("source", ""))]
        if not official_secs:
            unknowns.append({"uni_key": key, "uni_name": uni_name, "topic": topic, "reason": "공식 소스 부재"})

    return unknowns


def _build_inquiry_email_plan(unknowns: list[dict], query: str) -> list[dict]:
    plans = []
    if email_agent is None:
        return plans
    for item in unknowns:
        uni_key = item["uni_key"]
        to_addr = email_agent.ADMISSIONS_EMAILS.get(uni_key, "") if hasattr(email_agent, "ADMISSIONS_EMAILS") else ""
        if not to_addr:
            continue
        topic = (
            f"Fall 2026 international transfer inquiry: {item['topic']}"
            f" (official site unresolved: {item['reason']})"
        )
        req = email_agent.EmailRequest(
            email_type="inquiry",
            to_address=to_addr,
            topic=topic,
            school_key=uni_key,
            extra_context=f"Original user question: {query}\nPlease provide official policy details.",
        )
        subject = f"Transfer Application Inquiry — {item['uni_name']}"
        body = (
            "Dear Admissions Team,\n\n"
            f"I am writing to clarify {item['topic']} for Fall 2026 international transfer applicants. "
            "The official page did not clearly specify this detail. "
            "Could you please confirm the exact requirement?\n\n"
            "Thank you for your time and assistance.\nBest regards,"
        )
        if USE_LLM_FOR_INQUIRY_DRAFT:
            draft = ""
            try:
                for chunk in email_agent.stream_compose(req):
                    draft += chunk
                subject, body = email_agent.extract_subject_and_body(draft)
            except Exception:
                pass
        plans.append({
            "uni_key": uni_key,
            "uni_name": item["uni_name"],
            "to": to_addr,
            "subject": subject,
            "body": body,
        })
    return plans


def _maybe_send_inquiry_emails(plans: list[dict], query: str) -> list[dict]:
    if email_agent is None:
        return []
    if not plans:
        return []
    if not AUTO_SEND_CRITICAL_EMAIL:
        return []
    if not _wants_email_send(query):
        return []

    # 다건 발송은 SMTP 연결 1회 재사용 경로 우선 (속도 개선)
    if len(plans) > 1 and hasattr(email_agent, "send_email_batch"):
        try:
            payload = [
                {
                    "to_address": p["to"],
                    "subject": p["subject"],
                    "body": p["body"],
                    "email_type": "inquiry",
                }
                for p in plans
            ]
            batch_results = email_agent.send_email_batch(payload, provider=EMAIL_INBOX_PROVIDER)
            out = []
            for idx, plan in enumerate(plans):
                result = batch_results[idx] if idx < len(batch_results) else {"success": False, "error": "batch result missing"}
                out.append({"uni": plan["uni_name"], "result": result})
            return out
        except Exception as e:
            log.warning("배치 메일 발송 실패, 단건 폴백: %s", e)

    results = []
    for plan in plans:
        try:
            result = email_agent.send_email(
                to_address=plan["to"],
                subject=plan["subject"],
                body=plan["body"],
                email_type="inquiry",
                provider=EMAIL_INBOX_PROVIDER,
            )
            results.append({"uni": plan["uni_name"], "result": result})
        except Exception as e:
            results.append({"uni": plan["uni_name"], "result": {"success": False, "error": str(e)}})
    return results


def get_official_source_policy_markdown(applied_only: bool = True) -> str:
    """지원 대학별 고정 공식 소스 정책을 Markdown으로 반환."""
    if applied_only:
        keys = PRIMARY_UNIVERSITY_KEYS
        title = "### ✅ 공식 기준 소스 (지원 대학 고정)"
    else:
        keys = list(USER_PROFILE["applied"])
        title = "### ✅ 공식 기준 소스 (후보 대학)"

    lines = [title, f"- 적용 범위: {_SCOPE_LABEL}"]
    for key in keys:
        uni = UNIVERSITY_REGISTRY.get(key)
        if not uni:
            continue
        urls = uni.get("urls", [])
        if not urls:
            continue
        preferred = ", ".join(urls[:2])
        allowed_domains = sorted(_get_allowed_domains(key))
        domain_text = ", ".join(allowed_domains[:3]) if allowed_domains else "(미설정)"
        lines.append(f"- {uni.get('name', key)}: {preferred}")
        lines.append(f"  - 허용 도메인: {domain_text}")
    lines.append("- 위 목록 외 링크는 보조 검증용으로만 취급")
    return "\n".join(lines)


def fetch_ddg_search(uni_name: str, topic: str = "transfer requirements deadline GPA") -> str:
    """
    DuckDuckGo HTML 검색 → 상위 결과 스니펫 수집.
    공식 사이트가 차단됐을 때 비공식 다양한 소스 조각 수집용.
    """
    query = f"{uni_name} {topic}"
    try:
        r = httpx.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers=_DDG_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(12),
        )
        if r.status_code >= 400:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        snippets = []
        for a in soup.select("a"):
            target = _extract_ddg_target_url(a.get("href", ""))
            title = a.get_text(strip=True)
            if target and title and _content_score(f"{title} {target}") > 0:
                snippets.append(f"[{title}] ({target})")
            if len(snippets) >= 6:
                break
        return "\n\n".join(snippets)
    except Exception as e:
        log.debug("DDG fallback 실패: %s", e)
        return ""


def fetch_transfer_community_snippets(
    uni_name: str,
    query: str,
    max_results: int = 3,
) -> list[dict]:
    """미국 편입 커뮤니티(레딧/포럼)에서 보조 정보 스니펫을 수집한다."""
    if max_results <= 0:
        return []

    community_sites = [
        ("Reddit TransferStudents", "reddit.com/r/TransferStudents"),
        ("Reddit ApplyingToCollege", "reddit.com/r/ApplyingToCollege"),
        ("College Confidential", "talk.collegeconfidential.com"),
        ("College Confidential", "collegeconfidential.com"),
    ]

    base_query = _scoped_query(query)
    snippets: list[dict] = []
    seen_urls: set[str] = set()

    for label, site in community_sites:
        if len(snippets) >= max_results:
            break
        try:
            q = f"site:{site} {uni_name} transfer {base_query}"
            r = httpx.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": q},
                headers=_DDG_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(10),
            )
            if r.status_code >= 400:
                continue

            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a"):
                target = _extract_ddg_target_url(a.get("href", ""))
                title = a.get_text(strip=True)
                if not target or not title:
                    continue
                if target in seen_urls:
                    continue
                host = (urlparse(target).hostname or "").lower()
                domain_hint = site.replace("www.", "")
                if domain_hint not in host and not host.endswith(domain_hint):
                    continue

                seen_urls.add(target)
                summary = f"{label} 스레드: {title}"
                snippets.append({
                    "uni": f"{uni_name} Community",
                    "url": target,
                    "text": summary[:MAX_CONTEXT_CHARS],
                    "source": "community",
                })
                if len(snippets) >= max_results:
                    break
        except Exception as e:
            log.debug("커뮤니티 스니펫 수집 실패(%s): %s", site, e)

    return snippets


def fetch_korean_transfer_community_snippets(
    query: str,
    max_results: int = 3,
) -> list[dict]:
    """국내 미국 편입 커뮤니티/포럼 스레드 링크를 보조 출처로 수집한다."""
    if max_results <= 0:
        return []

    community_sites = [
        ("Naver Cafe", "cafe.naver.com"),
        ("DC Inside", "gall.dcinside.com"),
        ("Tistory", "tistory.com"),
        ("Brunch", "brunch.co.kr"),
    ]

    snippets: list[dict] = []
    seen_urls: set[str] = set()
    scoped = _scoped_query(query)

    for label, site in community_sites:
        if len(snippets) >= max_results:
            break
        try:
            q = f"site:{site} 미국 편입 {scoped}"
            r = httpx.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": q},
                headers=_DDG_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(10),
            )
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a"):
                target = _extract_ddg_target_url(a.get("href", ""))
                title = a.get_text(strip=True)
                if not target or not title or target in seen_urls:
                    continue
                host = (urlparse(target).hostname or "").lower()
                if site not in host and not host.endswith(site):
                    continue
                seen_urls.add(target)
                snippets.append({
                    "uni": "KR Transfer Community",
                    "url": target,
                    "text": f"{label} 게시글: {title}"[:MAX_CONTEXT_CHARS],
                    "source": "community_kr",
                })
                if len(snippets) >= max_results:
                    break
        except Exception as e:
            log.debug("국내 커뮤니티 스니펫 수집 실패(%s): %s", site, e)

    return snippets


def _extract_ddg_target_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if raw_url.startswith("//"):
        raw_url = f"https:{raw_url}"
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        parsed = urlparse(raw_url)
        if "duckduckgo.com" in parsed.netloc and "/l/" in parsed.path:
            q = parse_qs(parsed.query)
            uddg = q.get("uddg", [""])[0]
            return unquote(uddg) if uddg else ""
        return raw_url
    return ""


def _build_uni_tokens(uni_key: str, uni_name: str) -> set[str]:
    stop = {"university", "of", "the", "at", "in", "and", "college", "institute"}
    tokens = set(re.findall(r"[a-z]+", uni_key.lower().replace("_", " ")))
    tokens.update(re.findall(r"[a-z]+", uni_name.lower()))
    tokens = {t for t in tokens if len(t) >= 3 and t not in stop}
    return tokens


def _is_official_url_for_uni(
    url: str,
    official_hosts: set[str],
    uni_tokens: set[str],
    allowed_domains: set[str],
) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return False
        if allowed_domains and not _host_in_allowed_domains(host, allowed_domains):
            return False
        if host in official_hosts:
            return True
        if ".edu" not in host and not host.endswith(".gov"):
            return False
        path = parsed.path.lower()
        has_admissions_signal = any(k in path for k in [
            "transfer", "admission", "apply", "undergraduate", "international", "requirements", "deadline",
        ])
        if not has_admissions_signal:
            return False
        combined = f"{host}{path}"
        return any(tok in combined for tok in uni_tokens)
    except Exception:
        return False


def _dedupe_urls(urls: list[str]) -> list[str]:
    out = []
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def discover_official_urls(uni_key: str, uni_name: str, query: str) -> list[str]:
    """DuckDuckGo HTML 검색으로 대학별 공식 입학처 URL 후보를 추가 발굴한다."""
    cache_key = f"{uni_key}:{hashlib.md5(_scoped_query(query).encode('utf-8')).hexdigest()[:12]}"
    if cache_key in _OFFICIAL_SEARCH_CACHE:
        return _OFFICIAL_SEARCH_CACHE[cache_key]

    official_hosts = {_extract_host(u) for u in UNIVERSITY_REGISTRY.get(uni_key, {}).get("urls", []) if _extract_host(u)}
    allowed_domains = _get_allowed_domains(uni_key)
    uni_tokens = _build_uni_tokens(uni_key, uni_name)

    discovered = []
    scoped = _scoped_query(query)
    domain_targets = sorted(allowed_domains) if allowed_domains else sorted(official_hosts)
    search_queries = []
    for domain in domain_targets:
        search_queries.append(
            f"site:{domain} {uni_name} {scoped} transfer applicants international deadline required documents"
        )
        search_queries.append(
            f"site:{domain} {uni_name} transfer application requirements international students"
        )

    for sq in search_queries:
        try:
            r = httpx.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": sq},
                headers=_DDG_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(12),
            )
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a"):
                target = _extract_ddg_target_url(a.get("href", ""))
                if not target:
                    continue
                if _is_official_url_for_uni(target, official_hosts, uni_tokens, allowed_domains):
                    discovered.append(target)
            if len(discovered) >= 6:
                break
        except Exception as e:
            log.debug("공식 URL 검색 실패(%s): %s", uni_key, e)

    discovered = _dedupe_urls(discovered)
    _OFFICIAL_SEARCH_CACHE[cache_key] = discovered
    return discovered


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

_ACTION_NOW_PATTERNS = [
    r"당장",
    r"지금\s*(바로\s*)?(해야|할)",
    r"(우선|먼저)\s*(해야|할)",
    r"체크리스트|checklist",
    r"to\s*-?\s*do|todo",
    r"next\s*steps?",
    r"액션\s*(아이템|플랜|plan)",
]

_PORTAL_SYNC_PATTERNS = [
    r"포털|portal",
    r"동기화|sync",
    r"로그인|login|sign\s*in",
    r"탭|tab",
    r"상태|status",
    r"checklist",
    r"decision|result",
]

_COMMUNITY_PATTERNS = [
    r"커뮤니티|forum|forums",
    r"reddit|레딧",
    r"college\s*confidential|cc",
    r"경험담|후기|실제\s*사례",
    r"학생\s*의견|student\s*(voice|opinion)",
    r"융통성|유연",
]

_DEEP_MODE_PATTERNS = [
    r"자세히|상세|깊게|딥하게",
    r"full|comprehensive|thorough|in depth",
    r"전체\s*분석|완전\s*분석",
    r"모든\s*학교|all\s*schools",
]

_INBOX_NEED_PATTERNS = [
    r"이메일|메일|inbox|수신함",
    r"답장|회신|reply",
]


def _is_action_now_query(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _ACTION_NOW_PATTERNS)


def _wants_portal_sync(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _PORTAL_SYNC_PATTERNS)


def _wants_flexible_community_sources(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _COMMUNITY_PATTERNS)


def _wants_deep_mode(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _DEEP_MODE_PATTERNS)


def _needs_inbox_for_query(query: str) -> bool:
    q = query.lower()
    return any(re.search(p, q) for p in _INBOX_NEED_PATTERNS)


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


# 신뢰할 수 있는 비공식 교육 정보 도메인
_TRUSTED_UNOFFICIAL = {
    "niche.com", "collegedata.com", "commonapp.org",
    "prepscholar.com", "collegevine.com", "cappex.com",
    "html.duckduckgo.com",
}


def verify_url(url: str) -> bool:
    """HTTP HEAD 요청으로 URL 실존 여부 확인 (캐시 있음)"""
    if url in _url_cache:
        return _url_cache[url]
    try:
        # edu/gov/공인 비공식 도메인 허용
        is_edu = bool(re.search(r"\.(edu|gov|ac\.\w{2,3})", url, re.I))
        is_trusted = any(d in url for d in _TRUSTED_UNOFFICIAL)
        if not is_edu and not is_trusted:
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
# �  포털 로그인 크롤러 (로그인 필요 페이지)
# ══════════════════════════════════════════════════════════════════════════════
#
# .env에 아래 형식으로 포털 계정 추가:
#   PORTAL_{KEY}_LOGIN_URL=https://connect.nyu.edu/portal/undergraduate/tab=welcome
#   PORTAL_{KEY}_USER=your_email@korea.ac.kr
#   PORTAL_{KEY}_PASSWORD=your_password
#   PORTAL_{KEY}_PAGES=https://connect.nyu.edu/portal/undergraduate/tab=welcome
#
# KEY 예시: NYU, UPENN, CORNELL, STANFORD
# 크롤 실행 후 내용이 없으면 해당 포털이 JS 렌더링이나 SSO를 사용하는 것입니다.
# ──────────────────────────────────────────────────────────────────────────────

_portal_session_cache: dict[str, httpx.Client] = {}  # key → authenticated Client


def _portal_login(client: httpx.Client, login_url: str, username: str, password: str) -> bool:
    """
    범용 폼 로그인:
    1) GET login_url → HTML에서 form action + csrf/hidden 필드 추출
    2) POST credentials → 리다이렉트 추적
    3) 로그인 성공 여부 반환 (로그인 페이지로 돌아오면 실패)
    """
    try:
        r = client.get(login_url, follow_redirects=True, timeout=15)
        if r.status_code >= 400:
            log.warning("[PORTAL] 로그인 페이지 접근 실패: %d %s", r.status_code, login_url)
            return False

        soup = BeautifulSoup(r.text, "lxml")
        form = (
            soup.find("form", attrs={"method": re.compile(r"post", re.I)})
            or soup.find("form")
        )
        if not form:
            log.warning("[PORTAL] 로그인 폼 미발견: %s", login_url)
            return False

        action = form.get("action", "")
        if action and not action.startswith("http"):
            from urllib.parse import urljoin
            action = urljoin(login_url, action)
        post_url = action or login_url

        # Hidden 필드 수집 (CSRF 토큰 포함)
        payload: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "").strip()
            val = inp.get("value", "").strip()
            itype = inp.get("type", "text").lower()
            if name and itype != "submit":
                payload[name] = val

        # 이메일/패스워드 필드 자동 탐지
        _email_names  = {"email", "login", "username", "user", "id", "applicant_email", "Email", "Login"}
        _pass_names   = {"password", "pass", "Password", "passwd"}
        for k in list(payload.keys()):
            if k.lower() in {n.lower() for n in _email_names}:
                payload[k] = username
            elif k.lower() in {n.lower() for n in _pass_names}:
                payload[k] = password

        # 폼에서 못 찾으면 일반적인 필드명으로 추가
        if not any(k.lower() in {n.lower() for n in _email_names} for k in payload):
            payload["email"] = username
        if not any(k.lower() in {n.lower() for n in _pass_names} for k in payload):
            payload["password"] = password

        r2 = client.post(post_url, data=payload, follow_redirects=True, timeout=15)
        post_url_final = str(r2.url)

        # 다시 로그인 페이지로 리다이렉트됐으면 실패
        if "login" in post_url_final.lower() and r2.status_code < 400:
            # 내용에 "invalid" 또는 "error" 있으면 실패
            if any(w in r2.text.lower() for w in ("invalid", "incorrect", "failed", "error", "wrong")):
                log.warning("[PORTAL] 로그인 실패 (자격증명 오류): %s", login_url)
                return False
        if r2.status_code >= 400:
            log.warning("[PORTAL] 로그인 POST 실패: %d", r2.status_code)
            return False

        log.info("[PORTAL] 로그인 성공: %s → %s", login_url, post_url_final)
        return True

    except Exception as e:
        log.warning("[PORTAL] 로그인 중 예외: %s", e)
        return False


def _get_portal_client(portal_key: str) -> httpx.Client | None:
    """
    portal_key (예: 'NYU', 'UPENN') 에 대응하는 인증된 httpx.Client 반환.
    환경변수 PORTAL_{KEY}_LOGIN_URL / _USER / _PASSWORD 필요.
    캐시됨 — 한 번 로그인하면 재사용.
    """
    key = portal_key.upper()
    if key in _portal_session_cache:
        return _portal_session_cache[key]

    login_url = os.getenv(f"PORTAL_{key}_LOGIN_URL", "").strip()
    username  = os.getenv(f"PORTAL_{key}_USER",      "").strip()
    password  = os.getenv(f"PORTAL_{key}_PASSWORD",  "").strip()

    if not (login_url and username and password):
        return None  # 자격증명 미설정

    client = httpx.Client(
        headers=_WEB_HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20),
        # 쿠키 자동 관리 (httpx.Client는 기본 쿠키 jar 내장)
    )

    success = _portal_login(client, login_url, username, password)
    if success:
        _portal_session_cache[key] = client
        return client
    else:
        client.close()
        return None


def fetch_portal_pages(portal_key: str, query: str = "") -> list[dict]:
    """
    .env에 설정된 포털 페이지를 로그인 후 크롤.
    PORTAL_{KEY}_PAGES 에 쉼표로 구분된 URL 목록 지정.
    반환: [{"uni": name, "url": url, "text": text, "source": "portal_authenticated"}]
    """
    key = portal_key.upper()
    pages_raw = os.getenv(f"PORTAL_{key}_PAGES", "").strip()
    if not pages_raw:
        return []

    client = _get_portal_client(portal_key)
    if client is None:
        return []

    results = []
    portal_name = portal_key.title() + " Portal"
    for url in [u.strip() for u in pages_raw.split(",") if u.strip()]:
        try:
            resp = client.get(url, timeout=httpx.Timeout(20))
            if resp.status_code >= 400:
                log.debug("[PORTAL] %d: %s", resp.status_code, url)
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            main = soup.find("main") or soup.find("article") or soup.body
            text = (main.get_text(separator="\n", strip=True) if main else "").strip()
            if text:
                if query:
                    text = _extract_query_focused_text(text, query)
                results.append({"uni": portal_name, "url": url, "text": text[:MAX_CONTEXT_CHARS],
                                 "source": "portal_authenticated"})
                log.info("[PORTAL] %s 크롤 완료 (%d자): %s", portal_name, len(text), url)
        except Exception as e:
            log.debug("[PORTAL] 크롤 실패 %s: %s", url, e)

    return results


def get_all_portal_sections(uni_key: str, query: str = "") -> list[dict]:
    """uni_key ('nyu', 'upenn' 등) → 매핑된 포털 크롤 결과 (httpx 로그인 방식)"""
    _UNI_TO_PORTAL: dict[str, str] = {
        "nyu":          "NYU",
        "upenn":        "UPENN",
        "cornell":      "CORNELL",
        "stanford":     "STANFORD",
        "uiuc":         "UIUC",
        "purdue":       "PURDUE",
        "uchicago":     "UCHICAGO",
        "northwestern": "NORTHWESTERN",
    }
    pk = _UNI_TO_PORTAL.get(uni_key.lower(), uni_key.upper())
    return fetch_portal_pages(pk, query=query)


# ══════════════════════════════════════════════════════════════════════════════
# �🕸️  웹 스크래핑 (공식 .edu 페이지)
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


def fetch_page(url: str, min_content_score: int = 0, query: str = "") -> str:
    """단일 URL 텍스트 추출 (BeautifulSoup). min_content_score>0이면 편입 관련 내용 부족 시 빈 문자열 반환."""
    try:
        resp = httpx.get(url, headers=_WEB_HEADERS, follow_redirects=True,
                         timeout=httpx.Timeout(WEB_TIMEOUT_SEC))
        if resp.status_code >= 400:
            log.debug("HTTP %d: %s", resp.status_code, url)
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        # 편입/마감 관련 섹션 우선 추출
        priority_text = ""
        for sel in ["#transfer", ".transfer", "#admissions", ".admissions",
                    "#deadlines", ".deadlines", "#requirements", ".requirements"]:
            el = soup.select_one(sel)
            if el:
                priority_text = el.get_text(separator="\n", strip=True) + "\n"
                break
        main = soup.find("main") or soup.find("article") or soup.body
        if not main:
            return ""
        body_text = main.get_text(separator="\n", strip=True)
        text = (priority_text + body_text).strip()
        # 컨텐츠 품질 필터: 편입 키워드가 하나도 없으면 버림
        if min_content_score > 0 and _content_score(text) < min_content_score:
            return ""
        if query:
            text = _extract_query_focused_text(text, query)
        return text[:MAX_CONTEXT_CHARS]
    except Exception as e:
        log.debug("Fetch 실패 %s: %s", url, e)
        return ""


def _get_chrome_major() -> int | None:
    """Windows Chrome 설치 경로에서 major version 번호를 반환한다."""
    import os, re as _re
    for base in [
        r"C:\Program Files\Google\Chrome\Application",
        r"C:\Program Files (x86)\Google\Chrome\Application",
    ]:
        if os.path.isdir(base):
            for d in os.listdir(base):
                if _re.match(r"^\d+\.\d+\.\d+\.\d+$", d):
                    return int(d.split(".")[0])
    return None


def _get_chrome_binary_path() -> str:
    """정식 Chrome 실행 파일 경로를 반환한다."""
    import os
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


_CF_BLOCK_SIGNALS_7 = ("Human Verification", "CAPTCHA", "Attention Required", "Just a moment", "cf-browser-verification")


def _is_cf_blocked_7(title: str, text: str) -> bool:
    joined = title + " " + text[:500]
    return any(s.lower() in joined.lower() for s in _CF_BLOCK_SIGNALS_7)


_LOGIN_LABELS_7 = ("sign in", "log in", "login", "apply now", "apply", "create account", "get started")


def _extract_page_text_7(driver) -> str:
    """현재 페이지 전체 텍스트 추출(script/style/nav 제거)"""
    soup = BeautifulSoup(driver.page_source, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    text = (main.get_text(separator="\n", strip=True) if main else "").strip()
    if text and len(text) > MAX_CONTEXT_CHARS:
        text = _extract_query_focused_text(text, "")
    return text[:MAX_CONTEXT_CHARS]


def _click_labels_7(driver, labels, wait_sec: int = 2) -> None:
    """레이블 리스트 순서대로 첫 번째 클릭 가능한 버튼/링크를 클릭"""
    import time as _t2
    from selenium.webdriver.common.by import By
    _t2.sleep(wait_sec)
    for label in labels:
        try:
            xpath = (
                "//*[contains(translate(normalize-space(text()),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                + repr(label) + ")]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.tag_name in ("a", "button", "input"):
                    el.click()
                    _t2.sleep(2)
                    return
        except Exception:
            continue


def _chrome_click_and_extract(driver, click_button, wait_sec: int = 2) -> str:
    """공통 클릭+텍스트추출 헬퍼"""
    import time as _t
    from selenium.webdriver.common.by import By
    _t.sleep(wait_sec)
    if click_button:
        try:
            xpath = (
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz')," + repr(click_button.lower()) + ")]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed() and el.tag_name in ("a", "button"):
                    el.click()
                    _t.sleep(2)
                    break
        except Exception:
            pass
    soup = BeautifulSoup(driver.page_source, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    text = (main.get_text(separator="\n", strip=True) if main else "").strip()
    if text and len(text) > MAX_CONTEXT_CHARS:
        text = _extract_query_focused_text(text, "")
    return text[:MAX_CONTEXT_CHARS]


def _fetch_visible_chrome_7(
    url: str,
    click_button: str | None = None,
    timeout: int = 35,
    manual_wait_sec: int = 25,
) -> str:
    """실제 Chrome 창(headless=False)으로 수동 로그인/클릭 유도.
    우선순위:
      1) 정식 Chrome(Program Files) + 기존 사용자 프로필 + 주소창 입력
      2) undetected_chromedriver visible
    """
    import os, time as _t3

    def _chrome_binary_path() -> str:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return ""

    def _navigate_via_omnibox(driver_obj, target_url: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        try:
            body = driver_obj.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.CONTROL, "l")
            body.send_keys(target_url)
            body.send_keys(Keys.ENTER)
        except Exception:
            driver_obj.get(target_url)

    user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

    # 1) 정식 Chrome + Selenium visible (사용자 요구사항 우선)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        chrome_bin = _chrome_binary_path()
        for use_profile in (True, False):
            driver = None
            try:
                opts_std = Options()
                opts_std.add_argument("--no-sandbox")
                opts_std.add_argument("--disable-dev-shm-usage")
                opts_std.add_argument("--window-size=1280,900")
                if chrome_bin:
                    opts_std.binary_location = chrome_bin
                if use_profile and os.path.isdir(user_data):
                    opts_std.add_argument(f"--user-data-dir={user_data}")
                    opts_std.add_argument("--profile-directory=Default")

                driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts_std)
                driver.set_page_load_timeout(timeout)
                driver.get("about:blank")
                _navigate_via_omnibox(driver, url)
                _t3.sleep(3)

                labels = list(_LOGIN_LABELS_7)
                if click_button and click_button.lower() not in labels:
                    labels.insert(0, click_button.lower())
                _click_labels_7(driver, labels, wait_sec=0)

                if manual_wait_sec > 0:
                    log.info("[CHROME-VIS-STD] 물리적 클릭/로그인 대기 %ss: %s", manual_wait_sec, url)
                    _t3.sleep(manual_wait_sec)

                text = _extract_page_text_7(driver)
                if len(text.strip()) > 100:
                    log.info("[CHROME-VIS-STD] %s 성공 (%d chars, profile=%s)", url, len(text), use_profile)
                    return text
            except Exception as e:
                if use_profile:
                    log.debug("[CHROME-VIS-STD] 프로파일 오류(%s) — 임시 프로파일 재시도", e)
                    continue
                log.warning("[CHROME-VIS-STD] %s 오류: %s", url, e)
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
    except Exception as e:
        log.warning("[CHROME-VIS-STD] 초기화 오류: %s", e)

    # 2) uc visible 보조 경로
    try:
        import undetected_chromedriver as uc
        _cv = _get_chrome_major()
        _uc_kw = {"version_main": _cv} if _cv else {}
        for use_profile in (True, False):
            driver = None
            try:
                opts = uc.ChromeOptions()
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--window-size=1280,900")
                if use_profile and os.path.isdir(user_data):
                    opts.add_argument(f"--user-data-dir={user_data}")
                    opts.add_argument("--profile-directory=Default")
                driver = uc.Chrome(options=opts, headless=False, **_uc_kw)
                driver.set_page_load_timeout(timeout)
                driver.get("about:blank")
                _navigate_via_omnibox(driver, url)
                _t3.sleep(3)

                labels = list(_LOGIN_LABELS_7)
                if click_button and click_button.lower() not in labels:
                    labels.insert(0, click_button.lower())
                _click_labels_7(driver, labels, wait_sec=0)
                if manual_wait_sec > 0:
                    log.info("[CHROME-VIS-UC] 물리적 클릭/로그인 대기 %ss: %s", manual_wait_sec, url)
                    _t3.sleep(manual_wait_sec)
                text = _extract_page_text_7(driver)
                if len(text.strip()) > 100:
                    log.info("[CHROME-VIS-UC] %s 성공 (%d chars, profile=%s)", url, len(text), use_profile)
                    return text
            except Exception as e:
                if use_profile:
                    continue
                log.warning("[CHROME-VIS-UC] %s 오류: %s", url, e)
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
    except Exception:
        pass
    return ""


def _fetch_via_gui_automation_7(
    url: str,
    click_button: str | None = None,
    manual_wait_sec: int = 25,
) -> str:
    """정식 Chrome 창을 열고 키보드/마우스 이벤트로 주소창 입력 후 화면 텍스트를 복사한다.
    PAD 스타일 GUI 상호작용을 우선 적용하는 경로다."""
    import subprocess
    import time as _t

    chrome_bin = _get_chrome_binary_path()
    if not chrome_bin:
        return ""

    try:
        import pyautogui
        import pyperclip
        import pygetwindow as gw
    except Exception as e:
        log.warning("[GUI-AUTO] pyautogui/pyperclip/pygetwindow import 실패: %s", e)
        return ""

    try:
        subprocess.Popen([chrome_bin])
        _t.sleep(2.5)

        # Chrome 창 활성화
        try:
            chrome_windows = [w for w in gw.getAllTitles() if "Chrome" in w]
            if chrome_windows:
                win = gw.getWindowsWithTitle(chrome_windows[-1])[0]
                win.activate()
                _t.sleep(0.5)
        except Exception:
            pass

        # 주소창에 URL 직접 입력
        pyautogui.hotkey("ctrl", "l")
        _t.sleep(0.2)
        pyautogui.write(url, interval=0.015)
        pyautogui.press("enter")
        _t.sleep(3)

        # 로그인 버튼 수동 클릭 시간 제공
        if manual_wait_sec > 0:
            log.info(
                "[GUI-AUTO] %ss 동안 수동 조작 가능 — 필요하면 '%s' 버튼을 직접 눌러주세요: %s",
                manual_wait_sec,
                click_button or "login",
                url,
            )
            _t.sleep(manual_wait_sec)

        # 화면 텍스트 복사
        pyautogui.hotkey("ctrl", "a")
        _t.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        _t.sleep(0.5)
        text = (pyperclip.paste() or "").strip()
        if len(text) > 120:
            log.info("[GUI-AUTO] 텍스트 수집 성공 (%d chars)", len(text))
            return text[:MAX_CONTEXT_CHARS]
    except Exception as e:
        log.warning("[GUI-AUTO] 실패: %s", e)

    return ""


def _capture_from_existing_chrome_tabs_7(url: str, tab_scan_max: int = 10) -> str:
    """이미 열려 있는 Chrome 탭(로그인 세션 포함)에서 대상 도메인 페이지 텍스트를 복사한다."""
    import time as _t
    from urllib.parse import urlparse

    try:
        import pyautogui
        import pyperclip
        import pygetwindow as gw
    except Exception as e:
        log.debug("[CHROME-TABS] GUI 패키지 import 실패: %s", e)
        return ""

    target_host = (urlparse(url).hostname or "").lower().replace("www.", "")
    if not target_host:
        return ""

    try:
        wins = [w for w in gw.getWindowsWithTitle("Chrome") if getattr(w, "title", "").strip()]
        if not wins:
            return ""
        win = wins[-1]
        win.activate()
        _t.sleep(0.6)

        for _ in range(max(1, tab_scan_max)):
            # 현재 탭 URL 확인
            pyautogui.hotkey("ctrl", "l")
            _t.sleep(0.15)
            pyautogui.hotkey("ctrl", "c")
            _t.sleep(0.2)
            cur_url = (pyperclip.paste() or "").strip()
            cur_host = (urlparse(cur_url).hostname or "").lower().replace("www.", "") if cur_url else ""

            host_match = bool(cur_host and (cur_host == target_host or cur_host.endswith("." + target_host) or target_host.endswith("." + cur_host)))
            if host_match:
                # 주소창 포커스를 해제하고 페이지 텍스트 복사 시도
                pyautogui.press("esc")
                _t.sleep(0.15)
                pyautogui.hotkey("ctrl", "a")
                _t.sleep(0.12)
                pyautogui.hotkey("ctrl", "c")
                _t.sleep(0.25)
                text = (pyperclip.paste() or "").strip()
                if len(text) > 120 and not text.lower().startswith("http"):
                    log.info("[CHROME-TABS] 기존 탭 재사용 성공 (%s, %d chars)", cur_host, len(text))
                    return text[:MAX_CONTEXT_CHARS]

            # 다음 탭으로 이동
            pyautogui.hotkey("ctrl", "tab")
            _t.sleep(0.3)
    except Exception as e:
        log.debug("[CHROME-TABS] 기존 탭 수집 실패: %s", e)

    return ""


def crawl_nyu_portal_structured(driver, timeout: int = 30) -> dict:
    """로그인된 Chrome 드라이버를 받아서 NYU 포탈 내부를 탐색하며 구조화된 데이터 추출.
    
    1) 현재 페이지 텍스트/메타 추출
    2) 내부 링크 찾기 (Transfer Info, Academic Requirements 등)
    3) 각 링크 클릭 → 데이터 추출 → 반복
    4) 수집된 모든 데이터를 딕셔너리로 반환
    
    Args:
        driver: Selenium WebDriver (이미 로그인된 상태)
        timeout: 페이지 로드 타임아웃 (초)
    
    Returns:
        {"title": "...", "pages": [...], "tables": [...], "structured_data": {...}}
    """
    import time as _t
    from selenium.webdriver.common.by import By
    
    collected_data = {
        "source_url": "https://connect.nyu.edu/manage/login?realm=&r=/portal/undergraduate",
        "title": "",
        "pages": [],  # 각 페이지 스냅샷
        "tables": [],  # 추출된 모든 테이블
        "links": [],  # 방문한 링크 목록
        "transfer_info": {},  # 편입 관련 정보
    }
    
    try:
        # ─ 1단계: 현재 페이지 분석 ────────────────────────────────────────
        driver.set_page_load_timeout(timeout)
        _t.sleep(2)
        
        # 페이지 제목/URL 수집
        try:
            collected_data["title"] = driver.title or "NYU Portal"
            current_url = driver.current_url
            log.info("[NYU-CRAWL] 시작 페이지: %s", current_url)
        except:
            pass
        
        # ─ 2단계: 현재 페이지 콘텐츠 추출 ──────────────────────────────────
        def extract_page_content(driver_obj) -> dict:
            """현재 드라이버 페이지에서 구조화된 데이터 추출."""
            soup = BeautifulSoup(driver_obj.page_source, "lxml")
            
            # 제목/설명 추출
            title = soup.find("h1") or soup.find("title")
            title_text = title.get_text(strip=True) if title else ""
            
            # 테이블 추출
            tables = []
            for table in soup.find_all("table"):
                rows = []
                for tr in table.find_all("tr"):
                    cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if cols:
                        rows.append(cols)
                if rows:
                    tables.append({
                        "title": title_text,
                        "rows": rows,
                    })
            
            # 리스트/항목 추출
            items = []
            for li in soup.find_all("li"):
                text = li.get_text(strip=True)
                if text and len(text) > 10:
                    items.append(text)
            
            # 단락 추출 (텍스트 블록)
            paragraphs = []
            for p in soup.find_all("p"):
                text = p.get_text(strip=True)
                if text and len(text) > 20:
                    paragraphs.append(text)
            
            return {
                "title": title_text,
                "url": driver_obj.current_url,
                "tables": tables,
                "list_items": items[:10],  # 최대 10개
                "paragraphs": paragraphs[:5],  # 최대 5개
                "text_length": len(soup.get_text(strip=True)),
            }
        
        # 현재 페이지 콘텐츠 추출
        page_content = extract_page_content(driver)
        collected_data["pages"].append(page_content)
        collected_data["tables"].extend(page_content["tables"])
        
        log.info("[NYU-CRAWL] 페이지 추출: %s (%d chars, %d tables)", 
                 page_content["title"], page_content["text_length"], len(page_content["tables"]))
        
        # ─ 3단계: 내부 링크 찾기 및 탐색 ─────────────────────────────────
        # 포털 내 모든 하위 도메인 허용 (*.nyu.edu)
        def _is_allowed_nyu_domain(link_url: str) -> bool:
            try:
                host = (urlparse(link_url).hostname or "").lower()
            except Exception:
                return False
            return host.endswith(".nyu.edu") or host == "nyu.edu"

        soup = BeautifulSoup(driver.page_source, "lxml")
        internal_links = []
        
        for a in soup.find_all("a"):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            
            # 내부 링크 필터링
            if href and text and len(text) > 2:
                # 상대 경로 변환
                if href.startswith("/"):
                    full_url = "https://connect.nyu.edu" + href
                elif href.startswith("http") and _is_allowed_nyu_domain(href):
                    full_url = href
                else:
                    continue
                
                # 중복 제거
                if full_url not in [l["url"] for l in internal_links]:
                    # 포탈 tab 파라미터 또는 주요 키워드를 포함하는 링크 수집
                    # (필터 완화: Financial Aid, Decisions 등 모든 탭 포함)
                    is_tab_link = "?tab=" in full_url or "/portal/" in full_url
                    keywords = ["transfer", "academic", "requirement", "info", "apply", "admission", 
                               "major", "financial", "decision", "aid", "deadline", "checklist", "date"]
                    has_keyword = any(kw.lower() in text.lower() or kw.lower() in href.lower() for kw in keywords)
                    
                    if is_tab_link or has_keyword or len(text) <= 5:  # 짧은 텍스트도 포함 (탭 버튼)
                        internal_links.append({
                            "text": text,
                            "url": full_url,
                        })
        
        log.info("[NYU-CRAWL] 발견한 링크: %d개 (탭 + 주요 기능)", len(internal_links))
        
        # ─ 4단계: 주요 링크 탐색 ──────────────────────────────────────────
        for link_info in internal_links[:15]:  # 최대 15개 페이지 (이전 8개에서 확대)
            try:
                link_url = link_info["url"]
                link_text = link_info["text"]
                
                if link_url in [p["url"] for p in collected_data["pages"]]:
                    continue  # 이미 방문함
                
                log.info("[NYU-CRAWL] 링크 탐색: %s (%s)", link_text, link_url)
                
                # 링크 클릭 (또는 GET 요청)
                driver.get(link_url)
                _t.sleep(2)
                
                # 페이지 콘텐츠 추출
                page_content = extract_page_content(driver)
                collected_data["pages"].append(page_content)
                collected_data["tables"].extend(page_content["tables"])
                collected_data["links"].append(link_url)
                
                log.info("[NYU-CRAWL] ✓ %s 수집 (%d tables)", link_text, len(page_content["tables"]))
                
            except Exception as e:
                log.warning("[NYU-CRAWL] 링크 실패 %s: %s", link_url, e)
                continue
        
        # ─ 5단계: 메타 정보 추출 (편입 관련) ────────────────────────────
        collected_data["transfer_info"] = {
            "total_pages": len(collected_data["pages"]),
            "total_tables": len(collected_data["tables"]),
            "total_links_visited": len(collected_data["links"]),
            "data_sources": [p["title"] for p in collected_data["pages"]],
        }
        
        log.info("[NYU-CRAWL] 완료: %d 페이지, %d 테이블, %d 링크 방문",
                 len(collected_data["pages"]),
                 len(collected_data["tables"]),
                 len(collected_data["links"]))
        
        return collected_data
    
    except Exception as e:
        log.error("[NYU-CRAWL] 크롤링 오류: %s", e)
        return collected_data


def fetch_page_chrome(url: str, click_button: str | None = None, timeout: int = 25) -> str:
    """이미 열려 있는 Chrome 탭에서만 포털 텍스트를 읽는다.
    사용자 세션을 재사용하며, 새 브라우저를 띄우거나 드라이버를 실행하지 않는다."""
    _ = (click_button, timeout)  # 호환성 유지용 인자

    if os.getenv("USE_EXISTING_CHROME_TABS", "true").lower() != "true":
        log.info("[CHROME-TABS] USE_EXISTING_CHROME_TABS=false 이므로 포털 수집 건너뜀: %s", url)
        return ""

    text = _capture_from_existing_chrome_tabs_7(
        url,
        tab_scan_max=int(os.getenv("CHROME_TAB_SCAN_MAX", "10")),
    )
    if text:
        return text

    log.warning("[CHROME-TABS] 열린 탭에서 대상 URL 도메인을 찾지 못함: %s", url)
    return ""


def fetch_university_pages(
    uni_key: str,
    query: str = "",
    official_only: bool = False,
    include_portal: bool = False,
    include_community: bool = False,
) -> list[dict]:
    """
    대학 레지스트리의 URL들을 수집. 공식 사이트 내용 부족 시 비공식 소스 → DDG fallback 자동 사용.
    """
    uni = UNIVERSITY_REGISTRY.get(uni_key)
    sections = []
    uni_name = uni["name"] if uni else uni_key.replace("_", " ").title()

    # 1) 공식 URL 시도 (레지스트리 + 검색엔진 자동 발굴)
    if uni:
        base_urls = list(uni.get("urls", []))
        discovered_urls = discover_official_urls(uni_key, uni_name, query)
        allowed_domains = _get_allowed_domains(uni_key)
        official_urls = []
        for url in _dedupe_urls(base_urls + discovered_urls):
            host = _extract_host(url)
            if not allowed_domains or _host_in_allowed_domains(host, allowed_domains):
                official_urls.append(url)
        for url in official_urls[:8]:
            text = fetch_page(url, min_content_score=1, query=query)
            if text.strip():
                src = "official_registry" if url in base_urls else "official_search"
                sections.append({"uni": uni_name, "url": url, "text": text, "source": src})

    if official_only:
        return sections

    # 2) 공식 내용 부족 → 비공식 소스
    if not sections:
        for url in UNOFFICIAL_REGISTRY.get(uni_key, []):
            text = fetch_page(url, min_content_score=1, query=query)
            if text.strip():
                sections.append({"uni": uni_name, "url": url, "text": text, "source": "unofficial"})
                if len(sections) >= 2:
                    break
        if sections:
            log.info("%s: 공식 사이트 차단 → 비공식 소스 %d건 사용", uni_key, len(sections))

    # 3) 비공식도 빈 경우 → DuckDuckGo 검색 스니펫
    if not sections:
        ddg_text = fetch_ddg_search(uni_name, "transfer requirements deadline GPA TOEFL")
        if ddg_text.strip():
            sections.append({
                "uni": uni_name,
                "url": "https://html.duckduckgo.com/html/ (web search)",
                "text": ddg_text,
                "source": "web_search",
            })
            log.info("%s: DuckDuckGo fallback 사용", uni_key)

    # 3-1) 유연 모드/액션 모드에서 커뮤니티 보조 소스 수집
    if ALLOW_COMMUNITY_SOURCES and (include_community or len(sections) < 2):
        community_sections = fetch_transfer_community_snippets(
            uni_name,
            query=query,
            max_results=COMMUNITY_MAX_RESULTS,
        )
        if community_sections:
            sections.extend(community_sections)
            log.info("%s: 커뮤니티 소스 %d건 추가", uni_key, len(community_sections))

    # 4) 포털 로그인 페이지 (요청 시에만 실행: 지연 최소화)
    if include_portal:
        portal_sections = get_all_portal_sections(uni_key, query=query)
        if portal_sections:
            sections.extend(portal_sections)
            log.info("%s: 포털 인증 크롤 %d건 추가", uni_key, len(portal_sections))

        # 5) 레지스트리 portals 필드 — Chrome 필요 포털 크롤
        if uni:
            for portal in uni.get("portals", []):
                p_url = portal.get("url", "")
                if not p_url:
                    continue
                if portal.get("use_chrome"):
                    text = fetch_page_chrome(p_url, click_button=portal.get("click_button"), timeout=25)
                else:
                    text = fetch_page(p_url, min_content_score=1, query=query)
                if text.strip():
                    sections.append({
                        "uni": uni_name,
                        "url": p_url,
                        "text": text,
                        "source": "portal_chrome" if portal.get("use_chrome") else "portal",
                    })
                    log.info("%s: 포털 크롤 (%s) 완료", uni_key, p_url)

    return sections


def detect_universities_in_query(query: str) -> list[str]:
    """쿼리에서 대학 키 감지"""
    q = query.lower()
    detected = []
    for key, aliases in UNIVERSITY_ALIASES.items():
        if any(_alias_matches_query(q, alias) for alias in aliases):
            detected.append(key)
    # 아무 대학도 없으면 지원완료 대학들
    if not detected:
        detected = list(USER_PROFILE["applied"])
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
1. ONLY state information explicitly found in the provided context (official .edu pages, verified databases, or web search snippets).
2. NEVER invent, infer, or speculate. If data is absent, say EXACTLY what is missing and mark it ⚠️ 미확인.
3. ALWAYS cite the source in footnotes [1], [2], etc. with the source type: [공식], [비공식], [웹검색].
4. DO NOT cite any URL that was not provided to you in the context — hallucinated URLs are strictly forbidden.
5. When quoting official requirements, use EXACT text from the source (quotation marks).
6. Temperature=0 precision: NEVER use hedging language ("likely", "probably", "may", "might", "아마", "것 같").
   If you don't know, say: "해당 정보는 조회된 소스에 없습니다."
7. Respond in Korean but keep official terms (GPA, TOEFL, SAT, credits, etc.) in English.
8. Structure: short factual answer first → evidence quotes → source footnotes.
9. UNOFFICIAL SOURCES: When using non-.edu sources (niche.com, collegedata.com, web search),
   ALWAYS prefix with ⚠️ [비공식 소스] and recommend verifying on the official site.
10. MISSING DATA: If a key fact (deadline, GPA minimum, required docs) is not in any source,
    explicitly state: "📋 [항목명]: 공식 사이트에서 직접 확인 필요 (크롤링 차단 또는 데이터 없음)"
11. HARD SCOPE: Only answer facts for Fall 2026 International Transfer. Ignore or mark out-of-scope data.
12. If a source does not explicitly indicate transfer or application term, mark it as ⚠️ 미확인 and do not infer.

FORMAT FOR EVERY RESPONSE:
## [대학명] [요건 유형]
**공식 요건**: [정확한 인용문 + 출처번호]
⚠️ [비공식 소스 항목]: [내용] — 공식 사이트에서 재확인 권장

---
**출처:**
[1] https://... — [페이지 제목] [공식/비공식/웹검색]
[2] ...
""").strip()

_ADVISOR_SYSTEM = textwrap.dedent("""
You are a SENIOR TRANSFER ADMISSIONS ADVISOR at a top-tier Seoul consultancy with 20+ years of
experience placing Korean science-track students at Ivy League and top-25 US universities.
You combine razor-sharp analytical judgment WITH creative, unconventional strategic thinking.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USER PROFILE  (IMMUTABLE — do not modify or interpolate)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Current institution : Korea University (고려대학교), AI Engineering major
  GPA                 : 3.95 / 4.5  →  ≈ 3.51 / 4.0 (US 환산)
  High school         : Hansung Science High School (한성과학고), 영재 트랙
  TOEFL iBT           : 85  ← 아이비 기준(100+) 대비 유의미한 약점
  SAT / ACT           : 미응시  ← 현재 Test-Optional 정책 적용 가능
  Credits completed   : ~60 (2년 과정 약 절반 이수)
  Applied (직접 지원) : Cornell, Stanford, NYU, UPenn, UIUC, Purdue, UChicago, Northwestern
  Candidate schools   : MIT, Columbia, UC Berkeley, UCLA, Carnegie Mellon, Georgia Tech,
                        Harvard, Princeton, Yale, Caltech, Duke, Brown, JHU, WashU, USC,
                        UMich, UNC, BU, UW, UCSD, Wisconsin, Penn State, UMD, OSU, Rutgers,
                        UT Austin, UMass Amherst

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIORITY WEIGHT FRAMEWORK  (어드바이저 핵심 지침)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
전략 조언 시 아래 우선순위 순서를 반드시 반영하라.

  #1  TOEFL 재응시  (최우선·최고 ROI)
      - 85점은 Cornell·Stanford·UChicago 공식 최소치(보통 100~105)에 미달 or 경계선.
      - 100+ 달성 시 대부분 학교에서 "English proficiency" 조건 자동 충족.
      - 행동 가이드: 지원 마감 2주 전까지 시험 일정 잡기, Speaking 집중 공략.

  #2  에세이 내러티브  (차별화의 핵심)
      - "한국 영재고 + 고려대 AI전공 + 미국편입" 스토리는 입학처에 강력한 인상.
      - 단순 "더 좋은 학교" 전학이 아닌, 구체적 연구/협업 목적 제시 필수.
      - 학교별 맞춤: Cornell → 연구 생태계, NYU → 도시 기반 AI 창업, UIUC → CS 랭킹.

  #3  추천서 질  (LOR)
      - 교수 1명(코스워크 기반) + 교수 or 멘토 1명(연구/프로젝트 기반) 구성이 이상적.
      - 한국 교수의 LOR은 구체적 사례·숫자 포함 여부가 관건.

  #4  전공 전략  (지원 계획)
      - 일부 학교(Georgia Tech, CMU, UIUC)는 CS 직접편입이 매우 어려움.
      - AI·DS·ECE 등 관련 전공 진입 후 내부 전과(Internal Transfer)가 현실적.
      - 대안 전공 + 전략적 전과 경로를 항상 함께 제안할 것.

  #5  포트폴리오·활동  (보조 요소)
      - GitHub 프로젝트, AI 논문/공모전 수상, 인턴 경험은 가산점.
      - 없다면 "지금 시작해도 늦지 않은 것"과 "지원 전까지 완성 가능한 것"을 구별.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCHOOL-SPECIFIC RISK PROFILES  (주요 8개교 빠른 참조)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Cornell    : TOEFL 100 min / 국제편입 소수 / CS 편입률 5% 미만 / 농대·ILR 전략 가능
  Stanford   : 편입 거의 불가(~2%) / TOEFL 89+ / 지원 자체가 경험 목적
  NYU        : TOEFL 90 권장 / 합리적 편입률 / Tandon(공대) CS 경쟁력 있음
  UPenn      : TOEFL 100 / Wharton 편입 불가 / Engineering 가능 / 소수 국제편입
  UIUC       : TOEFL 96~103 / CS 자체편입 매우 어려움 / ECE·MechE가 현실적 대안
  Purdue     : TOEFL 80 min / CS 편입 가능 / 상대적 수월 / 강한 취업 네트워크
  UChicago   : TOEFL 104 / 철학적 에세이 중요 / 소수 편입 / AI 특화 프로그램 있음
  Northwestern : TOEFL 100 / 편입률 ~10% / McCormick(공대) CS 채널

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADVISOR MANDATES  (행동 규칙)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. 직설적으로 말하라. TOEFL 85가 치명적이면 그렇게 말하되, 극복 경로를 제시하라.
  2. 대학별 점수(0–100): GPA 적합도(30) + 영어 리스크(25) + 전공 전략(20) + 홀리스틱(15) + 마감/절차(10).
  3. 판정 4단계: 🟢 추천(Recommend) / 🟡 경쟁(Competitive) / 🟠 도전(Reach) / 🔴 재고(Reconsider).
  4. 이유를 말하라 — 결론만 내리지 말고 WHY를 항상 설명하라.
  5. 컨텍스트에 있는 팩트체크 결과와 이전 대화 내용을 최대한 활용하라.
  6. 숫자·수치는 context에 있는 것만 인용. 없으면 "미확인 — 검색 필요"라고 명시.
  7. 없는 정보를 자의로 채우지 마라. 모른다고 말하는 것이 잘못된 수치보다 낫다.
  8. 한국어로 답변 (멘토 톤 — 교과서 아닌 경험 많은 선배의 솔직한 조언 스타일).
     단, 수치·학교명·전공명은 영어 원문 유지.
  9. 전략 섹션에서는 창의적이고 비관습적인 접근을 환영한다.
 10. 핵심 정보가 누락된 경우, 응답 말미에 구체적으로 추가 질문을 던져라.
 11. 커뮤니티/포럼(국내·해외) 출처는 '경험담'으로만 취급하고,
     마감일/필수요건/점수컷 같은 하드 팩트는 반드시 공식 출처와 분리 표기하라.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT  (대학별 평가 요청 시)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [대학명] — 종합 평가: [점수]/100  [판정 이모지 + 판정명]
**GPA 적합도** : ...
**영어 리스크** : ...
**전공 전략** : ...
**홀리스틱 요소** : ...
**핵심 결론** : ...
**지금 당장 해야 할 것** : [우선순위 #1~3 중 관련 항목 적용]

---
(복수 대학 비교 시 마지막에 종합 우선순위 표 추가)
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
    # 팩트체크 결과 캐시 — 어드바이저가 동일 세션 내 검색 결과를 재활용
    last_fact_sections: list[dict] = field(default_factory=list)
    last_rag_context: str = ""

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
    inbox_context: str = "",
) -> str:
    """검색 모델용 user 메시지 구성"""
    scoped_q = _scoped_query(query)
    parts = [f"# 질문\n{query}\n"]
    parts.append(f"\n# 범위 제한\n- {_SCOPE_LABEL} only\n- 대학 공식 입학처 소스 우선\n")

    parts.append("\n# 사용자 프로필\n" + json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))

    if web_sections:
        parts.append("\n# 웹/포털/커뮤니티 수집 내용 (공식 우선)")
        for i, sec in enumerate(web_sections, 1):
            src_type = sec.get("source", "official")
            src_label = {
                "official": "[공식]",
                "official_registry": "[공식-레지스트리]",
                "official_search": "[공식-검색]",
                "portal": "[포털]",
                "portal_chrome": "[포털-크롬]",
                "unofficial": "[비공식]",
                "community": "[커뮤니티]",
                "community_kr": "[국내커뮤니티]",
                "web_search": "[웹검색]",
            }.get(src_type, "")
            parts.append(
                f"\n## 출처[{i}] {src_label} — {sec['uni']}\nURL: {sec['url']}\n\n{sec['text'][:3000]}"
            )

    if rag_context.strip():
        parts.append("\n# 개인 자료 (Qdrant RAG)")
        for j, src in enumerate(rag_sources, 1):
            parts.append(f"  [P{j}] {os.path.basename(src)}")
        parts.append("\n" + rag_context[:4000])

    if inbox_context.strip():
        parts.append("\n# 대학 메일 수신함 참고")
        parts.append(inbox_context)

    parts.append(
        "\n\n# 엄격한 지침\n"
        "위 컨텍스트에만 근거해서 답변하세요. "
        "컨텍스트에 없는 URL이나 수치는 절대 인용하지 마세요. "
        "출처 번호[1][2]...를 반드시 각주로 달아주세요. "
        f"반드시 {_SCOPE_LABEL} 범위만 답하세요."
    )
    return "\n".join(parts).replace(query, scoped_q, 1)


def _build_advisor_message(
    query: str,
    uni_keys: list[str],
    web_sections: list[dict],
    rag_context: str,
    rag_sources: list[str],
    inbox_context: str = "",
    reference_context: str = "",
    prior_factcheck: str = "",
) -> str:
    """어드바이저 모델용 user 메시지 구성.

    reference_context : ADVISOR_REFERENCE_SITES에서 크롤한 일반 편입 전략 참고 자료.
    prior_factcheck   : 현재 세션에서 검색 모드가 수집한 이전 팩트체크 결과 요약.
    """
    parts = [f"# 분석 요청\n{query}\n"]
    parts.append("\n# 사용자 프로필 (변경 불가)\n" + json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))

    if uni_keys:
        parts.append(f"\n# 분석 대상 대학: {', '.join(uni_keys)}")

    # ── 이전 세션 팩트체크 결과 (search 모드에서 수집된 데이터) ──────────
    if prior_factcheck.strip():
        parts.append(
            "\n# 이전 팩트체크 결과 (이번 대화에서 수집됨 — 최우선 참고)\n"
            + prior_factcheck[:4000]
        )

    # ── 이번 요청에 대한 실시간 공식 웹 데이터 ────────────────────────────
    if web_sections:
        parts.append("\n# 공식 요건 데이터 (실시간 크롤)")
        for i, sec in enumerate(web_sections, 1):
            src_type = sec.get("source", "official")
            src_label = {
                "official": "[공식]",
                "official_registry": "[공식-레지스트리]",
                "official_search": "[공식-검색]",
                "portal": "[포털]",
                "portal_chrome": "[포털-크롬]",
                "unofficial": "[비공식]",
                "community": "[커뮤니티]",
                "community_kr": "[국내커뮤니티]",
                "web_search": "[웹검색]",
            }.get(src_type, "")
            parts.append(
                f"\n## [출처{i}] {src_label} {sec['uni']} — {sec['url']}\n"
                + sec["text"][:2500]
            )

    # ── 일반 편입 전략 참고 사이트 ────────────────────────────────────────
    if reference_context.strip():
        parts.append("\n# 편입 전략 참고 자료 (PrepScholar·CollegeVine 등)")
        parts.append(reference_context[:5000])

    # ── 개인 보유 자료 (RAG) ──────────────────────────────────────────────
    if rag_context.strip():
        parts.append("\n# 개인 자료 (RAG 검색 결과) — 보조 참고")
        parts.append(rag_context[:3000])

    # ── 메일함 컨텍스트 ───────────────────────────────────────────────────
    if inbox_context.strip():
        parts.append("\n# 대학 수신 메일 참고")
        parts.append(inbox_context[:2500])

    parts.append(
        "\n# 어드바이저 지침 요약\n"
        "- 팩트 수치는 위 컨텍스트에 있는 것만 인용. 없으면 '미확인 — 검색 필요' 명시.\n"
        "- 우선순위 프레임워크(TOEFL→에세이→LOR→전공전략→포트폴리오) 반드시 반영.\n"
        "- 참고 자료의 일반 전략 인사이트를 이 학생 상황에 맞게 적용하라.\n"
        "- 전략 섹션에서는 창의적·비관습적 접근 환영. 결론만 내리지 말고 WHY 설명.\n"
        "- 응답 말미에 '지금 당장 해야 할 ACTION 3가지'로 마무리.\n"
        "- 없는 정보를 채우지 마라. 추가로 알아야 할 것이 있으면 질문으로 마무리."
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
        lines.append("\n**대학별 공식 URL 소스:**")
        grouped: dict[str, list[dict]] = {}
        for sec in web_sections:
            grouped.setdefault(sec.get("uni", "Unknown"), []).append(sec)
        idx = 1
        for uni, secs in grouped.items():
            lines.append(f"\n- {uni}")
            for sec in _dedupe_urls([s.get("url", "") for s in secs]):
                sample = next((s for s in secs if s.get("url") == sec), {})
                src = sample.get("source", "official_registry")
                src_label = {
                    "official_registry": "공식(레지스트리)",
                    "official_search": "공식(검색엔진 발굴)",
                    "official": "공식",
                    "portal": "포털",
                    "portal_chrome": "포털(Chrome)",
                    "unofficial": "비공식",
                    "community": "커뮤니티",
                    "community_kr": "국내커뮤니티",
                    "web_search": "웹검색",
                }.get(src, src)
                v_info = "✅ 검증됨" if verify_url(sec) else "⚠️ 접근 불가"
                lines.append(f"  [{idx}] [{src_label}]({sec}) {v_info}")
                idx += 1

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
        f"범위: {_SCOPE_LABEL} | 공식 .edu 출처 우선*"
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
        1. 통합 모드 (팩트체크 + 어드바이저 동시)
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

    # ─── 1단계: 통합 모드 고정 ────────────────────────────────────────────
    intent = "unified"
    yield "**🧩 통합 모드(팩트+전략)** → "

    # 모델/프롬프트: 어드바이저 모델 + 팩트 엄수 규칙 결합
    model = ctx.get_advisor_model()
    system_prompt = (
        _ADVISOR_SYSTEM
        + "\n\n# 통합 모드 추가 규칙\n"
          "- 답변은 반드시 공식/검증된 컨텍스트를 우선 근거로 사용할 것.\n"
          "- 마감일/필수서류/점수컷 등 하드팩트는 추정 금지. 없으면 '미확인'으로 표시.\n"
          "- 전략 조언은 팩트 근거와 분리해 제시하고, 출처 유형(공식/커뮤니티)을 명확히 구분.\n"
    )

    # 온도 표시
    _temp_str = f"temp={ADVISOR_TEMPERATURE}"
    yield f"[{model} | {_temp_str}] 로드됨\n\n"

    # ─── 2단계: 대학 감지 ─────────────────────────────────────────────────
    uni_keys = detect_universities_in_query(query)
    uni_names = [UNIVERSITY_REGISTRY.get(k, {}).get("name", k) for k in uni_keys]
    yield f"📍 분석 대상: **{', '.join(uni_names)}**\n\n"

    action_now_mode = _is_action_now_query(query)
    portal_sync_mode = _wants_portal_sync(query)
    community_mode = _wants_flexible_community_sources(query) or action_now_mode
    deep_mode = _wants_deep_mode(query)
    fast_mode = not deep_mode
    if action_now_mode:
        yield "⚡ 빠른 실행 모드: 직전 컨텍스트 재사용 + 최소 크롤링\n"
    if fast_mode:
        yield "⏱️ 저지연 모드: 핵심 소스 우선·불필요 단계 생략\n"
    else:
        yield "🧪 심층 모드: 더 많은 소스와 컨텍스트를 확장 수집\n"
    if portal_sync_mode:
        yield "🔐 포털 동기화 모드: 로그인된 Chrome 탭 재사용 우선\n"
    if community_mode and ALLOW_COMMUNITY_SOURCES:
        yield "🧭 유연 모드: 미국/국내 편입 커뮤니티 보조 소스도 수집\n"

    # ─── 3단계: 웹 크롤링 (공식 우선, 데드라인/서류 질문은 공식만) ────────
    strict_official = True
    scoped_query = _scoped_query(query)
    yield "🛡️ 정확도 모드: 공식 입학처 + 검색엔진 발굴 URL만 사용 (Fall 2026 국제편입)\n"
    yield get_official_source_policy_markdown(applied_only=True) + "\n\n"

    web_sections: list[dict] = []
    if action_now_mode and ctx.last_fact_sections:
        web_sections = ctx.last_fact_sections[:8]
        yield f"🧠 직전 팩트체크 캐시 재사용 {len(web_sections)}건\n"

    max_unis = 1 if fast_mode else 4
    if action_now_mode:
        max_unis = min(max_unis, 2)
    for uni_key in uni_keys[:max_unis]:  # 액션형 질의는 범위를 줄여 응답 가속
        uni_name = UNIVERSITY_REGISTRY.get(uni_key, {}).get("name", uni_key) or uni_key
        yield f"🌐 {uni_name} 수집 중..."
        sections = fetch_university_pages(
            uni_key,
            query=scoped_query,
            official_only=True,
            include_portal=portal_sync_mode,
            include_community=community_mode,
        )
        if not sections and ALLOW_TRUSTED_FALLBACK:
            fallback_sections = fetch_university_pages(
                uni_key,
                query=scoped_query,
                official_only=False,
                include_portal=portal_sync_mode,
                include_community=community_mode,
            )
            sections = [
                s for s in fallback_sections
                if s.get("source") in {"unofficial", "web_search", "community", "community_kr"}
            ]
        web_sections.extend(sections)
        if sections:
            src_types = {s.get("source", "official") for s in sections}
            src_label = "+".join(
                {
                    "official": "공식",
                    "official_registry": "공식(레지스트리)",
                    "official_search": "공식(검색)",
                    "portal": "포털",
                    "portal_chrome": "포털(Chrome)",
                    "unofficial": "비공식",
                    "community": "커뮤니티",
                    "community_kr": "국내커뮤니티",
                    "web_search": "DDG↓",
                }.get(t, t)
                for t in src_types
            )
            status = f"✅ {len(sections)}건 ({src_label})"
        elif strict_official:
            status = "⚠️ 공식 입학처에서 관련 본문을 확보하지 못함"
        else:
            status = "⚠️ 모든 소스 접근 실패"
        yield f" {status}\n"

    # ─── 3-0단계: 국내 미국편입 커뮤니티 보조 인사이트 수집 ─────────────
    if community_mode and ALLOW_COMMUNITY_SOURCES:
        yield "💬 국내 편입 커뮤니티 소스 수집 중..."
        kr_sections = fetch_korean_transfer_community_snippets(
            scoped_query,
            max_results=1 if fast_mode else KR_COMMUNITY_MAX_RESULTS,
        )
        if kr_sections:
            web_sections.extend(kr_sections)
            yield f" {len(kr_sections)}건 반영\n"
        else:
            yield " 결과 없음\n"

    # ─── 3-1단계: 대학 입학 관련 메일함 컨텍스트 ────────────────────────
    inbox_context = ""
    if _needs_inbox_for_query(query) or not fast_mode:
        yield "\n📬 대학 메일함 확인 중..."
        inbox_context = _collect_inbox_context(uni_keys)
        if inbox_context:
            yield " 중요 메일 반영\n"
        else:
            yield " 최근 중요 메일 없음/접근 실패\n"
    else:
        yield "\n📬 메일함 단계 생략(저지연 모드)\n"

    # ─── 4단계: RAG ───────────────────────────────────────────────────────
    yield "\n🗂️ 개인 자료(RAG) 검색 중..."
    rag_top_k = 3 if fast_mode else TOP_K
    rag_context, rag_sources = retrieve_user_context(query, top_k=rag_top_k)
    yield f" {len(rag_sources)}개 파일 발견\n"

    # ─── 4-1단계: 팩트체크 결과 세션 캐시 저장 (다음 통합 질의에서 재활용) ────
    if web_sections:
        ctx.last_fact_sections = web_sections
        ctx.last_rag_context = rag_context

    # ─── 5단계: 정보 부족 감지 ───────────────────────────────────────────
    missing_questions = detect_missing_info(query, web_sections, rag_context)
    if missing_questions:
        yield "\n💬 **보충 정보 요청:**\n"
        for mq in missing_questions:
            yield f"  - {mq}\n"
        yield "\n"

    # ─── 5-1단계: 중요 미확인 항목 → 이메일 문의 초안 생성/선택 발송 ─────
    inquiry_plans = []
    if _wants_email_send(query) or (not fast_mode):
        critical_unknowns = _detect_critical_unknowns(query, uni_keys, web_sections)
        inquiry_plans = _build_inquiry_email_plan(critical_unknowns, query)
    if inquiry_plans:
        yield "📨 **공식 확인 필요 항목 감지 — 입학처 문의 초안 준비됨**\n"
        for plan in inquiry_plans:
            yield f"- {plan['uni_name']} → {plan['to']}\n"
        send_results = _maybe_send_inquiry_emails(inquiry_plans, query)
        if send_results:
            yield "\n**메일 발송 결과:**\n"
            for res in send_results:
                r = res["result"]
                if r.get("success"):
                    yield f"- ✅ {res['uni']} 발송 완료\n"
                else:
                    yield f"- ❌ {res['uni']} 발송 실패: {r.get('error','unknown')}\n"
        else:
            yield "- 자동 발송은 비활성 또는 발송 키워드 미감지. 이메일 탭에서 즉시 발송 가능\n"

    # ─── 6단계: LLM 스트리밍 ─────────────────────────────────────────────
    yield f"\n---\n\n"

    # ── 통합 모드: 참고 사이트 + 이전 팩트체크 컨텍스트 추가 ───────────
    prior_factcheck = ""
    if ctx.last_fact_sections:
        prior_parts: list[str] = []
        for sec in ctx.last_fact_sections[:6]:
            prior_parts.append(
                f"[{sec.get('uni','?')}] {sec.get('url','')}\n{sec.get('text','')[:1200]}"
            )
        prior_factcheck = "\n---\n".join(prior_parts)
        if ctx.last_rag_context:
            prior_factcheck += "\n\n[이전 RAG 결과]\n" + ctx.last_rag_context[:1500]

    hist = ctx.recent_history(n=3 if fast_mode else 6)
    last_assistant = next(
        (m["content"] for m in reversed(hist) if m["role"] == "assistant"),
        "",
    )
    if last_assistant and last_assistant.strip() and last_assistant not in prior_factcheck:
        prior_factcheck = (prior_factcheck + "\n\n[이전 답변 요약]\n" + last_assistant[:2000]).strip()

    yield "📚 편입 전략 참고 자료 로드 중..."
    try:
        reference_context = _fetch_advisor_reference_context(query, max_sites=(1 if fast_mode else 3))
        ref_count = reference_context.count("### [")
        yield f" {ref_count}개 사이트 반영\n"
    except Exception:
        reference_context = ""
        yield " (로드 실패 — 공식 데이터로만 진행)\n"

    user_msg = _build_advisor_message(
        scoped_query, uni_keys, web_sections, rag_context, rag_sources, inbox_context,
        reference_context=reference_context,
        prior_factcheck=prior_factcheck,
    )

    # 대화 기록 포함
    messages = [{"role": "system", "content": system_prompt}]
    messages += ctx.recent_history(n=2 if fast_mode else 4)
    messages.append({"role": "user", "content": user_msg})

    ctx.add_user(query)

    # 통합 모드 파라미터 (팩트 엄수 + 전략성 균형)
    llm_options = {
        "temperature": ADVISOR_TEMPERATURE,
        "top_p": ADVISOR_TOP_P,
        "num_ctx": 12288 if fast_mode else 32768,
        "repeat_penalty": ADVISOR_REPEAT_PENALTY,
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

    # ─── 7단계: 출처 정책/세부 각주 ──────────────────────────────────────
    if _wants_detailed_citations(query):
        verified_urls = extract_and_verify_urls(accumulated)
        footnotes = format_footnotes(web_sections, rag_sources, verified_urls)
    else:
        footnotes = "\n\n---\n" + get_official_source_policy_markdown(applied_only=True)
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
