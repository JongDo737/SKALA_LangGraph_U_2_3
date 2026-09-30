# 작성자: 손민재
# 파일 설명: 친환경, 에너지, 핵융합 도메인의 시장조사 PDF를 읽어 Semantic Chunking 후
# BGE-M3 로 임베딩하고 ChromaDB(chroma_db/)에 저장합니다. (시장성 평가 에이전트가 검색하는 벡터DB를 만드는 파일)
#
# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------
# - 실행: python embed.py --reset   (기존 DB 삭제 후 data/for_embed/*.pdf 전체를 새로 임베딩)
# - Semantic Chunking: 문장 임베딩 유사도(0.65 미만)가 떨어지는 지점에서 분할, 청크 200~1200자
# - 스캔 PDF(텍스트 레이어 없음)는 macOS Vision OCR 로 읽음 (비 macOS 는 OCR 불가 → 경고 후 건너뜀)
# - 문서 메타데이터(domain / sub_domain / doc_type / region / keywords)를 청크마다 저장
#     · data/doc_metadata.json 에 있으면 그대로 사용, 없는 PDF 는 LLM(OPENAI_API_KEY)이 자동 생성해 저장
# - 새 PDF 추가: data/for_embed/ 에 넣고 다시 실행 (PDF 총 200페이지 이내 권장)
# ---------------------------------------------------------------------------


import argparse
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

import chromadb
import pymupdf as fitz
import numpy as np
from sentence_transformers import SentenceTransformer


# 경로 설정
BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "data" / "for_embed"
DB_DIR = BASE_DIR / "chroma_db"
DOC_METADATA_PATH = BASE_DIR / "data" / "doc_metadata.json"  # 문서(PDF)별 분야·유형 메타데이터

COLLECTION_NAME = "rag_documents"
MODEL_NAME = "BAAI/bge-m3"

# Semantic Chunking 설정
MAX_CHUNK_SIZE = 1200
MIN_CHUNK_SIZE = 200
SIMILARITY_THRESHOLD = 0.65

# 임베딩 설정
BATCH_SIZE = 32

# 스캔 PDF OCR 설정
OCR_DPI = 200


def ocr_page(page):
    """텍스트 레이어가 없는 스캔 페이지를 macOS Vision OCR(한국어/영어)로 읽는다."""
    try:
        import Vision
        from Foundation import NSData
    except ImportError:
        return ""

    png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
    data = NSData.dataWithBytes_length_(png, len(png))

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["ko-KR", "en-US"])
    request.setUsesLanguageCorrection_(True)

    success, _ = handler.performRequests_error_([request], None)

    if not success:
        return ""

    return "\n".join(
        observation.topCandidates_(1)[0].string()
        for observation in request.results()
    )


def extract_pdf_pages(pdf_path):
    """PDF에서 페이지별 텍스트를 추출한다. 텍스트가 없는 페이지는 OCR로 대체한다."""
    pages = []

    doc = fitz.open(pdf_path)

    for page_number, page in enumerate(doc):
        text = page.get_text("text").strip()

        if not text:
            text = ocr_page(page).strip()

            if text:
                print(f"  (p.{page_number + 1} OCR 적용)")

        if text:
            pages.append(
                {
                    "page": page_number + 1,
                    "text": text,
                }
            )

    doc.close()

    return pages


def split_sentences(text):
    """텍스트를 문장 단위로 분리한다."""
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return []

    sentences = re.split(
        r"(?<=[.!?。！？])\s+|(?<=다)\.\s+|(?<=요)\.\s+",
        text,
    )

    return [
        sentence.strip()
        for sentence in sentences
        if sentence.strip()
    ]


def split_paragraphs(text):
    """페이지 텍스트를 문단 단위로 분리한다."""
    paragraphs = re.split(r"\n\s*\n+", text)

    return [
        paragraph.strip()
        for paragraph in paragraphs
        if paragraph.strip()
    ]


def cosine_similarity(a, b):
    """두 임베딩의 cosine similarity를 계산한다."""
    denominator = np.linalg.norm(a) * np.linalg.norm(b)

    if denominator == 0:
        return 0.0

    return float(np.dot(a, b) / denominator)


def semantic_chunk_page(text, model):
    """페이지 텍스트를 의미 기반 Chunk로 분리한다."""
    paragraphs = split_paragraphs(text)

    all_sentences = []

    for paragraph in paragraphs:
        sentences = split_sentences(paragraph)

        if sentences:
            all_sentences.extend(sentences)

    if not all_sentences:
        return []

    if len(all_sentences) == 1:
        return all_sentences

    embeddings = model.encode(
        all_sentences,
        normalize_embeddings=True,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )

    chunks = []
    current_chunk = [all_sentences[0]]
    current_length = len(all_sentences[0])

    for i in range(1, len(all_sentences)):
        sentence = all_sentences[i]
        sentence_length = len(sentence)

        similarity = cosine_similarity(
            embeddings[i - 1],
            embeddings[i],
        )

        exceeds_max_size = (
            current_length + sentence_length > MAX_CHUNK_SIZE
        )

        semantic_break = (
            similarity < SIMILARITY_THRESHOLD
            and current_length >= MIN_CHUNK_SIZE
        )

        if exceeds_max_size or semantic_break:
            chunks.append(" ".join(current_chunk))

            current_chunk = [sentence]
            current_length = sentence_length

        else:
            current_chunk.append(sentence)
            current_length += sentence_length

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return split_oversized_chunks(chunks)


def split_oversized_chunks(chunks):
    """Semantic Chunking 후에도 너무 긴 Chunk를 보조적으로 분할한다."""
    result = []

    for chunk in chunks:
        if len(chunk) <= MAX_CHUNK_SIZE:
            result.append(chunk)
            continue

        sentences = split_sentences(chunk)

        current = []
        current_length = 0

        for sentence in sentences:
            sentence_length = len(sentence)

            if (
                current
                and current_length + sentence_length > MAX_CHUNK_SIZE
            ):
                result.append(" ".join(current))
                current = []
                current_length = 0

            current.append(sentence)
            current_length += sentence_length

        if current:
            result.append(" ".join(current))

    return result


def load_doc_metadata():
    """파일명 → 문서 메타데이터(domain/sub_domain/doc_type/region/keywords). 파일명은 NFC 로 맞춘다."""
    if not DOC_METADATA_PATH.exists():
        return {}

    data = json.loads(DOC_METADATA_PATH.read_text(encoding="utf-8"))

    return {unicodedata.normalize("NFC", name): meta for name, meta in data.items()}


DOC_META_SYSTEM = """너는 시장조사·연구 PDF의 메타데이터를 만드는 사서다. 파일명과 앞부분 텍스트를 보고 채운다.
- domain: 상위 분야 (예: 데이터센터, 배터리·에너지저장, 재생에너지, 핵융합)
- sub_domain: 이 문서가 다루는 구체적인 시장·기술 (예: 태양열 집광기(CSP), 데이터센터 냉각). 인접하지만 다른 기술은 서로 다른 값으로 구분한다.
- doc_type: market_report(시장 조사 보고서) / paper(논문·연구) / policy_report(정책·기관 보고서) / other
- region: 문서가 다루는 지역. 글로벌이면 global, 한 나라면 ISO 2자리 코드(예: KR)
- keywords: 검색에 도움이 되는 핵심어 4~6개, 쉼표로 구분
이미 쓰인 domain / sub_domain 이 목록에 주어지면 같은 분야는 그 표기를 그대로 재사용해 일관되게 한다."""


def auto_tag(pdf_path, known_metadata):
    """메타데이터가 없는 PDF 를 LLM 으로 태깅한다. API 키가 없거나 실패하면 None."""
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")

    if not os.getenv("OPENAI_API_KEY"):
        return None

    from typing import Literal

    from openai import OpenAI
    from pydantic import BaseModel

    class DocMeta(BaseModel):
        domain: str
        sub_domain: str
        doc_type: Literal["market_report", "paper", "policy_report", "other"]
        region: str
        keywords: str

    text = "\n".join(page["text"] for page in extract_pdf_pages(pdf_path)[:3])[:3000]

    if not text.strip():
        return None

    domains = sorted({meta["domain"] for meta in known_metadata.values()})
    sub_domains = sorted({meta["sub_domain"] for meta in known_metadata.values()})

    try:
        response = OpenAI().chat.completions.parse(
            model=os.getenv("MARKET_LLM_MODEL", "gpt-4.1-mini"),
            temperature=0,
            messages=[
                {"role": "system", "content": DOC_META_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"파일명: {pdf_path.name}\n"
                        f"이미 쓰인 domain: {domains}\n이미 쓰인 sub_domain: {sub_domains}\n\n"
                        f"앞부분 텍스트:\n{text}"
                    ),
                },
            ],
            response_format=DocMeta,
        )
    except Exception as error:
        print(f"  ! 자동 태깅 실패: {error}")
        return None

    return response.choices[0].message.parsed.model_dump()


def save_doc_metadata(doc_metadata):
    """자동 태깅 결과를 doc_metadata.json 에 저장한다. (다음 실행부터는 캐시로 쓰이고, 직접 고칠 수도 있다)"""
    DOC_METADATA_PATH.write_text(
        json.dumps(doc_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def create_chunks(pdf_path, model, doc_metadata=None):
    """PDF 전체를 읽어서 Semantic Chunk를 생성한다."""
    pages = extract_pdf_pages(pdf_path)

    # 문서 단위 메타데이터. 없으면 unknown 으로 표시한다. (Chroma 메타데이터는 문자열·숫자만 가능)
    doc_meta = (doc_metadata or {}).get(
        unicodedata.normalize("NFC", pdf_path.name),
        {"domain": "unknown", "sub_domain": "unknown", "doc_type": "unknown", "region": "unknown", "keywords": ""},
    )

    chunks = []

    for page_data in pages:
        page_number = page_data["page"]
        page_text = page_data["text"]

        page_chunks = semantic_chunk_page(
            page_text,
            model,
        )

        for chunk_index, chunk_text in enumerate(page_chunks):
            if not chunk_text.strip():
                continue

            chunks.append(
                {
                    "text": chunk_text,
                    "metadata": {
                        "source": pdf_path.name,
                        "page": page_number,
                        "chunk": chunk_index,
                        **doc_meta,
                    },
                }
            )

    return chunks


def create_chroma_collection():
    """ChromaDB Collection을 생성한다."""
    client = chromadb.PersistentClient(
        path=str(DB_DIR)
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        configuration={
            "hnsw": {
                "space": "cosine"
            }
        },
        metadata={
            "embedding_model": MODEL_NAME,
            "embedding_dimension": 1024,
            "chunking": "semantic",
        },
    )

    return collection


def reset_database():
    """기존 ChromaDB를 삭제한다."""
    if DB_DIR.exists():
        shutil.rmtree(DB_DIR)

        print("기존 ChromaDB를 삭제했습니다.")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--reset",
        action="store_true",
        help="기존 ChromaDB를 삭제하고 새로 생성합니다.",
    )

    args = parser.parse_args()

    if args.reset:
        reset_database()

    PDF_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("=" * 70)
    print("BGE-M3 Semantic Chunking RAG Embedding")
    print("=" * 70)

    print(f"PDF directory : {PDF_DIR}")
    print(f"ChromaDB      : {DB_DIR}")
    print(f"Model         : {MODEL_NAME}")
    print(f"Max chunk     : {MAX_CHUNK_SIZE}")
    print(f"Min chunk     : {MIN_CHUNK_SIZE}")
    print(f"Threshold     : {SIMILARITY_THRESHOLD}")
    print()

    print("BGE-M3 모델을 로딩합니다...")

    model = SentenceTransformer(
        MODEL_NAME
    )

    print("모델 로딩 완료")
    print()

    pdf_files = sorted(
        PDF_DIR.glob("*.pdf")
    )

    if not pdf_files:
        print("PDF 파일을 찾을 수 없습니다.")
        return

    print(f"PDF 파일 수: {len(pdf_files)}")
    print()

    doc_metadata = load_doc_metadata()

    all_chunks = []

    for pdf_path in pdf_files:
        print(f"[PDF] {pdf_path.name}")

        name = unicodedata.normalize("NFC", pdf_path.name)

        if name not in doc_metadata:
            tagged = auto_tag(pdf_path, doc_metadata)

            if tagged:
                doc_metadata[name] = tagged
                save_doc_metadata(doc_metadata)
                print(f"  + 메타데이터 자동 생성: {tagged['domain']} / {tagged['sub_domain']} ({tagged['doc_type']}, {tagged['region']})")
            else:
                print(f"  ! 메타데이터가 없고 자동 생성도 못 했습니다 (OPENAI_API_KEY 확인). unknown 으로 저장")

        chunks = create_chunks(
            pdf_path,
            model,
            doc_metadata,
        )

        print(f"  → Semantic Chunk {len(chunks)}개 생성")

        if not chunks:
            print("  ! 청크가 0개입니다. 텍스트가 없는 스캔 PDF 로 보이며, OCR(macOS 전용)을 쓸 수 없는 환경일 수 있습니다.")

        all_chunks.extend(chunks)

    if not all_chunks:
        print("생성된 Chunk가 없습니다.")
        return

    print()
    print(f"전체 Chunk 수: {len(all_chunks)}")
    print()

    documents = [
        chunk["text"]
        for chunk in all_chunks
    ]

    print("BGE-M3 임베딩을 생성합니다...")

    embeddings = model.encode(
        documents,
        normalize_embeddings=True,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
        convert_to_numpy=True,
    )

    print("임베딩 생성 완료")
    print()

    collection = create_chroma_collection()

    ids = [
        f"chunk-{index}"
        for index in range(len(all_chunks))
    ]

    metadatas = [
        chunk["metadata"]
        for chunk in all_chunks
    ]

    print("ChromaDB에 저장합니다...")

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings.tolist(),
        metadatas=metadatas,
    )

    print()
    print("=" * 70)
    print("Embedding 완료")
    print("=" * 70)
    print(f"Collection : {COLLECTION_NAME}")
    print(f"Documents  : {collection.count()}")
    print(f"Embedding  : {embeddings.shape}")
    print("=" * 70)


if __name__ == "__main__":
    main()