"""
6_advisor_agent.py — 대학별·스펙별 합격가능성 분석 어드바이저
==============================================================
역할: 
  1. 4_transfer_agent에서 팩트 수집 (SAT 면제 여부, 영어 최저 등)
  2. Qdrant RAG에서 메타정보 검색 (합격률, 평균 GPA, 학비 등)
  3. 사용자 프로필과 비교 분석 → 합격가능성·추천/경고
  4. 유학원 어드바이저처럼 전략 제시

실행: python 6_advisor_agent.py
"""

import os
import sys
import json
import textwrap
from typing import TypedDict, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ollama
import qdrant_client
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# 팩트체크 모듈 임포트
import importlib
spec_module = importlib.import_module("4_transfer_agent")

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  설정
# ══════════════════════════════════════════════════════════════════════════════
PROJECT_DIR       = os.getenv("PROJECT_DIR",       r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH       = os.getenv("QDRANT_PATH",       os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL        = os.getenv("QDRANT_URL", "")
LOCAL_OLLAMA_URL  = os.getenv("LOCAL_OLLAMA_URL",  "http://localhost:11434")
EMBED_MODEL_NAME  = os.getenv("EMBED_MODEL_NAME",  "intfloat/multilingual-e5-large")
ADVISOR_MODEL     = os.getenv("ADVISOR_MODEL",     "qwen3:14b")
TOP_K             = int(os.getenv("TOP_K",             "5"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "10000"))

# ── 사용자 프로필 ────────────────────────────────────────────────────────────
USER_PROFILE = {
    "school":     "Korea University (고려대학교)",
    "major":      "Artificial Intelligence",
    "gpa_scored": 3.95,
    "gpa_total":  4.5,
    "gpa_us":     (3.95 / 4.5) * 4.0,  # ~3.51 on 4.0 scale
    "highschool": "Hansung Science High School (한성과학고)",
    "toefl":      85,
    "duolingo":   None,
    "sat":        None,
    "credits":    60,
    "applied":    ["cornell", "stanford", "nyu", "upenn"],
}

# ══════════════════════════════════════════════════════════════════════════════
# 📊  데이터 타입 정의
# ══════════════════════════════════════════════════════════════════════════════


class UniversityMetadata(TypedDict, total=False):
    """대학 메타정보 (RAG에서 검색)"""

    name: str
    avg_gpa: float
    avg_sat: Optional[int]
    toefl_min: Optional[int]
    acceptance_rate: float
    international_rate: float
    cost_per_year: int
    avg_aid_int: int
    top_majors: list[str]
    notable_features: list[str]


class FitAnalysis(TypedDict, total=False):
    """합격가능성 분석 결과"""

    university: str
    fit_score: int  # 0-100
    verdict: str  # "추천" | "경쟁" | "도전" | "재고"
    gpa_fit: str  # "우수" | "충분" | "부족"
    english_fit: str  # "충분" | "경계" | "부족"
    sat_fit: str  # "면제" | "충분" | "부족"
    holistic_bonus: str  # "강함" | "중간" | "약함"
    warnings: list[str]  # 🔴 주의사항
    opportunities: list[str]  # 💡 강화 포인트
    advisor_opinion: str  # 어드바이저 톤의 조언
    estimated_probability: str  # "75-85%" 형태


# ══════════════════════════════════════════════════════════════════════════════
# 🗄️  RAG 헬퍼
# ══════════════════════════════════════════════════════════════════════════════
_rag_index: Optional[VectorStoreIndex] = None


def _get_rag_index() -> "VectorStoreIndex | None":
    global _rag_index
    if _rag_index is None:
        try:
            qclient = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
            vector_store = QdrantVectorStore(qclient, collection_name="omni_persona_v3")
            storage_ctx = StorageContext.from_defaults(vector_store=vector_store)
            embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)
            _rag_index = VectorStoreIndex.from_vector_store(
                vector_store=vector_store,
                storage_context=storage_ctx,
                embed_model=embed_model,
            )
        except Exception as e:
            print(f"⚠️  RAG 인덱스 로드 실패: {e}")
            return None
    return _rag_index


def search_metadata(university_name: str) -> UniversityMetadata:
    """대학 메타정보를 RAG에서 검색"""
    idx = _get_rag_index()
    if idx is None:
        return {}

    try:
        retriever = idx.as_retriever(similarity_top_k=TOP_K)
        query = (
            f"{university_name} 합격률 평균GPA 토플 SAT 학비 국제학생 "
            f"지원자 통계 특징 장점"
        )
        nodes = retriever.retrieve(query)

        texts = [node.get_content() for node in nodes if node.get_content()]
        merged = "\n".join(texts)[:MAX_CONTEXT_CHARS]

        # LLM으로 메타정보 추출
        client = ollama.Client(host=LOCAL_OLLAMA_URL)
        res = client.chat(
            model=ADVISOR_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "다음 텍스트에서 JSON 형식으로 대학 메타정보를 추출하세요.\n"
                        "없는 정보는 null로. "
                        "반환: {avg_gpa, avg_sat, toefl_min, acceptance_rate, international_rate, "
                        "cost_per_year, avg_aid_int, top_majors, notable_features}"
                    ),
                },
                {"role": "user", "content": merged},
            ],
            options={"temperature": 0, "top_p": 1},
            keep_alive=0,
        )

        try:
            import re

            match = re.search(r"\{.*\}", res["message"]["content"], re.DOTALL)
            if match:
                data = json.loads(match.group())
                return {
                    "name": university_name,
                    "avg_gpa": data.get("avg_gpa"),
                    "avg_sat": data.get("avg_sat"),
                    "toefl_min": data.get("toefl_min"),
                    "acceptance_rate": data.get("acceptance_rate"),
                    "international_rate": data.get("international_rate"),
                    "cost_per_year": data.get("cost_per_year"),
                    "avg_aid_int": data.get("avg_aid_int"),
                    "top_majors": data.get("top_majors", []),
                    "notable_features": data.get("notable_features", []),
                }
        except Exception:
            pass

    except Exception as e:
        print(f"⚠️  메타정보 검색 실패: {e}")

    return {}


# ══════════════════════════════════════════════════════════════════════════════
# 🎓  어드바이저 핵심 분석 로직
# ══════════════════════════════════════════════════════════════════════════════


def analyze_fit(uni_key: str) -> FitAnalysis:
    """대학과 사용자 매칭도 분석 (0-100 점수)"""

    uni_name = spec_module.UNIVERSITY_REGISTRY.get(uni_key, {}).get("name", uni_key)
    print(f"  📊 {uni_name} 분석 중...", flush=True)

    # ── 1) 팩트 수집 ─────────────────────────────────────────────────────────
    facts = {"sat_waived": False, "toefl_min": 0, "duolingo_accepted": False}
    try:
        # 4_transfer_agent에서 요건 페칭 (간단히)
        # 실제로는 fetch_university_pages로 페이지 긁어서 분석하는 게 나음
        pages = spec_module.fetch_university_pages(uni_key)
        if pages:
            # 페이지 텍스트에서 SAT/TOEFL 요건 추출 (정규식 또는 LLM)
            page_text = "\n".join(p.get("text", "") for p in pages)
            if "waived" in page_text.lower() or "optional" in page_text.lower():
                facts["sat_waived"] = True
            # TOEFL 최저점 추출 (예: "TOEFL iBT 90" → 90)
            import re

            toefl_match = re.search(r"TOEFL.*?(\d{2,3})", page_text)
            if toefl_match:
                facts["toefl_min"] = int(toefl_match.group(1))
    except Exception as e:
        print(f"    ⚠️  팩트 추출 실패: {e}")

    # ── 2) 메타정보 검색 ─────────────────────────────────────────────────────
    meta = search_metadata(uni_name)

    # ── 3) 점수 계산 ────────────────────────────────────────────────────────
    score = 50  # 기본점

    # GPA 평가 (가중치: 30점)
    gpa_fit = "부족"
    if meta.get("avg_gpa"):
        avg_gpa = meta["avg_gpa"]
        user_gpa = USER_PROFILE["gpa_us"]
        if user_gpa >= avg_gpa + 0.3:
            score += 25
            gpa_fit = "우수"
        elif user_gpa >= avg_gpa:
            score += 15
            gpa_fit = "충분"
        elif user_gpa >= avg_gpa - 0.2:
            score += 5
            gpa_fit = "경계"
        else:
            score -= 10
            gpa_fit = "부족"

    # 영어점수 평가 (가중치: 25점)
    english_fit = "충분"
    toefl_min = facts.get("toefl_min") or meta.get("toefl_min") or 80
    if USER_PROFILE["toefl"] >= toefl_min:
        score += 20
        english_fit = "충분"
    elif USER_PROFILE["toefl"] >= toefl_min - 5:
        score += 10
        english_fit = "경계"
    else:
        score -= 15
        english_fit = "부족"

    # SAT 평가 (가중치: 20점)
    sat_fit = "면제"
    if facts.get("sat_waived") or USER_PROFILE["credits"] >= 30:
        score += 20
        sat_fit = "면제"
    else:
        score -= 20
        sat_fit = "부족"

    # Holistic 보너스 (한국 명문대+과고+AI) (가중치: 15점)
    holistic_bonus = "약함"
    if USER_PROFILE["school"] == "Korea University (고려대학교)":
        holistic_bonus = "중간"
        score += 8
        if USER_PROFILE["highschool"] == "Hansung Science High School (한성과학고)":
            holistic_bonus = "강함"
            score += 7

    # 전공 매칭 (가중치: 10점)
    if meta.get("top_majors"):
        if any("AI" in m or "computer" in m.lower() for m in meta["top_majors"]):
            score += 8

    score = min(100, max(0, score))

    # ── 4) 결론 도출 ───────────────────────────────────────────────────────
    verdict = "추천"
    if score >= 75:
        verdict = "추천"
        prob = "75-90%"
    elif score >= 60:
        verdict = "경쟁"
        prob = "50-65%"
    elif score >= 45:
        verdict = "도전"
        prob = "25-40%"
    else:
        verdict = "재고"
        prob = "10-25%"

    # ── 5) 경고 & 기회 ──────────────────────────────────────────────────────
    warnings = []
    opportunities = []

    if english_fit == "부족":
        warnings.append("🔴 TOEFL 점수가 대학 최저 이하입니다. 입학처 직접 문의 필수.")
    if english_fit == "경계":
        warnings.append("⚠️ TOEFL 점수가 최저선에 가깝습니다. 다른 서류로 보완 필요.")
    if gpa_fit == "부족":
        warnings.append("⚠️ GPA가 평균보다 낮지만 전공(AI) 강점으로 보완 가능.")

    if score >= 60 and holistic_bonus == "강함":
        opportunities.append(
            "💡 한성과고 + 고려대 AI 배경이 강력합니다. "
            "에세이에서 이 경로의 의미를 구체적으로 표현하세요."
        )
    if USER_PROFILE["toefl"] >= toefl_min:
        opportunities.append("💡 영어점수가 충분합니다. Duolingo 응시 불필요.")

    # ── 6) 어드바이저 의견 ──────────────────────────────────────────────────
    advisor_opinion = _generate_advisor_opinion(
        uni_name, verdict, gpa_fit, english_fit, sat_fit, meta, score
    )

    return {
        "university": uni_name,
        "fit_score": score,
        "verdict": verdict,
        "gpa_fit": gpa_fit,
        "english_fit": english_fit,
        "sat_fit": sat_fit,
        "holistic_bonus": holistic_bonus,
        "warnings": warnings,
        "opportunities": opportunities,
        "advisor_opinion": advisor_opinion,
        "estimated_probability": prob,
    }


def _generate_advisor_opinion(
    uni_name: str,
    verdict: str,
    gpa_fit: str,
    english_fit: str,
    sat_fit: str,
    meta: UniversityMetadata,
    score: int,
) -> str:
    """유학원 어드바이저 톤의 조언 생성"""
    lines = []

    if verdict == "추천":
        lines.append(f"✅ **{uni_name} 강력 추천**")
        lines.append(
            f"당신의 스펙(GPA {USER_PROFILE['gpa_us']:.2f}, TOEFL {USER_PROFILE['toefl']})은 "
            f"{uni_name}의 평균 입학자를 능가합니다."
        )
    elif verdict == "경쟁":
        lines.append(f"⚖️ **{uni_name} 경쟁 대학**")
        lines.append(
            f"GPA와 영어점수가 충분하지만, 높은 경쟁률로 인해 "
            f"에세이·추천서·경력이 중요합니다."
        )
    elif verdict == "도전":
        lines.append(f"🎯 **{uni_name} 도전 대학**")
        lines.append(
            f"점수상 불리하지만, 강한 에세이와 경험으로 "
            f"역전 가능한 케이스입니다. 신청할 가치 있습니다."
        )
    else:
        lines.append(f"⛔ **{uni_name} 신중히 재고**")
        lines.append(
            f"현재 스펙으로는 매우 도전적입니다. "
            f"TOEFL 재응시 또는 다른 대학 검토 권장."
        )

    lines.append("")
    lines.append("**구체적 조언:**")

    if gpa_fit == "우수":
        lines.append("- GPA가 뛰어나 경쟁 우위에 있습니다.")
    elif gpa_fit == "부족":
        lines.append(
            "- GPA 부족분을 고대 AI 프로그램의 엄격함과 "
            "과고 배경으로 보상하세요 (에세이 필수)."
        )

    if english_fit == "충분":
        lines.append(
            f"- TOEFL {USER_PROFILE['toefl']}은 충분합니다. "
            f"Duolingo 응시 불필요."
        )
    elif english_fit == "부족":
        lines.append(
            f"- TOEFL {USER_PROFILE['toefl']}은 요건 이하입니다. "
            f"입학처에 상황을 설명하거나 Duolingo 응시 고려."
        )

    if sat_fit == "면제":
        lines.append("- SAT 제출 불필요합니다.")

    if meta.get("cost_per_year"):
        cost = meta["cost_per_year"]
        lines.append(f"- 연간 학비: ${cost:,} (~장학금 후 일부 충당 가능)")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 🚀  CLI 인터페이스
# ══════════════════════════════════════════════════════════════════════════════


def run_advisor_analysis(uni_keys: Optional[list[str]] = None) -> None:
    """여러 대학 분석 실행"""
    if uni_keys is None:
        uni_keys = list(USER_PROFILE["applied"])  # 지원 완료 대학

    print("\n" + "=" * 72)
    print("  🎓 대학별 합격가능성 분석 (Advisor Mode)")
    print(f"  학생: {USER_PROFILE['school']} {USER_PROFILE['major']}")
    print(
        f"  스펙: GPA {USER_PROFILE['gpa_us']:.2f}/4.0 | TOEFL {USER_PROFILE['toefl']} | "
        f"{USER_PROFILE['credits']} credits"
    )
    print("=" * 72 + "\n")

    results = {}
    for uni_key in uni_keys:
        try:
            analysis = analyze_fit(uni_key)
            results[uni_key] = analysis

            # 결과 출력
            print(f"\n📊 {analysis['university']}")
            print(f"   점수: {analysis['fit_score']}/100  |  판정: {analysis['verdict']}")
            print(f"   합격확률: {analysis['estimated_probability']}")
            print(f"\n   GPA: {analysis['gpa_fit']} | 영어: {analysis['english_fit']} | SAT: {analysis['sat_fit']}")
            print(f"   Holistic: {analysis['holistic_bonus']}")

            if analysis["warnings"]:
                print(f"\n   경고사항:")
                for w in analysis["warnings"]:
                    print(f"   {w}")

            if analysis["opportunities"]:
                print(f"\n   강화 포인트:")
                for o in analysis["opportunities"]:
                    print(f"   {o}")

            print(f"\n   💬 어드바이저 의견:")
            for line in analysis["advisor_opinion"].split("\n"):
                print(f"      {line}")

        except Exception as e:
            print(f"❌ {uni_key} 분석 실패: {e}")

    # 요약 레이킹
    print("\n" + "=" * 72)
    print("📈 최종 요약 (점수 순서)")
    print("=" * 72)
    ranked = sorted(results.items(), key=lambda x: x[1]["fit_score"], reverse=True)
    for rank, (uni_key, analysis) in enumerate(ranked, 1):
        print(
            f"{rank}. {analysis['university']:<30} "
            f"[{analysis['fit_score']}/100] {analysis['verdict']:<10} "
            f"({analysis['estimated_probability']})"
        )

    print("\n💡 전략:")
    print("- 상위 대학(점수 75+)에 우선 집중하되, 도전 대학도 신청할 가치 있음")
    print("- 모든 에세이에 과고→고대→미국 편입 경로의 의미 명확히 표현")
    print("- TOEFL 부족하면 Duolingo 또는 입학처 문의 검토")
    print("- 추천서/이력서: AI 전공·리더십·다문화 경험 강조")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="편입 어드바이저: 대학별 합격가능성 분석"
    )
    parser.add_argument(
        "--universities",
        nargs="+",
        default=None,
        help="대학 코드 (예: cornell stanford nyu upenn). 기본값: 지원 완료 대학",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="모든 등록된 대학 분석 (느림)",
    )

    args = parser.parse_args()

    if args.all:
        uni_keys = list(spec_module.UNIVERSITY_REGISTRY.keys())
    elif args.universities:
        uni_keys = args.universities
    else:
        uni_keys = None

    run_advisor_analysis(uni_keys)
