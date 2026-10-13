"""시장성 RAG 검색 평가: Hit Rate@K, MRR@10 (청크 단위).

    python scripts/eval_retrieval.py            # 결과를 출력하고 docs/retrieval_eval.md 에 저장

- 데이터: data/eval/retrieval_qa.json (질의 54개, 각 질의의 정답 문서 목록)
- 검색: agents/market_agent.py 와 같은 임베딩(BAAI/bge-m3, 정규화)·ChromaDB(cosine)로 상위 청크를 가져옴
- Hit Rate@K : 상위 K개 청크 중 정답 문서의 청크가 하나라도 있는 질의 비율
- MRR@10    : 정답 문서 청크가 처음 나온 순위의 역수 평균(상위 10개 안에 없으면 0)
- 운영 설정  : 시스템은 상위 TOP_K=4 청크 중 cosine 거리 0.4 이내만 채택하므로, 그 조건에서의 적중률도 함께 구함
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chromadb  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

KS = (1, 3, 4, 6, 10)
TOP_K, MAX_DISTANCE = 4, 0.4  # agents/market_agent.py 의 운영 설정


def main() -> None:
    qa = json.loads((ROOT / "data" / "eval" / "retrieval_qa.json").read_text(encoding="utf-8"))["items"]
    collection = chromadb.PersistentClient(path=str(ROOT / "chroma_db")).get_collection("rag_documents")
    model = SentenceTransformer("BAAI/bge-m3")
    embeddings = model.encode([q["query"] for q in qa], normalize_embeddings=True, convert_to_numpy=True)
    result = collection.query(
        query_embeddings=embeddings.tolist(), n_results=max(KS), include=["metadatas", "distances"]
    )

    hits = {k: 0 for k in KS}
    rr_sum = 0.0
    adopted_hit = 0
    adopted_any = 0
    by_style: dict[str, list[int]] = {}
    misses = []
    for item, metas, dists in zip(qa, result["metadatas"], result["distances"]):
        relevant = set(item["relevant"])
        rank = next((i for i, m in enumerate(metas, 1) if m["source"] in relevant), None)
        for k in KS:
            hits[k] += int(rank is not None and rank <= k)
        rr_sum += 1.0 / rank if rank else 0.0
        top = list(zip(metas[:TOP_K], dists[:TOP_K]))
        adopted = [(m, d) for m, d in top if d <= MAX_DISTANCE]
        adopted_any += int(bool(adopted))
        adopted_hit += int(any(m["source"] in relevant for m, _ in adopted))
        by_style.setdefault(item["style"], []).append(int(rank is not None and rank <= 4))
        if rank is None or rank > 4:
            misses.append((item["id"], item["query"], rank, metas[0]["source"][:30]))

    n = len(qa)
    lines = [
        "# 시장성 RAG 검색 평가",
        "",
        f"- 임베딩: BAAI/bge-m3 · 벡터DB: ChromaDB(cosine) · 문서 18건 / 청크 {collection.count()}개 · 질의 {n}개",
        "- 질의 구성: 문서 18건 × 3종(분야 질의 / 자연어 질문 / 키워드·영문). 정답은 사람이 인정하는 문서 목록(`data/eval/retrieval_qa.json`)",
        "",
        "| 지표 | 값 |",
        "|---|---|",
    ]
    lines += [f"| Hit Rate@{k} | {hits[k] / n:.3f} ({hits[k]}/{n}) |" for k in KS]
    lines += [
        f"| MRR@10 | {rr_sum / n:.3f} |",
        f"| 운영 설정(Top-{TOP_K}, 거리 ≤ {MAX_DISTANCE})에서 정답 문서를 채택한 비율 | {adopted_hit / n:.3f} ({adopted_hit}/{n}) |",
        f"| 운영 설정에서 아무 청크도 채택하지 못한 비율(→ 웹 보완 검색으로 대체) | {(n - adopted_any) / n:.3f} ({n - adopted_any}/{n}) |",
        "",
        "질의 유형별 Hit Rate@4: " + ", ".join(f"{s} {sum(v) / len(v):.2f}" for s, v in by_style.items()),
        "",
        "## 한계",
        "- 질의는 직접 작성한 소규모 세트(54개)라 절대 수치보다 설정 비교용으로 보는 것이 맞습니다.",
        "- 정답 문서가 겹치는 주제(태양광 3종, 연료전지 2종)는 사람이 인정하는 문서를 모두 정답으로 두었습니다.",
    ]
    if misses:
        lines += ["", "## Top-4 안에 정답이 없던 질의", "", "| id | 질의 | 정답 순위 | 1위로 나온 문서 |", "|---|---|---|---|"]
        lines += [f"| {i} | {q} | {r or '10위 밖'} | {s} |" for i, q, r, s in misses]
    text = "\n".join(lines) + "\n"
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "retrieval_eval.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
