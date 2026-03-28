import os
import json
import ollama
import qdrant_client
from PIL import Image
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ==============================
# Configuration
# ==============================
PROJECT_DIR = os.getenv("PROJECT_DIR", r"C:\My_Digital_Persona_Own_LLM_Project")
QDRANT_PATH = os.getenv("QDRANT_PATH", os.path.join(PROJECT_DIR, "qdrant_db"))
QDRANT_URL  = os.getenv("QDRANT_URL", "")
LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434")
LOCAL_VISION_MODEL = os.getenv("LOCAL_VISION_MODEL", "qwen3-vl:8b")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "intfloat/multilingual-e5-large")
TOP_K = int(os.getenv("TOP_K", "8"))
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "3"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "14000"))


# ==============================
# Core utilities
# ==============================
def normalize_path(path):
    try:
        return os.path.abspath(path)
    except Exception:
        return path


def is_image_file(path):
    ext = os.path.splitext(path.lower())[1]
    return ext in [".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"]


def safe_collect_images(file_paths, max_images=3):
    images = []
    for p in file_paths:
        if len(images) >= max_images:
            break
        if not p or not os.path.exists(p):
            continue
        if not is_image_file(p):
            continue

        try:
            # 파일 무결성 확인 후 경로만 전달
            with Image.open(p) as img:
                img.verify()
            images.append(p)
        except Exception:
            continue

    return images


def build_system_prompt():
    return (
        "You are Omni-Brain, a source-grounded academic assistant. "
        "Use only the provided context and source files. "
        "If evidence is insufficient, say what is missing. "
        "When writing syllabus or essay drafts, align to top US university standards and mention assumptions explicitly."
    )


def build_user_prompt(query, context_text, source_paths):
    source_list = "\n".join(f"- {p}" for p in source_paths)
    return (
        f"[질문]\n{query}\n\n"
        f"[검색 컨텍스트]\n{context_text}\n\n"
        f"[근거 원본 파일 경로]\n{source_list}\n\n"
        "요구사항:\n"
        "1) 답변은 근거 기반으로 작성.\n"
        "2) 실라버스 요청이면 미국 최상위권 대학 수준(학습목표, 주차별 토픽, 평가방식, 레퍼런스)으로 작성.\n"
        "3) 에세이 요청이면 사용자 이력과 증거를 반영한 초안으로 작성.\n"
        "4) 마지막에 참고한 source file_path 목록을 다시 출력."
    )


# ==============================
# Retriever + Source-Augmented Generation
# ==============================
def build_index():
    client = qdrant_client.QdrantClient(url=QDRANT_URL) if QDRANT_URL else qdrant_client.QdrantClient(path=QDRANT_PATH)
    vector_store = QdrantVectorStore(client=client, collection_name="omni_persona_v3")
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME, device="cuda")
    return VectorStoreIndex.from_vector_store(vector_store=vector_store, storage_context=storage_context, embed_model=embed_model)


def retrieve_context(index, query):
    retriever = index.as_retriever(similarity_top_k=TOP_K)
    nodes = retriever.retrieve(query)

    selected_texts = []
    selected_paths = []

    for node in nodes:
        text = node.get_content()
        md = node.metadata or {}
        path = normalize_path(md.get("file_path", ""))

        if text:
            selected_texts.append(text)
        if path:
            selected_paths.append(path)

    merged = "\n\n".join(selected_texts)
    return merged[:MAX_CONTEXT_CHARS], sorted(set(selected_paths))


def generate_answer(query):
    index = build_index()
    context_text, source_paths = retrieve_context(index, query)

    if not context_text.strip():
        return "검색된 컨텍스트가 없습니다. 먼저 1_ingest_data.py로 인덱싱을 수행하세요."

    image_paths = safe_collect_images(source_paths, max_images=MAX_IMAGES)

    client = ollama.Client(host=LOCAL_OLLAMA_URL)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(query, context_text, source_paths)

    messages = [
        {"role": "system", "content": system_prompt},
    ]

    if image_paths:
        messages.append({"role": "user", "content": user_prompt, "images": image_paths})
    else:
        messages.append({"role": "user", "content": user_prompt})

    try:
        res = client.chat(
            model=LOCAL_VISION_MODEL,
            messages=messages,
            keep_alive=0,
            options={
                "num_ctx": 8192,
                "temperature": 0.2,
                "top_p": 0.9,
            },
        )
        answer = res.get("message", {}).get("content", "")
    except Exception as e:
        answer = f"로컬 추론 실패: {e}"

    report = {
        "query": query,
        "top_k": TOP_K,
        "used_image_count": len(image_paths),
        "source_paths": source_paths,
        "answer": answer,
    }
    return json.dumps(report, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("Omni-Brain 추론 에이전트 시작")
    print("종료하려면 exit 입력")

    while True:
        user_query = input("\n질문> ").strip()
        if not user_query:
            continue
        if user_query.lower() in ["exit", "quit"]:
            print("종료합니다.")
            break

        result = generate_answer(user_query)
        print("\n" + result)
