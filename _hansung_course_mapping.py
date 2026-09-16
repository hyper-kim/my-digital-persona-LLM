"""
_hansung_course_mapping.py
한성과고 이수 과정 → 미국 대학 과목 매핑 분석기
삼성노트, 원노트, 시험지/종이필기 데이터 기반
"""

import os, sys, time, textwrap
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qdrant_client
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
import ollama

PROJECT_DIR = r"C:\My_Digital_Persona_Own_LLM_Project"
QDRANT_URL  = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_PATH = os.path.join(PROJECT_DIR, "qdrant_db")
EMBED_MODEL = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-small")
CHAT_MODEL  = "qwen3:14b"
TOP_K       = 12  # 과목별로 더 많이 가져오기

# 검색할 과목 쿼리 목록 (한국어+영어 혼용 → multilingual 임베딩 최대 활용)
SUBJECT_QUERIES = [
    # 수학 계열
    "고급수학 미적분 극한 수열 급수",
    "선형대수 벡터공간 행렬 고유값",
    "확률과통계 분포 가설검정",
    "이산수학 조합론 그래프이론",
    # 물리 계열
    "고급물리 역학 뉴턴 운동량 에너지",
    "전자기학 맥스웰 방정식 전기장 자기장",
    "파동 광학 양자역학",
    "열역학 통계역학",
    # 화학 계열
    "고급화학 원자구조 화학결합 분자궤도",
    "유기화학 반응메커니즘 탄소화합물",
    "물리화학 열역학 반응속도론",
    # 생물 계열
    "고급생물 세포생물학 유전학 유전체",
    "분자생물학 DNA 단백질 발현",
    # 정보/컴퓨터
    "알고리즘 자료구조 프로그래밍",
    "인공지능 머신러닝 딥러닝",
    # 과학 일반
    "시험지 필기 과학고 실험 보고서",
    "한성과고 1학년 2학년 3학년 과목",
    "수학올림피아드 KMO 과학올림피아드",
    # 대회/R&E
    "R&E 연구 논문 실험",
    "정보올림피아드 KOI 프로그래밍 대회",
]

# 한성과고 관련 소스 필터 키워드
HANSUNG_KEYWORDS = ["한성", "hansung", "삼성노트", "Samsung Note", "oneNote", "onenote",
                    "시험지", "필기", "과학고", "science high"]

def is_hansung_source(metadata: dict) -> bool:
    fp = str(metadata.get("file_path", "") + metadata.get("source", "")).lower()
    return any(k.lower() in fp for k in HANSUNG_KEYWORDS)

def build_index():
    print("[1/4] Qdrant 인덱스 로드 중 (최초 약 30초)...")
    qclient = (qdrant_client.QdrantClient(url=QDRANT_URL)
               if QDRANT_URL else
               qdrant_client.QdrantClient(path=QDRANT_PATH))
    vstore  = QdrantVectorStore(client=qclient, collection_name="omni_persona_v3")
    sctx    = StorageContext.from_defaults(vector_store=vstore)
    embed   = HuggingFaceEmbedding(model_name=EMBED_MODEL)
    idx     = VectorStoreIndex.from_vector_store(
        vector_store=vstore, storage_context=sctx, embed_model=embed)
    print("[1/4] 인덱스 로드 완료")
    return idx

def collect_hansung_chunks(idx) -> dict[str, list[str]]:
    print(f"[2/4] {len(SUBJECT_QUERIES)}개 쿼리로 한성과고 청크 수집 중...")
    retriever = idx.as_retriever(similarity_top_k=TOP_K)
    seen_ids  = set()
    by_subject: dict[str, list[str]] = {}

    for q in SUBJECT_QUERIES:
        try:
            nodes = retriever.retrieve(q)
        except Exception as e:
            print(f"  ⚠ 쿼리 실패 [{q[:30]}]: {e}")
            continue

        subject_chunks = []
        for node in nodes:
            nid = node.node_id
            if nid in seen_ids:
                continue
            text = node.get_content() or ""
            meta = node.metadata or {}
            # 한성과고 소스 우선, 아니면 score 기반으로도 포함
            fp = str(meta.get("file_path", "") + meta.get("source", "")).lower()
            is_hs = any(k.lower() in fp for k in HANSUNG_KEYWORDS)
            if not is_hs and (node.score or 0) < 0.5:
                continue
            seen_ids.add(nid)
            preview = text[:400].replace("\n", " ")
            source_tag = f"[{os.path.basename(meta.get('file_path', meta.get('source', 'unknown')))}]"
            subject_chunks.append(f"{source_tag} {preview}")

        if subject_chunks:
            by_subject[q] = subject_chunks
            print(f"  ✓ '{q[:40]}' → {len(subject_chunks)}청크")
        else:
            print(f"  - '{q[:40]}' → 매칭 없음")

    total = sum(len(v) for v in by_subject.values())
    print(f"[2/4] 총 {total}개 청크 수집 완료 ({len(by_subject)}개 주제)")
    return by_subject

def format_context(by_subject: dict[str, list[str]]) -> str:
    parts = []
    for subj, chunks in by_subject.items():
        parts.append(f"\n### 검색 주제: {subj}")
        for c in chunks[:4]:  # 주제당 최대 4개
            parts.append(f"  • {c[:350]}")
    return "\n".join(parts)[:30000]  # 전체 30K자 제한

MAPPING_PROMPT = textwrap.dedent("""
당신은 한국 과학고 교육과정 전문가이자 미국 명문대 입학처 어드바이저입니다.

아래는 **한성과학고등학교 (Hansung Science High School)** 학생의 삼성노트, 원노트, 
시험지 및 종이필기에서 수집된 실제 학습 데이터입니다.

=== 수집된 학습 데이터 (RAG 검색 결과) ===
{context}
=== 데이터 끝 ===

위 데이터를 바탕으로 **학년별(1학년, 2학년, 3학년)** 이수 과정을 분석하고,
각 과목이 미국 대학의 어떤 교육과정과 매핑되는지 상세히 작성하시오.

## 작성 형식

### 🎓 한성과학고 교육과정 개요
(과학고 특성 설명)

---
### 📚 1학년 (Grade 10 equivalent)
| 이수 과목 | 수준/내용 | 매핑되는 미국 대학 과목 | 해당 학점 | 근거 |
|---|---|---|---|---|
...

### 📚 2학년 (Grade 11 equivalent)
| 이수 과목 | 수준/내용 | 매핑되는 미국 대학 과목 | 해당 학점 | 근거 |
|---|---|---|---|---|
...

### 📚 3학년 (Grade 12 equivalent)
| 이수 과목 | 수준/내용 | 매핑되는 미국 대학 과목 | 해당 학점 | 근거 |
|---|---|---|---|---|
...

---
### 📊 종합 분석: 미국 대학 입학 시 인정 가능 학점 요약
(대학별 Credit transfer 가능성 포함 — Cornell, Stanford, NYU, UPenn 중심)

---
### 💡 미국 편입 시 시사점
(어떤 과목들이 편입 심사에서 강점이 되는지, AP/IB 동등성 논의 포함)

## 중요 지침
- 실제 데이터에서 확인된 내용만 서술 (없으면 "데이터 미확인" 명시)
- 미국 대학 과목명은 공식 명칭 사용 (예: Calculus I/II, Linear Algebra, Mechanics, E&M 등)
- AP 시험 동등 과목은 AP 코드도 함께 표기
- 과학고 고급 과목(고급수학, 고급물리, 고급화학 등)은 College-level로 명확히 표기
- 한국어로 상세히 작성하되 과목명은 영문 병기
""")

def run_analysis(context: str) -> str:
    print("[3/4] qwen3:14b로 과정 매핑 분석 중 (2-5분 소요)...")
    prompt = MAPPING_PROMPT.format(context=context)

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={
            "temperature": 0.3,
            "num_predict": 8192,
            "top_p": 0.9,
        },
        stream=True,
    )

    result_parts = []
    for chunk in response:
        part = chunk["message"]["content"]
        print(part, end="", flush=True)
        result_parts.append(part)
    print()  # newline
    return "".join(result_parts)

def save_result(result: str):
    out_path = os.path.join(PROJECT_DIR, "_hansung_course_mapping_result.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# 한성과학고 이수 과정 → 미국 대학 교육과정 매핑 분석\n\n")
        f.write(f"> 생성일: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> 데이터 소스: Qdrant omni_persona_v3 (삼성노트, 원노트, 시험지/종이필기)\n\n")
        f.write("---\n\n")
        f.write(result)
    print(f"\n[4/4] 결과 저장 완료: {out_path}")
    return out_path

def main():
    start = time.time()
    idx = build_index()
    by_subject = collect_hansung_chunks(idx)

    if not by_subject:
        print("⚠ 한성과고 관련 데이터가 Qdrant에서 검색되지 않았습니다.")
        print("  → 임베딩이 완료되지 않았거나 컬렉션이 비어있을 수 있습니다.")
        # 전체 top-k로 재시도
        print("  → 전체 데이터에서 상위 결과로 재시도합니다...")
        retriever = idx.as_retriever(similarity_top_k=30)
        try:
            nodes = retriever.retrieve("한성과학고 고급수학 물리 화학 수업 내용")
            total_text = "\n".join(
                f"[{os.path.basename(n.metadata.get('file_path', 'unknown'))}] {n.get_content()[:300]}"
                for n in nodes
            )
            by_subject = {"general_fallback": [total_text]}
        except Exception as e:
            print(f"  재시도 실패: {e}")
            sys.exit(1)

    context = format_context(by_subject)
    print(f"\n📄 컨텍스트 크기: {len(context):,}자\n")
    print("=" * 80)

    result = run_analysis(context)
    out_path = save_result(result)
    elapsed = time.time() - start
    print(f"\n✅ 총 소요 시간: {elapsed:.0f}초")
    print(f"📄 결과 파일: {out_path}")

if __name__ == "__main__":
    main()
