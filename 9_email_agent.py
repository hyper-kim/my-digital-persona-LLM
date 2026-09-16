"""
9_email_agent.py — 편입 커뮤니케이션 에이전트
===============================================

기능:
  ✉️  AI 이메일 초안 작성 (입학처 문의 / 추천서 요청 / 서류 요청 등)
  📨  실제 이메일 발송 (Gmail SMTP — 항상 미리보기 후 수동 승인)
  📬  수신함 조회 (입학처 답장 확인)
  📋  발송 이력 SQLite 저장

보안/안전:
  · 자격증명 전부 환경변수 (.env)
  · 절대 자동 발송 없음 — 항상 --confirm 플래그 or UI 버튼 클릭 필요
  · 자격증명 로그 출력 없음
  · 시간당 최대 10통 발송 제한 (가족 실수 방지)

설정 (환경변수 / .env 파일):
  GMAIL_USER          = yourname@gmail.com
  GMAIL_APP_PASSWORD  = xxxx xxxx xxxx xxxx   # Google 앱 비밀번호
  EMAIL_SENDER_NAME   = 홍길동                 # 발신자 이름
  (Naver도 지원: NAVER_USER / NAVER_PASSWORD + useNaver=True)

Gmail 앱 비밀번호 발급:
  1. 구글 계정 → 보안 → 2단계 인증 활성화
  2. https://myaccount.google.com/apppasswords → "앱 비밀번호" 생성
  3. 위 환경변수에 저장

실행 CLI:
  python 9_email_agent.py --type inquiry --to admissions@cornell.edu --topic "TOEFL waiver"
  python 9_email_agent.py --type rec     --to teacher@school.kr --name "김선생님"
  python 9_email_agent.py --inbox        # 최근 수신 10통 확인
  python 9_email_agent.py --history      # 발송 이력 확인
"""

from __future__ import annotations

import os
import re
import sys
import json
import sqlite3
import smtplib
import imaplib
import email as email_lib
import textwrap
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
from typing import Iterator, Optional

import ollama

# .env 자동 로드 (VS Code 터미널 주입이 꺼져 있어도 동작)
try:
    from dotenv import load_dotenv
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_THIS_DIR, ".env"), override=False)
except Exception:
    # python-dotenv 미설치/로드 실패 시 OS 환경변수만 사용
    pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("email_agent")

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  설정
# ══════════════════════════════════════════════════════════════════════════════

LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434")
PROJECT_DIR      = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")

EMAIL_MODEL      = os.getenv("EMAIL_MODEL", "qwen3:14b")
EMAIL_TEMPERATURE = float(os.getenv("EMAIL_TEMPERATURE", "0.55"))
EMAIL_PROVIDER   = os.getenv("EMAIL_PROVIDER", "auto").lower()  # auto|gmail|naver|naver_works

EMAIL_DB_PATH    = os.path.join(PROJECT_DIR, "email_log.db")

# 발신자 정보 (환경변수)
GMAIL_USER       = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
SENDER_NAME      = os.getenv("EMAIL_SENDER_NAME", "")

# Naver 일반 메일
NAVER_USER     = os.getenv("NAVER_USER", "")
NAVER_PASSWORD = os.getenv("NAVER_PASSWORD", "")
USE_NAVER      = os.getenv("USE_NAVER", "false").lower() == "true"

# Naver Works (학교/기업 메일 — worksmobile.com)
NAVER_WORKS_USER     = os.getenv("NAVER_WORKS_USER", "")     # 예: kjy@korea.ac.kr
NAVER_WORKS_PASSWORD = os.getenv("NAVER_WORKS_PASSWORD", "") # 웍스 앱 비밀번호
USE_NAVER_WORKS      = os.getenv("USE_NAVER_WORKS", "false").lower() == "true"

# 안전 제한
MAX_SENDS_PER_HOUR = int(os.getenv("MAX_EMAIL_PER_HOUR", "10"))
SMTP_TIMEOUT_SEC = int(os.getenv("SMTP_TIMEOUT_SEC", "12"))
ALLOW_NON_EDU_IMPORTANT_SENDERS = os.getenv("ALLOW_NON_EDU_IMPORTANT_SENDERS", "false").lower() == "true"
EXTRA_ALLOWED_IMPORTANT_SENDER_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("EXTRA_ALLOWED_IMPORTANT_SENDER_DOMAINS", "").split(",")
    if d.strip()
}

# 사용자 프로필
try:
    import importlib
    _ua = importlib.import_module("7_unified_agent")
    USER_PROFILE = _ua.USER_PROFILE
except Exception:
    USER_PROFILE: dict = {
        "school": "Korea University (고려대학교)",
        "major": "Artificial Intelligence",
        "gpa": "3.95 / 4.5",
        "highschool": "Hansung Science High School (한성과학고)",
        "toefl": 85,
        "sat": "NOT TAKEN",
        "credits": "~60 credits",
        "applied": ["cornell", "stanford", "nyu", "upenn"],
    }

# ══════════════════════════════════════════════════════════════════════════════
# 📋  이메일 템플릿 라이브러리
# ══════════════════════════════════════════════════════════════════════════════

EMAIL_TYPES = {
    "inquiry": {
        "label": "입학처 문의",
        "subject_hint": "Transfer Application Inquiry — [TOPIC]",
        "description": "공식 웹사이트에 없는 정보 문의 (test waiver, interview, timeline 등)",
        "tips": [
            "간결하게 (3문단 이내)",
            "자신 소개 → 구체적 질문 → 감사 인사",
            "Application ID가 있으면 명시",
            "Re: 답장이 오면 동일 스레드 유지",
        ],
    },
    "rec_request": {
        "label": "추천서 요청 (영문)",
        "subject_hint": "Letter of Recommendation Request for US Transfer — [SCHOOL]",
        "description": "교수님/선생님께 영어로 추천서 요청",
        "tips": [
            "최소 6주 전에 요청",
            "제출 마감일(학교별) 명시",
            "지원하는 학교 목록 첨부",
            "본인의 수업에서 기억에 남는 에피소드 1~2개 언급",
            "Common App 초대 링크 발송 예정 명시",
            "추천인 부담 줄이기: 성적표·이력서·에세이 초안 첨부 제안",
        ],
    },
    "rec_korean": {
        "label": "추천서 요청 (한국어)",
        "subject_hint": "[학교명] 편입 추천서 요청 — [교수님 성함]",
        "description": "한국 교수님/선생님께 한국어로 추천서 요청",
        "tips": [
            "정중한 경어 사용 (합쇼체)",
            "제출 플랫폼(Common App/Coalition) 구체적으로 설명",
            "마감일 학교별로 나열",
            "본인 수업에서 기억에 남는 경험 서술",
            "교수님 부담 최소화: 영문 CV·성적표 첨부 예정 명시",
            "오프라인 면담 요청 선택지 제공",
        ],
    },
    "rec_followup": {
        "label": "추천서 제출 확인",
        "subject_hint": "Re: LoR Request — Submission Confirmation",
        "description": "추천서 제출 여부 정중하게 확인 (마감 1주 전)",
        "tips": [
            "1회만 follow-up",
            "정중하고 간결하게 (5문장 이내)",
            "마감일 다시 명시",
            "플랫폼 링크 재첨부",
            "영어/한국어 선택 가능",
        ],
    },
    "doc_request": {
        "label": "서류/성적표 요청",
        "subject_hint": "Official Document Request — [DOCUMENT TYPE]",
        "description": "학교 행정처, 교무처에 공식 서류 요청",
        "tips": [
            "요청 서류 종류 명확히 (영문 성적증명서, 재학증명서 등)",
            "필요 부수 명시",
            "제출처 주소/방식 설명",
            "발급 기간 확인",
        ],
    },
    "follow_up": {
        "label": "후속 확인",
        "subject_hint": "Re: [ORIGINAL SUBJECT] — Application Status Follow-up",
        "description": "이전 이메일 미회신 후속 문의 또는 서류 수령 확인",
        "tips": [
            "원본 이메일 발송일 언급",
            "1회만 follow-up (과도한 문의 금지)",
            "정중하고 간결하게",
        ],
    },
    "thank_you": {
        "label": "감사 인사",
        "subject_hint": "Thank You — [CONTEXT]",
        "description": "인터뷰 후, 추천서 작성 완료 후, 도움에 감사",
        "tips": [
            "24시간 이내 발송",
            "인터뷰/대화에서 기억에 남은 구체적 내용 언급",
            "짧게 (1~2문단)",
        ],
    },
    "custom": {
        "label": "직접 작성",
        "subject_hint": "[SUBJECT]",
        "description": "위 템플릿에 맞지 않는 자유 형식",
        "tips": ["프로페셔널한 톤 유지", "자기소개 → 본론 → 마무리 구조"],
    },
}

# 주요 대학 입학처 이메일 (참고용)
ADMISSIONS_EMAILS = {
    "cornell":   "transfer_admissions@cornell.edu",
    "stanford":  "admission@stanford.edu",
    "nyu":       "admissions@nyu.edu",
    "upenn":     "info@admissions.upenn.edu",
    "mit":       "admissions@mit.edu",
    "columbia":  "ugrad-ask@columbia.edu",
    "northwestern": "ug-admission@northwestern.edu",
    "uiuc":      "admissions@illinois.edu",
    "purdue":    "admissions@purdue.edu",
    "uchicago":  "collegeadmissions@uchicago.edu",
    "yale":      "student.questions@yale.edu",
    "princeton": "uaoffice@princeton.edu",
}

# 중요 메일 감지 키워드 (대학 입학 관련)
IMPORTANT_SUBJECT_KEYWORDS = [
    "admission", "admissions", "application", "transfer", "deadline", "due",
    "recommendation", "letter", "transcript", "document", "missing", "requirement",
    "interview", "decision", "status", "portal", "financial aid", "verification",
    "toefl", "ielts", "duolingo", "sat", "waiver", "supplement",
]

IMPORTANT_SENDER_KEYWORDS = [
    ".edu", "admissions", "undergrad", "ugrad", "application", "transfer",
    "stanford", "cornell", "upenn", "nyu", "columbia", "mit", "princeton", "yale",
]


def _extract_email_address(raw: str) -> str:
    """헤더 문자열에서 이메일 주소만 추출."""
    if not raw:
        return ""
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", raw, flags=re.I)
    return (m.group(0).strip().lower() if m else "")


def _email_domain(addr: str) -> str:
    if "@" not in (addr or ""):
        return ""
    return addr.rsplit("@", 1)[1].strip().lower()


def _is_allowed_important_sender(sender_raw: str) -> bool:
    """중요메일 요약에 허용할 발신자 도메인 검증(.edu 기본 강제)."""
    addr = _extract_email_address(sender_raw)
    domain = _email_domain(addr)
    if not domain:
        return False
    if domain.endswith(".edu"):
        return True
    if domain in EXTRA_ALLOWED_IMPORTANT_SENDER_DOMAINS:
        return True
    return ALLOW_NON_EDU_IMPORTANT_SENDERS


def _is_valid_inquiry_recipient(addr: str) -> bool:
    """입학처 문의(inquiry)는 기본적으로 .edu 수신자만 허용."""
    domain = _email_domain((addr or "").lower())
    return bool(domain and domain.endswith(".edu"))

# ══════════════════════════════════════════════════════════════════════════════
# 🗄️  SQLite 발송 이력
# ══════════════════════════════════════════════════════════════════════════════

def _init_db():
    conn = sqlite3.connect(EMAIL_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at     TEXT    NOT NULL,
            to_addr     TEXT    NOT NULL,
            subject     TEXT    NOT NULL,
            email_type  TEXT,
            body_preview TEXT,
            status      TEXT    DEFAULT 'sent'
        )
    """)
    conn.commit()
    return conn


def _log_sent(to_addr: str, subject: str, email_type: str, body: str):
    try:
        conn = _init_db()
        conn.execute(
            "INSERT INTO sent_emails (sent_at, to_addr, subject, email_type, body_preview) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), to_addr, subject, email_type, body[:500])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("이메일 로그 저장 실패: %s", e)


def _count_recent_sends(hours: int = 1) -> int:
    try:
        conn = _init_db()
        cutoff = datetime.fromtimestamp(time.time() - hours * 3600).isoformat()
        n = conn.execute(
            "SELECT COUNT(*) FROM sent_emails WHERE sent_at >= ?", (cutoff,)
        ).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def get_send_history(n: int = 20) -> list[dict]:
    try:
        conn = _init_db()
        rows = conn.execute(
            "SELECT sent_at, to_addr, subject, email_type, status FROM sent_emails ORDER BY id DESC LIMIT ?",
            (n,)
        ).fetchall()
        conn.close()
        return [{"sent_at": r[0], "to": r[1], "subject": r[2], "type": r[3], "status": r[4]} for r in rows]
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════════════════
# 🧠  시스템 프롬프트
# ══════════════════════════════════════════════════════════════════════════════

_EMAIL_SYSTEM = textwrap.dedent("""
You are an expert college admissions email consultant.
You draft professional English emails for a Korean transfer applicant.

APPLICANT PROFILE:
{profile}

RULES FOR EMAIL DRAFTING:
1. Subject line: clear, specific, professional
2. Salutation: "Dear [Name/Title]," or "Dear Admissions Team,"
3. Opening: state who you are + purpose in 1-2 sentences
4. Body: concise, specific questions or requests — NO rambling
5. Closing: "Thank you for your time and assistance."
6. Sign-off: "Best regards," then [NAME] / [SCHOOL] / [PHONE if provided]
7. Length: inquiries ≤ 200 words, rec requests 200–300 words
8. Tone: professional, respectful, not desperate or over-flattering
9. NEVER invent facts you don't have

AFTER THE DRAFT output:
📧 DRAFT:
---
[Subject: ...]

[Full email body]

---

💬 COACH:
[한국어 코칭 노트: 이 이메일을 쓴 이유, 주의할 점, 추가 정보 있으면 넣어야 할 사항]
""")

# ══════════════════════════════════════════════════════════════════════════════
# 🔧  SMTP / IMAP 연결
# ══════════════════════════════════════════════════════════════════════════════

def _get_smtp_config(provider: str = "auto") -> tuple[str, int, str, str, str, str]:
    """(host, port, user, password, sender_name, selected_provider) 반환"""
    p = (provider or EMAIL_PROVIDER or "auto").lower()

    def _works_ready() -> bool:
        return bool(NAVER_WORKS_USER and NAVER_WORKS_PASSWORD)

    def _naver_ready() -> bool:
        return bool(NAVER_USER and NAVER_PASSWORD)

    def _gmail_ready() -> bool:
        return bool(GMAIL_USER and GMAIL_APP_PASSWORD)

    # 명시 선택 모드: USE_* 무시하고 해당 계정 자격증명만 검사
    if p == "naver_works":
        if _works_ready():
            return ("smtp.worksmobile.com", 587, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, SENDER_NAME, "naver_works")
        raise ValueError("naver_works 선택됨: NAVER_WORKS_USER/NAVER_WORKS_PASSWORD 필요")

    if p == "naver":
        if _naver_ready():
            return ("smtp.naver.com", 587, NAVER_USER, NAVER_PASSWORD, SENDER_NAME, "naver")
        raise ValueError("naver 선택됨: NAVER_USER/NAVER_PASSWORD 필요")

    if p == "gmail":
        if _gmail_ready():
            return ("smtp.gmail.com", 587, GMAIL_USER, GMAIL_APP_PASSWORD, SENDER_NAME, "gmail")
        raise ValueError("gmail 선택됨: GMAIL_USER/GMAIL_APP_PASSWORD 필요")

    if p != "auto":
        raise ValueError("provider는 auto|gmail|naver|naver_works 중 하나여야 합니다")

    # auto 모드: 우선순위 = 명시 USE_* > 자격증명만 있는 항목
    if USE_NAVER_WORKS and _works_ready():
        return ("smtp.worksmobile.com", 587, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, SENDER_NAME, "naver_works")
    if USE_NAVER and _naver_ready():
        return ("smtp.naver.com", 587, NAVER_USER, NAVER_PASSWORD, SENDER_NAME, "naver")
    if _gmail_ready():
        return ("smtp.gmail.com", 587, GMAIL_USER, GMAIL_APP_PASSWORD, SENDER_NAME, "gmail")
    if _naver_ready():
        return ("smtp.naver.com", 587, NAVER_USER, NAVER_PASSWORD, SENDER_NAME, "naver")
    if _works_ready():
        return ("smtp.worksmobile.com", 587, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, SENDER_NAME, "naver_works")

    raise ValueError(
        "이메일 자격증명 없음. .env 파일 설정 필요:\n"
        "  [네이버 웍스] NAVER_WORKS_USER / NAVER_WORKS_PASSWORD\n"
        "  [네이버 일반] NAVER_USER / NAVER_PASSWORD\n"
        "  [Gmail]      GMAIL_USER / GMAIL_APP_PASSWORD"
    )


def _get_imap_config(provider: str = "auto") -> tuple[str, int, str, str, str]:
    """(host, port, user, password, selected_provider) 반환"""
    p = (provider or EMAIL_PROVIDER or "auto").lower()

    def _works_ready() -> bool:
        return bool(NAVER_WORKS_USER and NAVER_WORKS_PASSWORD)

    def _naver_ready() -> bool:
        return bool(NAVER_USER and NAVER_PASSWORD)

    def _gmail_ready() -> bool:
        return bool(GMAIL_USER and GMAIL_APP_PASSWORD)

    if p == "naver_works":
        if _works_ready():
            return ("imap.worksmobile.com", 993, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, "naver_works")
        raise ValueError("naver_works 선택됨: NAVER_WORKS_USER/NAVER_WORKS_PASSWORD 필요")
    if p == "naver":
        if _naver_ready():
            return ("imap.naver.com", 993, NAVER_USER, NAVER_PASSWORD, "naver")
        raise ValueError("naver 선택됨: NAVER_USER/NAVER_PASSWORD 필요")
    if p == "gmail":
        if _gmail_ready():
            return ("imap.gmail.com", 993, GMAIL_USER, GMAIL_APP_PASSWORD, "gmail")
        raise ValueError("gmail 선택됨: GMAIL_USER/GMAIL_APP_PASSWORD 필요")
    if p != "auto":
        raise ValueError("provider는 auto|gmail|naver|naver_works 중 하나여야 합니다")

    if USE_NAVER_WORKS and _works_ready():
        return ("imap.worksmobile.com", 993, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, "naver_works")
    if USE_NAVER and _naver_ready():
        return ("imap.naver.com", 993, NAVER_USER, NAVER_PASSWORD, "naver")
    if _gmail_ready():
        return ("imap.gmail.com", 993, GMAIL_USER, GMAIL_APP_PASSWORD, "gmail")
    if _naver_ready():
        return ("imap.naver.com", 993, NAVER_USER, NAVER_PASSWORD, "naver")
    if _works_ready():
        return ("imap.worksmobile.com", 993, NAVER_WORKS_USER, NAVER_WORKS_PASSWORD, "naver_works")
    raise ValueError("이메일 자격증명 없음")


def check_credentials() -> str:
    """자격증명 설정 여부 확인 (비밀번호 노출 없음)"""
    def _mask_account(addr: str) -> str:
        if "@" not in addr:
            return (addr[:3] + "***") if len(addr) > 3 else "***"
        local, domain = addr.split("@", 1)
        masked_local = (local[:3] + "***") if len(local) > 3 else "***"
        return f"{masked_local}@{domain}"

    lines = [f"기본 provider: {EMAIL_PROVIDER}"]

    if NAVER_WORKS_USER and NAVER_WORKS_PASSWORD:
        lines.append(f"✅ naver_works: {_mask_account(NAVER_WORKS_USER)}")
    elif NAVER_WORKS_USER:
        lines.append(f"⚠️  naver_works: {_mask_account(NAVER_WORKS_USER)} (비밀번호 없음)")
    else:
        lines.append("🔴 naver_works: 계정 없음")

    if NAVER_USER and NAVER_PASSWORD:
        lines.append(f"✅ naver: {_mask_account(NAVER_USER)}")
    elif NAVER_USER:
        lines.append(f"⚠️  naver: {_mask_account(NAVER_USER)} (비밀번호 없음)")
    else:
        lines.append("🔴 naver: 계정 없음")

    if GMAIL_USER and GMAIL_APP_PASSWORD:
        lines.append(f"✅ gmail: {_mask_account(GMAIL_USER)}")
    elif GMAIL_USER:
        lines.append(f"⚠️  gmail: {_mask_account(GMAIL_USER)} (앱 비밀번호 없음)")
    else:
        lines.append("🔴 gmail: 계정 없음")

    if SENDER_NAME:
        lines.append(f"✅ 발신자 이름: {SENDER_NAME}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# ✉️  이메일 초안 생성
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EmailRequest:
    email_type: str           # inquiry / rec_request / doc_request / follow_up / thank_you / custom
    to_address: str           # 수신 이메일
    topic: str                # 핵심 주제/목적
    recipient_name: str = ""  # 수신자 이름 ("교수님", "Prof. Chen", etc.)
    school_key: str = ""      # 대학 키 (cornell 등)
    extra_context: str = ""   # 추가 정보 (이전 이메일 내용, 특이사항 등)
    language: str = "english" # 이메일 언어 (기본 영어)


def build_email_user_message(req: EmailRequest) -> str:
    etype = EMAIL_TYPES.get(req.email_type, EMAIL_TYPES["custom"])
    parts = [
        f"# 이메일 종류\n{etype['label']}: {etype['description']}",
        f"\n# 수신자\n이메일: {req.to_address}",
    ]
    if req.recipient_name:
        parts.append(f"이름/호칭: {req.recipient_name}")
    if req.school_key:
        school_email = ADMISSIONS_EMAILS.get(req.school_key.lower(), "")
        parts.append(f"대학: {req.school_key.upper()}" + (f" ({school_email})" if school_email else ""))

    parts.append(f"\n# 핵심 목적\n{req.topic}")

    tips = etype.get("tips", [])
    if tips:
        parts.append("\n# 작성 팁\n" + "\n".join(f"  - {t}" for t in tips))

    if req.extra_context:
        parts.append(f"\n# 추가 컨텍스트\n{req.extra_context}")

    parts.append(
        f"\n# 지시\n"
        f"위 정보를 바탕으로 프로페셔널한 영문 이메일 초안을 작성하세요.\n"
        f"Subject line 포함. 발신자 이름은 '{SENDER_NAME or '[YOUR NAME]'}' 사용.\n"
        f"형식: 📧 DRAFT: 다음에 이메일 전문, 그 다음 💬 COACH: 한국어 조언."
    )
    return "\n".join(parts)


def build_rec_request_message(
    professor_name: str,
    professor_email: str,
    course_name: str,
    semester: str,
    episode: str,
    schools_deadlines: str,
    language: str = "korean",
    followup: bool = False,
) -> tuple[str, "EmailRequest"]:
    """
    구조화된 추천서 요청 이메일 메시지 + EmailRequest 빌더.

    Returns (user_message_str, EmailRequest)
    - language: 'korean' → rec_korean, 'english' → rec_request
    - followup: True → rec_followup 타입
    """
    if followup:
        etype = "rec_followup"
    elif language == "korean":
        etype = "rec_korean"
    else:
        etype = "rec_request"

    lang_label = "한국어" if language == "korean" else "영어"
    topic_parts = []
    if course_name:
        topic_parts.append(f"수강 과목: {course_name}" + (f" ({semester})" if semester else ""))
    if episode:
        topic_parts.append(f"기억에 남는 경험/에피소드: {episode}")
    if schools_deadlines:
        topic_parts.append(f"지원 대학 및 마감일:\n{schools_deadlines}")
    topic_parts.append(f"이메일 작성 언어: {lang_label}")
    if followup:
        topic_parts.append("※ 이미 요청 드린 추천서 제출 여부 정중하게 확인하는 follow-up 메일")

    topic = "\n".join(topic_parts)
    extra = (
        "Common App / Coalition App 초대 링크를 이메일로 발송할 예정임을 언급하세요.\n"
        "영문 CV, 성적표, 에세이 초안을 첨부로 제공할 수 있다고 명시하세요.\n"
        f"이메일은 반드시 {lang_label}로 작성하세요."
    )

    req = EmailRequest(
        email_type=etype,
        to_address=professor_email or "",
        topic=topic,
        recipient_name=professor_name or "교수님",
        extra_context=extra,
    )
    return build_email_user_message(req), req


def stream_compose(req: EmailRequest) -> Iterator[str]:
    """이메일 초안 스트리밍 — UI에서 직접 사용"""
    etype = EMAIL_TYPES.get(req.email_type, EMAIL_TYPES["custom"])
    yield f"**✉️ {etype['label']}** 초안 작성 → `{req.to_address}`\n"
    yield f"[{EMAIL_MODEL} | temp={EMAIL_TEMPERATURE}]\n\n---\n\n"

    system = _EMAIL_SYSTEM.format(profile=json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))
    user_msg = build_email_user_message(req)

    accumulated = ""
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        stream = client.chat(
            model=EMAIL_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": EMAIL_TEMPERATURE,
                "top_p": 0.9,
                "num_ctx": 8192,
            },
            keep_alive=0,
            stream=True,
        )
        for chunk in stream:
            token = chunk.get("message", {}).get("content", "")
            if token:
                accumulated += token
                yield token
    except Exception as e:
        yield f"\n\n❌ 에러: {e}"
        return

    yield "\n\n---\n⚠️ **이 초안은 저장만 됩니다. 실제 발송하려면 UI의 [발송] 버튼을 클릭하거나 `--confirm` 플래그를 사용하세요.**"


def extract_subject_and_body(draft_text: str) -> tuple[str, str]:
    """AI 출력에서 Subject와 Body 추출"""
    # 📧 DRAFT: 이후 섹션 추출
    draft_match = re.search(r"📧 DRAFT:[\s\-]*(.*?)(?:💬|$)", draft_text, re.DOTALL)
    content = draft_match.group(1).strip() if draft_match else draft_text

    # Subject: 추출
    subject_match = re.search(r"^Subject:\s*(.+)$", content, re.MULTILINE | re.IGNORECASE)
    if subject_match:
        subject = subject_match.group(1).strip()
        # Subject 줄 제거한 나머지가 body
        body = content[subject_match.end():].strip().lstrip("-\n ").strip()
    else:
        subject = "Transfer Application Inquiry"
        body = content.strip()

    return subject, body

# ══════════════════════════════════════════════════════════════════════════════
# 📨  실제 이메일 발송 (수동 승인 필수)
# ══════════════════════════════════════════════════════════════════════════════

def send_email(
    to_address: str,
    subject: str,
    body: str,
    email_type: str = "custom",
    cc: Optional[list[str]] = None,
    provider: str = "auto",
) -> dict:
    """
    이메일 발송.

    ⚠️ 이 함수는 항상 명시적 호출이 필요합니다.
    UI에서는 버튼 클릭, CLI에서는 --confirm 플래그로만 실행됩니다.
    """
    # 입력 검증
    if not to_address or "@" not in to_address:
        return {"success": False, "error": f"유효하지 않은 수신 주소: {to_address}"}

    # inquiry 안전장치: 기본적으로 .edu 수신자만 허용
    if (email_type or "").lower() == "inquiry":
        if not _is_valid_inquiry_recipient(to_address):
            return {
                "success": False,
                "error": f"입학처 문의 메일은 .edu 주소만 허용됩니다: {to_address}",
            }

    # 시간당 발송 제한
    recent = _count_recent_sends(hours=1)
    if recent >= MAX_SENDS_PER_HOUR:
        return {
            "success": False,
            "error": f"시간당 발송 제한 초과 ({MAX_SENDS_PER_HOUR}통). {60 - recent}분 후 재시도."
        }

    try:
        smtp_host, smtp_port, smtp_user, smtp_pass, sender_name, selected_provider = _get_smtp_config(provider)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    # 메시지 구성
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{sender_name} <{smtp_user}>" if sender_name else smtp_user
    msg["To"]      = to_address
    if cc:
        msg["Cc"] = ", ".join(cc)

    msg.attach(MIMEText(body, "plain", "utf-8"))

    # 발송
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SEC) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            all_recipients = [to_address] + (cc or [])
            server.sendmail(smtp_user, all_recipients, msg.as_string())

        _log_sent(to_address, subject, email_type, body)
        log.info("이메일 발송 완료: %s → %s", subject, to_address)
        return {
            "success": True,
            "to": to_address,
            "subject": subject,
            "provider": selected_provider,
            "sent_at": datetime.now().isoformat(),
        }
    except smtplib.SMTPAuthenticationError:
        return {
            "success": False,
            "error": "Gmail 인증 실패. 앱 비밀번호를 확인하세요.\n"
                     "발급: https://myaccount.google.com/apppasswords"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_email_batch(email_items: list[dict], provider: str = "auto") -> list[dict]:
    """다건 이메일을 SMTP 1회 연결로 발송해 지연을 줄인다.

    email_items 항목 예시:
      {
        "to_address": "admissions@nyu.edu",
        "subject": "...",
        "body": "...",
        "email_type": "inquiry",
        "cc": ["..."]
      }
    """
    if not email_items:
        return []

    try:
        smtp_host, smtp_port, smtp_user, smtp_pass, sender_name, selected_provider = _get_smtp_config(provider)
    except ValueError as e:
        return [{"success": False, "error": str(e)} for _ in email_items]

    recent = _count_recent_sends(hours=1)
    remaining = max(0, MAX_SENDS_PER_HOUR - recent)
    if remaining <= 0:
        return [{
            "success": False,
            "error": f"시간당 발송 제한 초과 ({MAX_SENDS_PER_HOUR}통).",
        } for _ in email_items]

    results: list[dict] = []
    sent_count = 0

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SEC) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)

            for item in email_items:
                if sent_count >= remaining:
                    results.append({
                        "success": False,
                        "error": f"시간당 발송 제한 초과 ({MAX_SENDS_PER_HOUR}통).",
                    })
                    continue

                to_address = (item.get("to_address") or "").strip()
                subject = (item.get("subject") or "").strip()
                body = item.get("body") or ""
                email_type = (item.get("email_type") or "custom").strip().lower()
                cc = item.get("cc") or []

                if not to_address or "@" not in to_address:
                    results.append({"success": False, "error": f"유효하지 않은 수신 주소: {to_address}"})
                    continue
                if email_type == "inquiry" and not _is_valid_inquiry_recipient(to_address):
                    results.append({
                        "success": False,
                        "error": f"입학처 문의 메일은 .edu 주소만 허용됩니다: {to_address}",
                    })
                    continue

                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = f"{sender_name} <{smtp_user}>" if sender_name else smtp_user
                msg["To"] = to_address
                if cc:
                    msg["Cc"] = ", ".join(cc)
                msg.attach(MIMEText(body, "plain", "utf-8"))

                try:
                    all_recipients = [to_address] + cc
                    server.sendmail(smtp_user, all_recipients, msg.as_string())
                    _log_sent(to_address, subject, email_type, body)
                    sent_count += 1
                    results.append({
                        "success": True,
                        "to": to_address,
                        "subject": subject,
                        "provider": selected_provider,
                        "sent_at": datetime.now().isoformat(),
                    })
                except Exception as e:
                    results.append({"success": False, "error": str(e)})
    except smtplib.SMTPAuthenticationError:
        return [{
            "success": False,
            "error": "SMTP 인증 실패. 계정/앱 비밀번호를 확인하세요.",
        } for _ in email_items]
    except Exception as e:
        return [{"success": False, "error": str(e)} for _ in email_items]

    return results

# ══════════════════════════════════════════════════════════════════════════════
# 📬  수신함 조회
# ══════════════════════════════════════════════════════════════════════════════

def read_inbox(n: int = 10, search_from: str = "", provider: str = "auto") -> list[dict]:
    """
    Gmail IMAP으로 최근 이메일 조회.
    search_from: 특정 발신자 주소로 필터 (예: "cornell.edu")
    """
    try:
        imap_host, imap_port, imap_user, imap_pass, selected_provider = _get_imap_config(provider)
    except ValueError as e:
        return [{"error": str(e)}]

    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")

        search_query = "ALL"
        if search_from:
            search_query = f'FROM "{search_from}"'

        _, msg_ids = mail.search(None, search_query)
        ids = msg_ids[0].split()
        recent_ids = ids[-n:] if len(ids) >= n else ids
        recent_ids = list(reversed(recent_ids))  # 최신순

        results = []
        for msg_id in recent_ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            # 헤더 디코딩
            def _decode(header_val: str) -> str:
                parts = decode_header(header_val or "")
                decoded = []
                for part, enc in parts:
                    if isinstance(part, bytes):
                        decoded.append(part.decode(enc or "utf-8", errors="replace"))
                    else:
                        decoded.append(str(part))
                return " ".join(decoded)

            subject = _decode(msg.get("Subject", "(제목 없음)"))
            sender  = _decode(msg.get("From",    "(발신자 없음)"))
            date    = msg.get("Date", "")

            # 본문 추출 (텍스트 파트만)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            body = part.get_payload(decode=True).decode(charset, errors="replace")
                        except Exception:
                            body = "(본문 디코딩 실패)"
                        break
            else:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    body = msg.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    body = "(본문 디코딩 실패)"

            results.append({
                "from": sender,
                "subject": subject,
                "date": date,
                "provider": selected_provider,
                "body_preview": body[:500].strip(),
            })

        mail.logout()
        return results

    except imaplib.IMAP4.error as e:
        return [{"error": f"IMAP 오류: {e}"}]
    except Exception as e:
        return [{"error": str(e)}]


def format_inbox(messages: list[dict]) -> str:
    """수신함 메시지를 읽기 쉽게 포맷"""
    if not messages:
        return "📭 받은 메시지 없음"
    if "error" in (messages[0] if messages else {}):
        return f"❌ {messages[0]['error']}"

    lines = [f"📬 최근 {len(messages)}통 이메일\n"]
    for i, m in enumerate(messages, 1):
        lines.append(f"{'─'*50}")
        lines.append(f"[{i}] 📅 {m.get('date','')}")
        lines.append(f"    📤 {m.get('from','')}")
        lines.append(f"    📌 {m.get('subject','')}")
        preview = m.get("body_preview", "")
        if preview:
            lines.append(f"    💬 {preview[:200]}...")
    return "\n".join(lines)


def _extract_deadline_hints(text: str) -> list[str]:
    """메일 본문/제목에서 날짜 힌트 추출"""
    if not text:
        return []
    patterns = [
        r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b",          # 2026-04-01
        r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b",          # 4/1/2026
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
    ]
    found = []
    for p in patterns:
        for m in re.findall(p, text, flags=re.I):
            if m not in found:
                found.append(m)
    return found[:3]


def _score_email_importance(m: dict) -> tuple[int, list[str]]:
    """중요도 점수와 근거 반환. CRITICAL 키워드는 별도 가산점."""
    score = 0
    reasons = []

    sender = (m.get("from") or "").lower()
    sender_addr = _extract_email_address(sender)
    sender_domain = _email_domain(sender_addr)
    subject = (m.get("subject") or "").lower()
    body = (m.get("body_preview") or "").lower()
    combined = f"{subject} {body}"

    # 🚨 CRITICAL 키워드 — 서류/마감 관련: 즉시 +12점
    CRITICAL_KEYWORDS = [
        "missing document", "missing materials", "incomplete application",
        "deadline", "due date", "by", "submit by", "required by",
        "action required", "respond by", "verify", "confirm",
        "official transcript", "financial aid", "scholarship",
        "admit", "admitted", "offer of admission", "waitlist", "waitlisted",
        "denied", "decision", "final decision",
    ]
    critical_hits = [kw for kw in CRITICAL_KEYWORDS if kw in combined]
    if critical_hits:
        score += 12
        reasons.append(f"🚨CRITICAL: {', '.join(critical_hits[:3])}")

    if sender_domain.endswith(".edu"):
        score += 5
        reasons.append(".edu 발신")

    if sender_domain.endswith(".edu"):
        for kw in IMPORTANT_SENDER_KEYWORDS:
            if kw in sender:
                score += 2
                reasons.append(f"발신자 키워드:{kw}")
                break

    hit_count = 0
    for kw in IMPORTANT_SUBJECT_KEYWORDS:
        if kw in combined:
            hit_count += 1
    if hit_count:
        score += min(8, hit_count * 2)
        reasons.append(f"핵심 키워드 {hit_count}개")

    if any(k in combined for k in ["missing", "required", "urgent", "deadline", "due"]):
        score += 4
        reasons.append("긴급/요구사항 포함")

    if any(k in combined for k in ["decision", "status update", "portal"]):
        score += 3
        reasons.append("결과/상태 업데이트")

    return score, reasons


def summarize_important_inbox(n: int = 40, provider: str = "gmail") -> str:
    """중요 대학 메일만 추려서 한국어로 요약"""
    messages = read_inbox(n=n, provider=provider)
    if not messages:
        return "📭 메일 없음"
    if "error" in messages[0]:
        return f"❌ {messages[0]['error']}"

    scored = []
    for m in messages:
        score, reasons = _score_email_importance(m)
        sender_l = (m.get("from") or "").lower()
        sender_addr = _extract_email_address(sender_l)
        sender_domain = _email_domain(sender_addr)
        sender_allowed = _is_allowed_important_sender(sender_l)
        subject_l = (m.get("subject") or "").lower()
        sender_related = sender_allowed
        subject_related = any(k in subject_l for k in [
            "admission", "application", "transfer", "university", "college", "portal", "deadline", "decision"
        ])

        # 컷오프: 발신자 도메인 검증 우선(.edu 기본 강제)
        if not sender_allowed:
            continue

        # 컷오프: CRITICAL 이메일은 점수 상관없이 포함, 일반은 6점 이상 + 대학 맥락
        is_critical_email = any("CRITICAL" in r or "🚨" in r for r in reasons)
        if is_critical_email or (score >= 6 and (sender_related or subject_related)):
            scored.append((score, reasons, m))

    if not scored:
        return (
            f"📭 최근 {len(messages)}통에서 중요 대학 메일을 찾지 못했습니다.\n"
            f"(provider={provider})"
        )

    scored.sort(
        key=lambda x: (
            # CRITICAL 이메일 최상단, 그 다음 점수 순
            0 if any("CRITICAL" in r or "🚨" in r for r in x[1]) else 1,
            -x[0],
        )
    )
    top = scored[:12]

    lines = [
        f"📌 중요 메일 요약 (최근 {len(messages)}통 스캔, 중요 {len(scored)}통, provider={provider})",
        "",
    ]

    action_items = []
    for idx, (score, reasons, m) in enumerate(top, 1):
        subj = m.get("subject", "(제목 없음)")
        sender = m.get("from", "(발신자 없음)")
        date = m.get("date", "")
        preview = (m.get("body_preview") or "").replace("\n", " ")
        deadlines = _extract_deadline_hints(f"{subj} {preview}")
        due_txt = f" | 📅 힌트: {', '.join(deadlines)}" if deadlines else ""

        # CRITICAL 이메일은 🚨로 강조
        is_critical = any("CRITICAL" in r or "🚨" in r for r in reasons)
        prefix = "🚨" if is_critical else "⭐"
        urgency = " ← 즉시 확인" if is_critical else ""

        lines.append(f"[{idx}] {prefix} 중요도 {score} | {subj}{urgency}")
        lines.append(f"    발신: {sender}")
        lines.append(f"    일시: {date}")
        lines.append(f"    근거: {', '.join(reasons)}{due_txt}")
        if preview:
            lines.append(f"    요약: {preview[:220]}...")
        lines.append("")

        if is_critical or any(k in (subj + " " + preview).lower() for k in ["missing", "required", "deadline", "due"]):
            action_items.append(f"- 🚨 {subj[:80]}: {'즉시' if is_critical else '요구사항/마감일'} 확인 필요")

    if action_items:
        lines.append("🧭 즉시 처리할 액션 (마감/서류/결과)")
        lines.extend(action_items[:8])

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# 🛠️  유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def format_history(records: list[dict]) -> str:
    if not records:
        return "📭 발송 이력 없음"
    lines = [f"📊 최근 발송 {len(records)}건\n"]
    for r in records:
        icon = "✅" if r["status"] == "sent" else "❌"
        lines.append(f"{icon} {r['sent_at'][:16]}  →  {r['to']}")
        lines.append(f"   제목: {r['subject']}")
        if r.get("type"):
            lines.append(f"   유형: {r['type']}")
    return "\n".join(lines)


def list_admissions_emails() -> str:
    lines = ["### 📧 주요 대학 입학처 이메일"]
    for school, addr in ADMISSIONS_EMAILS.items():
        lines.append(f"  {school.upper():10s} → {addr}")
    return "\n".join(lines)


def check_system() -> str:
    statuses = []
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        installed = {m.model for m in client.list().models}
        ok = any(EMAIL_MODEL.split(":")[0] in m for m in installed)
        statuses.append(f"{'🟢' if ok else '🔴'} 이메일 모델: {EMAIL_MODEL}  (temp={EMAIL_TEMPERATURE})")
    except Exception as e:
        statuses.append(f"🔴 Ollama: {e}")
    statuses.append(check_credentials())
    recent = _count_recent_sends(1)
    statuses.append(f"📊 오늘 시간당 발송: {recent}/{MAX_SENDS_PER_HOUR}통")
    statuses.append(f"📋 이메일 템플릿: {len(EMAIL_TYPES)}종")
    return "\n".join(statuses)

# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="편입 이메일 에이전트")
    parser.add_argument("--type",      "-t", type=str, default="inquiry",
                        choices=list(EMAIL_TYPES.keys()),
                        help="이메일 유형 (기본: inquiry)")
    parser.add_argument("--to",        "-r", type=str, help="수신자 이메일 주소")
    parser.add_argument("--name",      "-n", type=str, help="수신자 이름/호칭")
    parser.add_argument("--school",    "-s", type=str, help="대학 키 (cornell 등)")
    parser.add_argument("--topic",           type=str, help="이메일 주제/목적 (필수)")
    parser.add_argument("--context",         type=str, default="", help="추가 컨텍스트")
    parser.add_argument("--confirm",         action="store_true",
                        help="초안 확인 후 실제 발송 (이 플래그 없으면 초안만 출력)")
    parser.add_argument("--provider",        type=str, default="auto",
                        choices=["auto", "gmail", "naver", "naver_works"],
                        help="발송/수신에 사용할 계정 선택")
    parser.add_argument("--inbox",           action="store_true", help="수신함 최근 10통 조회")
    parser.add_argument("--important-summary", action="store_true", help="중요 대학 메일 요약")
    parser.add_argument("--from-filter",     type=str, default="", help="--inbox 필터 (발신자)")
    parser.add_argument("--history",         action="store_true", help="발송 이력 표시")
    parser.add_argument("--admissions",      action="store_true", help="대학별 입학처 이메일 목록")
    parser.add_argument("--status",          action="store_true", help="시스템 상태")
    args = parser.parse_args()

    if args.status:
        print(check_system())
        sys.exit(0)

    if args.history:
        print(format_history(get_send_history()))
        sys.exit(0)

    if args.admissions:
        print(list_admissions_emails())
        sys.exit(0)

    if args.inbox:
        print("📬 수신함 조회 중...")
        msgs = read_inbox(10, args.from_filter, provider=args.provider)
        print(format_inbox(msgs))
        sys.exit(0)

    if args.important_summary:
        print("📌 중요 메일 요약 생성 중...")
        print(summarize_important_inbox(n=40, provider=args.provider))
        sys.exit(0)

    if not args.topic:
        parser.print_help()
        print("\n예시:")
        print("  python 9_email_agent.py --type inquiry --to admissions@cornell.edu --topic 'TOEFL waiver for science HS'")
        print("  python 9_email_agent.py --type rec_request --to teacher@school.kr --name '김철수 선생님' --topic '추천서 요청 Cornell'")
        sys.exit(1)

    req = EmailRequest(
        email_type=args.type,
        to_address=args.to or ADMISSIONS_EMAILS.get(args.school or "", ""),
        topic=args.topic,
        recipient_name=args.name or "",
        school_key=args.school or "",
        extra_context=args.context,
    )

    print(f"\n{'='*64}")
    print(f"  ✉️  이메일 에이전트 — {EMAIL_TYPES[args.type]['label']}")
    print(f"  모델: {EMAIL_MODEL}  (temp={EMAIL_TEMPERATURE})")
    print(f"{'='*64}\n")

    # 초안 출력
    draft_full = ""
    for token in stream_compose(req):
        print(token, end="", flush=True)
        draft_full += token
    print("\n")

    # 실제 발송 (--confirm 있을 때만)
    if args.confirm and args.to:
        subject, body = extract_subject_and_body(draft_full)
        print(f"\n{'='*64}")
        print(f"  📤 발송 확인")
        print(f"  To:      {args.to}")
        print(f"  Subject: {subject}")
        print(f"{'='*64}")
        answer = input("\n정말 발송하시겠습니까? [yes/NO]: ").strip().lower()
        if answer == "yes":
            result = send_email(args.to, subject, body, args.type, provider=args.provider)
            if result["success"]:
                print(f"✅ 발송 완료: {result['sent_at']} (provider={result.get('provider', 'auto')})")
            else:
                print(f"❌ 발송 실패: {result['error']}")
        else:
            print("발송 취소됨.")
