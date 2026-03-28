"""
8_essay_agent.py — 편입 에세이 전담 에이전트
============================================

기능:
  · Common App Personal Statement (250-650단어)
  · 각 학교 Supplemental Essays (자동 프롬프트 로드)
  · 초안 작성 → 비평 → 재작성 이터레이션
  · 글자수 자동 검증
  · RAG로 지원자 컨텍스트 자동 주입
  · stream_essay() — UI 연동 스트리밍

모델 전략:
  ESSAY_MODEL  = qwen3:14b  (temp=0.82)  창의적 서술
  REVISE_MODEL = qwen3:14b  (temp=0.45)  비평+구조 개선

실행 CLI:
  python 8_essay_agent.py --school cornell --prompt "Why Cornell?"
  python 8_essay_agent.py --mode revise   (에세이를 stdin으로 받아 비평)
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import textwrap
import logging
from dataclasses import dataclass, field
from typing import Iterator, Optional

import ollama

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("essay_agent")

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  설정
# ══════════════════════════════════════════════════════════════════════════════

LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434")
PROJECT_DIR      = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH      = os.getenv("QDRANT_PATH", os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL       = os.getenv("QDRANT_URL", "")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-large")

# 에세이 모델 (창의적 서술 특화)
ESSAY_MODEL   = os.getenv("ESSAY_MODEL",  "qwen3:14b")
REVISE_MODEL  = os.getenv("REVISE_MODEL", "qwen3:14b")

ESSAY_TEMPERATURE   = float(os.getenv("ESSAY_TEMPERATURE",  "0.82"))
REVISE_TEMPERATURE  = float(os.getenv("REVISE_TEMPERATURE", "0.45"))
ESSAY_TOP_P         = float(os.getenv("ESSAY_TOP_P",        "0.95"))
ESSAY_REPEAT_PENALTY = float(os.getenv("ESSAY_REPEAT_PENALTY", "1.08"))

# 사용자 프로필 (7_unified_agent와 동기화)
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
# 📝  에세이 프롬프트 데이터베이스
# ══════════════════════════════════════════════════════════════════════════════

COMMON_APP_PROMPTS = {
    "background": {
        "id": "CA1",
        "prompt": (
            "Some students have a background, identity, interest, or talent that is so meaningful "
            "they believe their application would be incomplete without it. If this sounds like you, "
            "then please share your story."
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "challenge": {
        "id": "CA2",
        "prompt": (
            "The lessons we take from obstacles we encounter can be fundamental to later success. "
            "Recount a time when you faced a challenge, setback, or failure. How did it affect you, "
            "and what did you learn from the experience?"
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "belief": {
        "id": "CA3",
        "prompt": (
            "Reflect on a time when you questioned or challenged a belief or idea. What prompted your "
            "thinking? What was the outcome?"
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "gratitude": {
        "id": "CA4",
        "prompt": (
            "Reflect on something that someone has done for you that has made you happy or thankful "
            "in a surprising way. How has this gratitude affected or motivated you?"
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "accomplishment": {
        "id": "CA5",
        "prompt": (
            "Discuss an accomplishment, event, or realization that sparked a period of personal growth "
            "and a new understanding of yourself or others."
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "topic": {
        "id": "CA6",
        "prompt": (
            "Describe a topic, idea, or concept you find so engaging that it makes you lose all track "
            "of time. Why does it captivate you? What or who do you turn to when you want to learn more?"
        ),
        "word_limit": 650,
        "min_words": 450,
    },
    "free": {
        "id": "CA7",
        "prompt": "Share an essay on any topic of your choice — it can be one you've already written, one that responds to a different prompt, or one of your own design.",
        "word_limit": 650,
        "min_words": 250,
    },
}

SUPPLEMENTAL_PROMPTS: dict[str, list[dict]] = {
    "cornell": [
        {
            "id": "CORNELL_WHY",
            "prompt": "Why Cornell? How do your interests fit with the academic programs, culture, and community at Cornell? (Please respond in 650 words or fewer.)",
            "word_limit": 650,
            "min_words": 300,
            "tips": "Mention specific college (CAS/CoE/Dyson), faculty research, programs (MEng, CoRE, etc.)",
        },
        {
            "id": "CORNELL_ENGAGEMENT",
            "prompt": "Cornell's transfer applicants are asked to describe their academic interests and why Cornell is the right fit for their academic goals. (150–300 words)",
            "word_limit": 300,
            "min_words": 150,
            "tips": "Concrete academic goals, specific courses or labs at Cornell",
        },
    ],
    "stanford": [
        {
            "id": "STANFORD_INTELLECTUAL",
            "prompt": "Stanford students possess an intellectual vitality. Reflect on an idea or experience that has been important to your intellectual development. (100–250 words)",
            "word_limit": 250,
            "min_words": 100,
            "tips": "Show genuine curiosity, not just academic achievement",
        },
        {
            "id": "STANFORD_WHY",
            "prompt": "Virtually all of Stanford's undergraduates live on campus. Write a note to your future roommate that reveals something about you or that will help your roommate — and us — know you better. (100–250 words)",
            "word_limit": 250,
            "min_words": 100,
            "tips": "Personal, quirky, honest — show who you really are",
        },
        {
            "id": "STANFORD_WORLD",
            "prompt": "Tell us about something that is meaningful to you and why. (100–250 words)",
            "word_limit": 250,
            "min_words": 100,
            "tips": "Any topic — hobby, cause, person, artifact. Show depth not breadth.",
        },
    ],
    "nyu": [
        {
            "id": "NYU_WHY",
            "prompt": "Why NYU? We'd like to know why you're interested in NYU and the specific college, school, or program. (400 words)",
            "word_limit": 400,
            "min_words": 250,
            "tips": "Name specific NYU programs, CAS/Stern/Courant, research centers. NYC as extension of campus.",
        },
    ],
    "upenn": [
        {
            "id": "UPENN_WHY",
            "prompt": "How will you explore your intellectual and academic interests at the University of Pennsylvania? Please answer this question given the specific undergraduate school or program to which you are applying. (150–200 words)",
            "word_limit": 200,
            "min_words": 150,
            "tips": "Mention Penn's college within the school, interdisciplinary programs like Vagelos, Huntsman, VIPER",
        },
        {
            "id": "UPENN_COMMUNITY",
            "prompt": "At Penn, learning and growth happen outside of the classroom, too. How will you explore the community at Penn? Consider how this community will help you achieve your goals and how you will contribute to it. (150–200 words)",
            "word_limit": 200,
            "min_words": 150,
            "tips": "Clubs, research groups, Penn clubs specific to your interests",
        },
    ],
}

# ══════════════════════════════════════════════════════════════════════════════
# 🧠  시스템 프롬프트
# ══════════════════════════════════════════════════════════════════════════════

_ESSAY_SYSTEM = textwrap.dedent("""
You are an elite college admissions essay coach with 20+ years of experience at top-10 US universities.
You specialize in transfer applicants and international students.

CORE PRINCIPLES:
1. Every essay must SHOW, not tell — concrete scenes, sensory details, specific moments
2. Voice must sound authentically like the applicant, not generic
3. Answer the exact prompt; don't drift into generic "I want to contribute" clichés
4. For "Why [School]" essays: cite SPECIFIC programs, faculty (by name if possible), courses, research labs
5. Structure: hook → story/evidence → reflection → forward-looking connection to this school
6. Avoid: "ever since I was young", "I have always been passionate", "this school is renowned", clichés

APPLICANT PROFILE (use this to personalize every essay):
{profile}

RESPONSE LANGUAGE:
- Essays: write in English (admissions requirement)
- Coaching notes, critiques, explanations: Korean (한국어)
- Mark with 📝 ESSAY: ... / 💬 COACH: ...
""")

_REVISE_SYSTEM = textwrap.dedent("""
You are a rigorous college essay editor. Your job is to:
1. Identify SPECIFIC weaknesses (weak verbs, clichés, vague claims, word waste)
2. Rewrite the weakest paragraph as an example of improvement
3. Give 3-5 concrete action items the applicant should fix
4. Re-score the essay on: Hook (1-10), Specificity (1-10), Voice (1-10), Prompt Fit (1-10), Overall (1-10)

Applicant profile for context:
{profile}

Response in Korean (한국어). Essays in English.
""")

# ══════════════════════════════════════════════════════════════════════════════
# 🔍  RAG (personal context injection)
# ══════════════════════════════════════════════════════════════════════════════

def _get_rag_context(query: str) -> tuple[str, list[str]]:
    """Qdrant에서 에세이 관련 개인 자료 검색"""
    try:
        if not os.path.isdir(QDRANT_PATH):
            return "", []
        from qdrant_client import QdrantClient
        from sentence_transformers import SentenceTransformer

        q_client = QdrantClient(url=QDRANT_URL) if QDRANT_URL else QdrantClient(path=QDRANT_PATH)
        embed_model = SentenceTransformer(EMBED_MODEL_NAME)
        vec = embed_model.encode(query, normalize_embeddings=True).tolist()
        results = q_client.search(
            collection_name="omni_persona_v3",
            query_vector=vec,
            limit=5,
            with_payload=True,
        )
        chunks, sources = [], []
        for r in results:
            pl = r.payload or {}
            text = pl.get("text") or pl.get("_node_content", "")
            if text and "PENDING_OCR" not in text:
                chunks.append(text[:1500])
                src = pl.get("file_path") or pl.get("source", "")
                if src and src not in sources:
                    sources.append(src)
        return "\n\n---\n".join(chunks), sources
    except Exception as e:
        log.warning("RAG 실패: %s", e)
        return "", []

# ══════════════════════════════════════════════════════════════════════════════
# 📊  유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def count_words(text: str) -> int:
    # 에세이 섹션만 카운트 (코치 노트 제외)
    essay_text = re.sub(r"💬 COACH:.*?(?=📝 ESSAY:|$)", "", text, flags=re.DOTALL)
    # 마크다운 제거
    clean = re.sub(r"[#*_`\[\]()]", "", essay_text)
    return len(clean.split())


def get_prompt_info(school: Optional[str], prompt_key: Optional[str]) -> Optional[dict]:
    """학교+키워드로 프롬프트 정보 반환"""
    if school and school.lower() in SUPPLEMENTAL_PROMPTS:
        school_prompts = SUPPLEMENTAL_PROMPTS[school.lower()]
        if prompt_key:
            pk = prompt_key.lower()
            for p in school_prompts:
                if pk in p["id"].lower() or pk in p["prompt"].lower():
                    return p
        return school_prompts[0]  # 첫 번째 프롬프트 기본
    if prompt_key and prompt_key.lower() in COMMON_APP_PROMPTS:
        return COMMON_APP_PROMPTS[prompt_key.lower()]
    return None


def list_prompts(school: Optional[str] = None) -> str:
    """사용 가능한 프롬프트 목록 출력"""
    lines = ["### 📋 에세이 프롬프트 목록\n"]

    lines.append("#### Common App Personal Statement (650단어)")
    for key, info in COMMON_APP_PROMPTS.items():
        lines.append(f"  `--prompt {key}` — [{info['id']}] {info['prompt'][:80]}...")

    if school:
        school_key = school.lower()
        if school_key in SUPPLEMENTAL_PROMPTS:
            lines.append(f"\n#### {school.upper()} Supplemental")
            for p in SUPPLEMENTAL_PROMPTS[school_key]:
                lines.append(f"  [{p['id']}] {p['prompt'][:100]}... (최대 {p['word_limit']}단어)")
    else:
        lines.append("\n#### Supplemental (--school 지정 시 상세 보기)")
        for school_key in SUPPLEMENTAL_PROMPTS:
            n = len(SUPPLEMENTAL_PROMPTS[school_key])
            lines.append(f"  `--school {school_key}` — {n}개 프롬프트")

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# ✍️  에세이 생성 파이프라인
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EssayRequest:
    """에세이 작성 요청"""
    user_instruction: str          # 사용자가 쓴 요청 (어떤 이야기를 담을지 등)
    school: Optional[str] = None   # "cornell" | "stanford" | None (Common App)
    prompt_key: Optional[str] = None   # "why" | "background" | etc.
    custom_prompt: Optional[str] = None  # 직접 입력한 프롬프트
    word_limit: int = 650
    mode: str = "draft"            # "draft" | "revise" | "critique"
    existing_essay: Optional[str] = None  # revise 모드 시 기존 에세이


def build_essay_user_message(req: EssayRequest, rag_context: str) -> str:
    """LLM에 전달할 user 메시지 구성"""
    parts = []

    # 프롬프트 정보
    prompt_info = get_prompt_info(req.school, req.prompt_key)
    if req.custom_prompt:
        essay_prompt = req.custom_prompt
        word_limit = req.word_limit
        tips = ""
    elif prompt_info:
        essay_prompt = prompt_info["prompt"]
        word_limit = prompt_info["word_limit"]
        tips = prompt_info.get("tips", "")
    else:
        essay_prompt = req.user_instruction
        word_limit = req.word_limit
        tips = ""

    parts.append(f"# 에세이 프롬프트\n{essay_prompt}")
    parts.append(f"\n# 단어 수 요건\n최소 {prompt_info.get('min_words', 200) if prompt_info else 200}단어 / 최대 {word_limit}단어")

    if tips:
        parts.append(f"\n# 이 프롬프트 팁\n{tips}")

    parts.append(f"\n# 지원자가 전달하고 싶은 내용\n{req.user_instruction}")

    if rag_context:
        parts.append(f"\n# 지원자 개인 자료 (RAG, 배경 파악용)\n{rag_context[:3000]}")

    if req.mode == "revise" and req.existing_essay:
        parts.append(f"\n# 기존 에세이 (개선 대상)\n{req.existing_essay}")
        parts.append(
            "\n# 지시\n"
            "위 에세이를 철저히 비평하고, 개선된 버전을 완성해주세요.\n"
            "📝 ESSAY: [최종 개선본]\n"
            "💬 COACH: [구체적 변경사항 설명 및 추가 조언]"
        )
    elif req.mode == "critique":
        parts.append(
            "\n# 지시\n"
            "에세이를 비평해주세요 (아직 에세이 작성하지 말고, 구체적 약점과 개선 방향만).\n"
            "💬 COACH: [상세 비평]"
        )
    else:  # draft
        parts.append(
            "\n# 지시\n"
            "위 프롬프트에 맞는 에세이를 처음부터 작성해주세요.\n"
            "📝 ESSAY:\n[에세이 본문 here]\n\n"
            "💬 COACH:\n[지원자를 위한 코칭 노트 — 한국어]"
        )

    return "\n".join(parts)


def stream_essay(req: EssayRequest) -> Iterator[str]:
    """
    에세이 스트리밍 제너레이터.
    UI에서 직접 사용 가능.
    """
    school_label = req.school.upper() if req.school else "Common App"
    mode_label = {"draft": "✍️ 초안 작성", "revise": "🔧 개선", "critique": "🔍 비평"}[req.mode]

    yield f"**{mode_label}** → **{school_label}**"
    if req.prompt_key:
        yield f" / `{req.prompt_key.upper()}`"
    yield f" | 모델: [{ESSAY_MODEL} | temp={ESSAY_TEMPERATURE}]\n\n"

    # RAG 로드
    yield "🗂️ 개인 자료 검색 중..."
    rag_context, rag_sources = _get_rag_context(req.user_instruction)
    yield f" {len(rag_sources)}개 파일\n\n---\n\n"

    # 시스템 프롬프트
    if req.mode == "critique":
        system = _REVISE_SYSTEM.format(profile=json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))
        temp = REVISE_TEMPERATURE
    elif req.mode == "revise":
        system = _REVISE_SYSTEM.format(profile=json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))
        temp = REVISE_TEMPERATURE
    else:
        system = _ESSAY_SYSTEM.format(profile=json.dumps(USER_PROFILE, ensure_ascii=False, indent=2))
        temp = ESSAY_TEMPERATURE

    user_msg = build_essay_user_message(req, rag_context)

    # LLM 스트리밍
    accumulated = ""
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        stream = client.chat(
            model=ESSAY_MODEL if req.mode == "draft" else REVISE_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            options={
                "temperature": temp,
                "top_p": ESSAY_TOP_P,
                "repeat_penalty": ESSAY_REPEAT_PENALTY,
                "num_ctx": 16384,
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
        error_msg = f"\n\n❌ 에러: {e}\n`ollama pull {ESSAY_MODEL}` 확인"
        yield error_msg
        return

    # 단어 수 카운트
    wc = count_words(accumulated)
    prompt_info = get_prompt_info(req.school, req.prompt_key)
    limit = prompt_info["word_limit"] if prompt_info else req.word_limit
    min_w = prompt_info.get("min_words", 200) if prompt_info else 200

    wc_status = "✅" if min_w <= wc <= limit else ("⚠️ 초과" if wc > limit else "⚠️ 부족")
    yield f"\n\n---\n📊 **단어 수**: {wc} / {limit} {wc_status}"
    if rag_sources:
        yield f"\n📎 **참고 자료**: {', '.join(os.path.basename(s) for s in rag_sources)}"

# ══════════════════════════════════════════════════════════════════════════════
# 🛠️  편의 함수
# ══════════════════════════════════════════════════════════════════════════════

def quick_draft(school: str, prompt_key: str, story_idea: str) -> str:
    """동기 방식 에세이 생성 (CLI용)"""
    req = EssayRequest(
        user_instruction=story_idea,
        school=school,
        prompt_key=prompt_key,
        mode="draft",
    )
    result = ""
    for token in stream_essay(req):
        result += token
        print(token, end="", flush=True)
    return result


def quick_revise(school: str, prompt_key: str, existing_essay: str, notes: str = "") -> str:
    """동기 방식 에세이 개선 (CLI용)"""
    req = EssayRequest(
        user_instruction=notes or "기존 에세이를 최대한 개선해주세요",
        school=school,
        prompt_key=prompt_key,
        mode="revise",
        existing_essay=existing_essay,
    )
    result = ""
    for token in stream_essay(req):
        result += token
        print(token, end="", flush=True)
    return result


def check_system() -> str:
    statuses = []
    try:
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        installed = {m.model for m in client.list().models}
        essay_ok = any(ESSAY_MODEL.split(":")[0] in m for m in installed)
        statuses.append(f"{'🟢' if essay_ok else '🔴'} 에세이 모델: {ESSAY_MODEL}  (temp={ESSAY_TEMPERATURE})")
        statuses.append(f"{'🟢' if essay_ok else '🔴'} 수정 모델: {REVISE_MODEL}  (temp={REVISE_TEMPERATURE})")
    except Exception as e:
        statuses.append(f"🔴 Ollama 연결 실패: {e}")
    statuses.append(f"📋 Common App 프롬프트: {len(COMMON_APP_PROMPTS)}개")
    statuses.append(f"🏫 대학별 Supplemental: {sum(len(v) for v in SUPPLEMENTAL_PROMPTS.values())}개")
    return "\n".join(statuses)

# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="편입 에세이 에이전트")
    parser.add_argument("--school",  "-s", type=str, help="대학 키 (cornell/stanford/nyu/upenn)")
    parser.add_argument("--prompt",  "-p", type=str, help="프롬프트 키 (why/background/challenge 등)")
    parser.add_argument("--story",   "-t", type=str, help="담고 싶은 이야기/아이디어 (필수)")
    parser.add_argument("--mode",    "-m", type=str, default="draft",
                        choices=["draft", "revise", "critique"],
                        help="작업 모드: draft(초안) / revise(개선) / critique(비평만)")
    parser.add_argument("--essay-file", "-f", type=str, help="revise/critique 모드: 기존 에세이 파일 경로")
    parser.add_argument("--list",    "-l", action="store_true", help="프롬프트 목록 출력")
    parser.add_argument("--status",       action="store_true", help="시스템 상태")
    args = parser.parse_args()

    if args.status:
        print(check_system())
        sys.exit(0)

    if args.list:
        print(list_prompts(args.school))
        sys.exit(0)

    if not args.story and args.mode == "draft":
        parser.print_help()
        print("\n예시:\n  python 8_essay_agent.py --school cornell --prompt why --story \"딥러닝 연구실 경험과 Cornell AI 프로그램\"")
        sys.exit(1)

    existing = ""
    if args.essay_file:
        with open(args.essay_file, encoding="utf-8") as f:
            existing = f.read()

    req = EssayRequest(
        user_instruction=args.story or "에세이를 비평해주세요",
        school=args.school,
        prompt_key=args.prompt,
        mode=args.mode,
        existing_essay=existing,
    )

    print(f"\n{'='*64}")
    print(f"  ✍️  에세이 에이전트 — {args.mode.upper()} 모드")
    print(f"  모델: {ESSAY_MODEL}  (temp={ESSAY_TEMPERATURE})")
    print(f"{'='*64}\n")

    for token in stream_essay(req):
        print(token, end="", flush=True)
    print("\n")
