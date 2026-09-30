# 작성자: 손민재
# 파일 설명: 친환경, 에너지, 핵융합 도메인의 시장조사 PDF를 학습한 벡터DB(ChromaDB)에서
# 기업별 시장 정보를 찾아 시장 규모 / 성장성 / 수요 근거로 정리합니다. 정보가 부족하면 웹 검색으로 보완합니다.
#
# ---------------------------------------------------------------------------
# 요약 (rag.py 의 market_research_node 가 이 파일의 MarketAgent 를 사용)
# ---------------------------------------------------------------------------
# - MarketAgent.run(companies): 기업 리스트 → 각 기업에 market_context(source / summary / sources) 부착, 기업별 병렬 처리
# - 검색 질의: 기업의 사업 분야 문구 + 뒤쪽 단어를 줄인 짧은 문구들 중 가장 가까운 청크 채택
# - 분야 필드가 없으면 회사명·업종명·주소로 LLM 이 분야(topic/subdomain/country/description) 추정
# - 업종코드(KSIC) 뜻은 웹에서 조회해 data/ksic_cache.json 에 캐시 (LLM 기억에 의존하지 않음)
# - 관련성 판별: 검색된 문서의 sub_domain 이 기업 산업과 다르면 제외 → 웹(Research Nester)으로 대체
# - 요약: 자료에 있는 내용만 사용, 숫자 없는 규모·성장성은 null (지어내지 않음)
# - 단독 실행: python market_agent.py [--limit N] [--workers N]  (data/input/*.json → data/output/)
# - 설정 값(MAX_DISTANCE, TOP_K, MAX_WORKERS 등)은 아래 상단 상수 참고
# ---------------------------------------------------------------------------

"""시장성 평가 에이전트.

스타트업 탐색 에이전트가 넘겨준 기업 목록(약 10개)에 대해
  1) 벡터DB(RAG)에서 해당 기업 분야와 매칭되는 시장 자료를 찾고,
  2) RAG에 충분히 관련된 내용이 없으면 웹 검색(Research Nester) 자료로 대체한 뒤,
  3) 자료를 LLM 으로 시장 규모 / 성장성 / 수요 근거로 정리해서 붙여
다음 단계인 경쟁사 비교 에이전트로 넘긴다. (점수/평가는 하지 않는다. 원문은 붙이지 않고 출처만 남긴다.)

사용:
    python market_agent.py            # data/input/*.json 처리 → data/output/
    OPENAI_API_KEY 는 환경변수 또는 .env 에 있어야 한다.
"""

import argparse
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chromadb
import requests
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

AGENTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENTS_DIR.parent
BASE_DIR = PROJECT_ROOT  # 하위 호환 별칭
DB_DIR = PROJECT_ROOT / "chroma_db"
INPUT_DIR = PROJECT_ROOT / "data" / "input"  # 단독 실행용 JSON 입력
KSIC_CACHE = PROJECT_ROOT / "data" / "ksic_cache.json"  # 업종코드 → 업종명 조회 결과 캐시
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"  # 단독 실행용 JSON 출력
COLLECTION_NAME = "rag_documents"
MODEL_NAME = "BAAI/bge-m3"

# cosine distance 기준. 실측: 관련 주제 0.29~0.41, 코퍼스에 없는 주제 0.49 이상 (0.4 로 설정)
MAX_DISTANCE = 0.4
TOP_K = 4  # 요약에 쓸 RAG 청크 수 (가까운 순)
MIN_HITS = 1  # 기준 이내 청크가 이만큼 미만이면 웹 검색으로 대체
MIN_QUERY_WORDS = 2  # 질의를 줄일 때 남기는 최소 단어 수 (너무 짧으면 엉뚱한 문서에 걸린다)
WEB_MAX_RESULTS = 3  # 요약에 쓸 웹 검색 결과 수
LLM_MODEL = "gpt-4.1-mini"  # 환경변수 MARKET_LLM_MODEL 로 변경 가능
MAX_CONTEXT_CHARS = 1500  # 자료 1건당 LLM 에 넣는 최대 글자 수
MAX_WORKERS = 5  # 기업을 동시에 처리하는 수 (LLM·웹 검색 호출을 병렬로)
WEB_RETRIES = 3  # 웹 검색이 일시적으로 실패할 때 재시도 횟수
DDG_MIN_INTERVAL = 1.0  # 초. DuckDuckGo 는 동시 요청이 몰리면 차단되므로 호출 간격을 둔다
WEB_DOMAIN = "researchnester.com"  # 웹 검색은 Research Nester 시장 보고서로 한정

# 입력 JSON 의 필드명이 확정되지 않아 흔한 이름들을 앞에서부터 찾는다.
NAME_FIELDS = ("name", "company", "company_name", "기업명", "회사명", "회사")
TOPIC_FIELDS = (
    "sector", "sub_domain", "세부분야", "분야", "category", "industry",
    "description", "one_liner", "business", "summary", "intro",
    "inferred_topic",  # 분야 필드가 없을 때 LLM 이 회사명·업종코드 등으로 추정한 값 (run 에서 채운다)
)
MAX_TOPIC_WORDS = 15  # 소개문처럼 긴 텍스트는 첫 문장에서 이 단어 수까지만 쓴다
# "○○는 XX 분야에서 '과제명'을 전개하는 기업입니다." 형태에서 과제명을 뽑는다.
TITLE_PATTERN = re.compile(r"분야에서\s*['‘“\"](.+?)['’”\"]")
LIST_KEYS = ("eligible_companies", "candidates", "startups", "companies", "results", "items", "data")

load_dotenv(PROJECT_ROOT / ".env")


def first_value(company, fields):
    for field in fields:
        value = company.get(field)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def get_name(company):
    return first_value(company, NAME_FIELDS) or "(이름 없음)"


def get_topic(company):
    """검색에 쓸 사업 분야 문구. 분야 정보가 없으면 빈 문자열(회사명으로는 검색하지 않는다).
    소개문(intro)처럼 긴 텍스트는 핵심 과제명이나 첫 문장으로 줄인다."""
    text = first_value(company, TOPIC_FIELDS)

    match = TITLE_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    words = re.split(r"(?<=[.!?다])\s+", text)[0].split()
    return " ".join(words[:MAX_TOPIC_WORDS])


def read_input(path):
    """JSON 파일을 읽어 (원본 데이터, 기업 목록이 든 키, 기업 목록)을 돌려준다.
    {"eligible_companies": [...], ...} 같은 상태 JSON 이면 key 가 그 이름이고, 리스트/기업 1개 dict 이면 key 는 None."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    key = None

    if isinstance(data, dict):
        key = next((k for k in LIST_KEYS if isinstance(data.get(k), list)), None)
        companies = data[key] if key else [data]
    else:
        companies = data

    return data, key, [item for item in companies if isinstance(item, dict)]


def load_companies(path):
    return read_input(path)[2]


def build_queries(company):
    """기업의 사업 분야 중심으로 질의를 만든다. (RAG 문서는 기업이 아니라 시장 자료라서 회사명은 넣지 않는다.)
    세부 단어("비리튬", "VPP" 등)가 많으면 임베딩이 문서 주제에서 멀어지므로,
    전체 문구와 뒤쪽 단어를 줄인 짧은 문구(최소 MIN_QUERY_WORDS 단어)를 모두 질의로 만든다."""
    words = get_topic(company).split()

    if not words:
        return []

    shortest = min(MIN_QUERY_WORDS, len(words))

    return [
        f"{' '.join(words[:n])} 시장 규모 성장 전망 수요"
        for n in range(len(words), shortest - 1, -1)
    ]


def web_queries(company):
    """Research Nester 는 기업이 아니라 시장 보고서를 다루므로 회사명 없이 세부 분야로 검색한다.
    결과가 없으면 뒤쪽 단어를 하나씩 줄여 다시 검색할 수 있도록 후보를 길이 순으로 만든다."""
    words = get_topic(company).split()

    return [f"{' '.join(words[:n])} 시장 규모" for n in range(len(words), 0, -1)]


def tavily_search(query, api_key):
    response = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "max_results": WEB_MAX_RESULTS,
            "include_domains": [WEB_DOMAIN],
        },
        timeout=30,
    )
    response.raise_for_status()

    return [
        {
            "text": result.get("content", ""),
            "title": result.get("title", ""),
            "url": result.get("url", ""),
        }
        for result in response.json().get("results", [])
    ]


_ddg_lock = threading.Lock()
_last_ddg_call = 0.0


def ddg_text(query, max_results):
    """DuckDuckGo 검색. 병렬 처리 중에도 호출이 한 번에 하나씩, 최소 간격을 두고 나가도록 한다."""
    global _last_ddg_call
    from ddgs import DDGS

    with _ddg_lock:
        wait = DDG_MIN_INTERVAL - (time.monotonic() - _last_ddg_call)

        if wait > 0:
            time.sleep(wait)

        try:
            return DDGS().text(query, max_results=max_results)
        finally:
            _last_ddg_call = time.monotonic()


def ddg_search(query):
    return [
        {
            "text": result.get("body", ""),
            "title": result.get("title", ""),
            "url": result.get("href", ""),
        }
        for result in ddg_text(f"{query} site:{WEB_DOMAIN}", WEB_MAX_RESULTS)
    ]


def web_search(query):
    """웹 검색(Research Nester 한정). TAVILY_API_KEY 가 있으면 Tavily, 없거나 실패(한도 초과 등)하면 DuckDuckGo 로 대체한다."""
    errors = []

    api_key = os.getenv("TAVILY_API_KEY")
    if api_key:
        try:
            items = tavily_search(query, api_key)
            if items:
                return items, None
        except requests.RequestException as error:
            errors.append(f"Tavily 실패: {error}")

    for attempt in range(WEB_RETRIES):  # DuckDuckGo 가 간헐적으로 "결과 없음"을 주므로 재시도한다
        try:
            items = ddg_search(query)
            if items:
                return items, None
        except Exception as error:
            if attempt == WEB_RETRIES - 1:
                errors.append(f"DuckDuckGo 실패: {error}")

        time.sleep(2)

    return [], " / ".join(errors) or "웹 검색 결과 없음"


class MarketSummary(BaseModel):
    target_market: str | None = Field(description="수치가 가리키는 시장(지역 포함). 예: 글로벌 데이터센터 냉각 시장")
    market_size: str | None = Field(description="시장 규모. 연도와 단위를 포함한 수치")
    growth: str | None = Field(description="성장성. 성장률(CAGR 등)과 기간, 빠르게 크는 세부 부문")
    demand_evidence: str | None = Field(description="수요 근거. 시장이 커지는 이유와 수요 요인")


SUMMARY_SYSTEM = """너는 시장 조사 자료를 정리하는 애널리스트다. 주어진 [자료]에 적힌 내용만 사용해서 항목을 채운다.
- 자료에 없는 내용은 절대 추측하거나 지어내지 말고 null 로 둔다. 수치와 단위, 연도는 자료 그대로 쓴다.
- market_size 와 growth 에는 자료에 적힌 구체적인 숫자(금액, 성장률)가 있을 때만 쓰고, 숫자가 없으면 설명문으로 채우지 말고 null 로 둔다.
- 자료에 여러 시장·지역 수치가 있으면 기업의 사업 분야에 가장 가까운 시장(글로벌 우선)을 기준으로 한다.
- 자료가 기업의 사업 분야와 무관한 시장 이야기뿐이면 모든 항목을 null 로 둔다. (문서 분야가 기업 분야와 세부 기술이 다르면, 예: 태양광(PV) 기업에 태양열 집광기(CSP) 자료, 자료가 다루는 시장을 target_market 에 분명히 적는다.)
- 한국어로 간결하게 쓴다. 각 항목은 1~2문장 이내."""


SIZE_PATTERN = re.compile(r"\d[\d,.]*\s*(억|조|만|천|백만|십억|달러|원|billion|million|trillion|USD|[TGM]W)|[$￦₩]\s*\d", re.I)
GROWTH_PATTERN = re.compile(r"\d[\d,.]*\s*(%|퍼센트|배)")


def summarize(client, model, topic, texts):
    """자료 텍스트들을 시장 규모 / 성장성 / 수요 근거로 정리한다."""
    materials = "\n\n".join(
        f"[자료 {index}]\n{text[:MAX_CONTEXT_CHARS]}"
        for index, text in enumerate(texts, start=1)
    )

    response = client.chat.completions.parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": f"기업의 사업 분야: {topic}\n\n{materials}"},
        ],
        response_format=MarketSummary,
    )

    summary = response.choices[0].message.parsed

    # 프롬프트로 막아도 어기는 경우가 있어서, 금액·비율 숫자가 없는 규모·성장성은 코드에서 null 로 바꾼다
    # ("자료 1에 구체적 수치 없음" 같은 설명문에 든 숫자에 속지 않도록 단위가 붙은 숫자만 인정한다)
    for field, pattern in (("market_size", SIZE_PATTERN), ("growth", GROWTH_PATTERN)):
        value = getattr(summary, field)

        if value and not pattern.search(value):
            setattr(summary, field, None)

    return summary


def has_content(summary):
    return summary is not None and any(
        [summary.market_size, summary.growth, summary.demand_evidence]
    )


class TopicGuess(BaseModel):
    topic: str | None = Field(description="시장 조사 검색어로 쓸 사업 분야. 한국어 명사구 2~5단어. 근거가 부족하면 null")
    subdomain: str | None = Field(description="영문 소분류 라벨. 예: 'solar / PV', 'immersion cooling', 'waste treatment'")
    country: str | None = Field(description="본사 소재국 ISO 2자리 코드. 주소가 한국이면 KR")
    description: str | None = Field(description="이 기업이 무엇을 하는 회사인지 한국어 1~2문장. 사업 분야 수준으로만 쓰고 제품명·실적 등 자료에 없는 세부는 지어내지 않는다")
    basis: str = Field(description="추정 근거 한 줄")


TOPIC_SYSTEM = """너는 기업의 사업 분야를 파악하는 애널리스트다. 회사명, 업종명(한국표준산업분류), 주소, 판정 사유로
이 기업의 사업 분야를 시장 조사 검색어로 쓸 짧은 한국어 명사구(2~5단어)로 추정한다. 예: "태양광 발전", "데이터센터 액침냉각".
- 업종코드 숫자의 뜻을 기억으로 추측하지 마라. 제공된 '업종명'만 근거로 쓰고, 업종명이 없으면 코드는 무시한다.
- 회사명에 구체적인 사업 분야 단어(예: 태양광, 액침냉각, 폐기물)가 들어 있으면 그 분야를 쓴다. '에너지', '그린', '에코', '테크'처럼 모호한 이름이면 업종명을 쓴다.
- topic 에 회사명을 그대로 쓰지 마라. 항상 사업 분야를 나타내는 명사구여야 한다.
- 회사명과 업종명이 서로 다르면 근거에 그 불일치를 적는다.
- 판정 사유에는 일반적인 문구가 많으니 참고만 한다. 근거가 부족하면 지어내지 말고 topic 을 null 로 둔다.
- subdomain, country, description 도 같은 근거로 채운다. description 은 추정임을 전제로, 근거 있는 사업 분야만 서술한다."""


_ksic_lock = threading.Lock()


def read_ksic_cache():
    try:
        return json.loads(KSIC_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ksic_name(code):
    """업종코드의 업종명을 웹에서 조회한다(결과는 캐시). 못 찾으면 None."""
    code = str(code).strip()

    with _ksic_lock:
        cached = read_ksic_cache().get(code)

    if cached:
        return cached

    patterns = (
        re.compile(rf"(?<!\d){code}(?!\d)\s*\(([^)]+)\)"),  # 74211 (건축물 일반 청소업)
        re.compile(rf"(?<!\d){code}(?!\d)\s*[:\-]?\s*([가-힣][가-힣 ·,]{{1,25}}업)"),  # 3511 발전업
    )
    queries = (f"한국표준산업분류 {code} 세세분류 업종", f"표준산업분류 {code}", f"KSIC {code}")

    for attempt in range(2):  # DuckDuckGo 가 간헐적으로 빈 결과를 주므로 한 번 더 시도한다
        for query in queries:
            try:
                results = ddg_text(query, 5)
            except Exception:
                continue

            for result in results:
                text = f"{result.get('title', '')} {result.get('body', '')}"

                for pattern in patterns:
                    match = pattern.search(text)

                    if match:
                        name = match.group(1).strip()

                        with _ksic_lock:
                            cache = read_ksic_cache()
                            cache[code] = name
                            KSIC_CACHE.write_text(
                                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                            )

                        return name

        time.sleep(2)

    return None


def describe_company(company):
    """분야 추정에 쓸 정보만 골라 텍스트로 만든다. (재무 등 나머지 필드는 넣지 않는다.)"""
    lines = [f"회사명: {get_name(company)}"]

    for key, value in company.items():
        if "industry" in key.lower() and isinstance(value, (str, int)):
            name = ksic_name(value) if str(value).strip().isdigit() else None
            lines.append(f"업종코드 {value}: " + (f"업종명 '{name}'" if name else "업종명 조회 실패(코드 뜻 모름)"))

    if company.get("address"):
        lines.append(f"주소: {company['address']}")

    reason = company.get("reason") or (company.get("screening") or {}).get("reason")
    if reason:
        lines.append(f"판정 사유: {reason}")

    return "\n".join(lines)


def infer_topic(client, model, company):
    response = client.chat.completions.parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": TOPIC_SYSTEM},
            {"role": "user", "content": describe_company(company)},
        ],
        response_format=TopicGuess,
    )

    return response.choices[0].message.parsed


class RelevanceCheck(BaseModel):
    matching_sub_domains: list[str] = Field(
        description="기업이 속한 시장을 직접 다루는 문서 분야. 후보 목록에 있는 문자열을 그대로 쓴다. 없으면 빈 리스트"
    )


RELEVANCE_SYSTEM = """너는 시장 조사 자료가 기업의 사업 분야에 쓸 만한지 판별한다.
기업의 사업 분야와 후보 문서 분야 목록이 주어지면, 그 기업이 속한 산업의 시장을 다루는 문서 분야를 고른다.
- 같은 산업 분야이면 포함한다. 세부 기술이 달라도 같은 산업의 시장 자료면 포함한다.
  예: 태양광 발전 기업에 태양열 집광기 문서는 포함(같은 태양에너지 산업), 배터리 ESS 기업에 전력망용 배터리 문서는 포함.
- 산업 분야 자체가 다를 때만 제외한다. 예: 태양광 기업에 풍력 터빈이나 폐기물, 청소업 문서는 제외.
- 후보 목록에 있는 문자열만 그대로 반환한다."""


def check_relevance(client, model, topic, sub_domains):
    """후보 문서 분야 중 기업 분야와 같은 시장인 것만 골라낸다."""
    response = client.chat.completions.parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": RELEVANCE_SYSTEM},
            {
                "role": "user",
                "content": f"기업의 사업 분야: {topic}\n후보 문서 분야:\n" + "\n".join(f"- {name}" for name in sub_domains),
            },
        ],
        response_format=RelevanceCheck,
    )

    return set(response.choices[0].message.parsed.matching_sub_domains)


class MarketAgent:
    def __init__(self, db_dir=DB_DIR, workers=MAX_WORKERS):
        self.workers = workers
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY 가 없습니다. 환경변수로 설정하거나 .env 에 넣어주세요."
            )

        self.llm = OpenAI()
        self.llm_model = os.getenv("MARKET_LLM_MODEL", LLM_MODEL)
        self.model = SentenceTransformer(MODEL_NAME)
        self.collection = chromadb.PersistentClient(path=str(db_dir)).get_collection(
            COLLECTION_NAME
        )

    def search_rag(self, queries_per_company):
        """기업별 질의 목록을 한꺼번에 임베딩해서 검색한다.
        기업마다 모든 질의의 결과를 합쳐 거리가 가까운 순으로 TOP_K 개를 남기고, 기준 거리 밖의 청크는 버린다."""
        queries = [query for group in queries_per_company for query in group]

        if not queries:
            return [[] for _ in queries_per_company]

        embeddings = self.model.encode(
            queries, normalize_embeddings=True, convert_to_numpy=True
        )
        results = self.collection.query(
            query_embeddings=embeddings.tolist(),
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )

        hits_per_company = []
        offset = 0

        for group in queries_per_company:
            best = {}

            for index in range(offset, offset + len(group)):
                for document, metadata, distance in zip(
                    results["documents"][index],
                    results["metadatas"][index],
                    results["distances"][index],
                ):
                    key = (metadata["source"], metadata["page"], metadata["chunk"])

                    if distance > MAX_DISTANCE:
                        continue

                    if key not in best or distance < best[key]["distance"]:
                        best[key] = {
                            "text": document,
                            "source": metadata["source"],
                            "page": metadata["page"],
                            "distance": round(distance, 4),
                            "query": queries[index],
                            "domain": metadata.get("domain"),
                            "sub_domain": metadata.get("sub_domain"),
                            "doc_type": metadata.get("doc_type"),
                        }

            offset += len(group)
            hits_per_company.append(
                sorted(best.values(), key=lambda hit: hit["distance"])[:TOP_K]
            )

        return hits_per_company

    def summarize_context(self, company, source, items):
        """자료를 정리해서 market_context 를 만든다. 원문은 붙이지 않고 출처만 남긴다."""
        try:
            # RAG 자료는 문서의 세부 분야를 함께 알려줘서, 기업 분야와 다른 시장 자료를 구분해 정리하게 한다
            texts = [
                f"(문서 분야: {item['sub_domain']}, 유형: {item['doc_type']})\n{item['text']}"
                if item.get("sub_domain")
                else item["text"]
                for item in items
            ]
            summary = summarize(self.llm, self.llm_model, get_topic(company), texts)
        except Exception as error:
            return None, f"요약 실패: {error}"

        if not has_content(summary):
            return None, None

        if source == "rag":
            sources = [
                {
                    "source": item["source"],
                    "page": item["page"],
                    "distance": item["distance"],
                    "sub_domain": item.get("sub_domain"),
                    "doc_type": item.get("doc_type"),
                }
                for item in items
            ]
        else:
            sources = [{"title": item["title"], "url": item["url"]} for item in items]

        return {"source": source, "summary": summary.model_dump(), "sources": sources}, None

    def prepare(self, company):
        """분야 필드가 없으면 LLM 으로 추정해서 검색용 dict 와 추정 정보를 만든다."""
        if get_topic(company):
            return company, None

        try:
            guess = infer_topic(self.llm, self.llm_model, company)
        except Exception:
            return company, None

        if not guess.topic:
            return company, None

        inferred = {
            "topic": guess.topic,
            "basis": guess.basis,
            "subdomain": guess.subdomain,
            "country": guess.country,
            "description": guess.description,
        }

        return {**company, "inferred_topic": guess.topic}, inferred

    def filter_relevant(self, work, hits):
        """RAG 청크 중 문서 분야가 기업 분야와 다른 기술·시장인 것을 걸러낸다. (걸러진 분야는 함께 돌려준다)
        판별에 실패하면 걸러내지 않고 그대로 쓴다."""
        sub_domains = sorted({hit["sub_domain"] for hit in hits if hit.get("sub_domain")})

        if not sub_domains:
            return hits, []

        try:
            matching = check_relevance(self.llm, self.llm_model, get_topic(work), sub_domains)
        except Exception:
            return hits, []

        kept = [hit for hit in hits if hit.get("sub_domain") in matching]
        rejected = [name for name in sub_domains if name not in matching]

        return kept, rejected

    def process(self, company, work, inferred, hits):
        """기업 1개를 처리한다: RAG 자료 정리 → 없으면 웹 검색 자료 정리. (병렬로 실행되는 단위)"""
        context, error, rejected = None, None, []

        try:
            if not build_queries(work):
                error = "분야 정보 없음 (검색 질의를 만들 수 없음)"
            else:
                if hits:
                    hits, rejected = self.filter_relevant(work, hits)

                if hits:
                    context, error = self.summarize_context(work, "rag", hits)

                if context is None:  # RAG 에 없거나, 있어도 분야와 무관해서 정리할 내용이 없으면 웹으로
                    for query in web_queries(work):
                        items, search_error = web_search(query)
                        if items:
                            context, error = self.summarize_context(work, "web", items)
                            break
                        error = error or search_error
        except Exception as exc:  # 한 기업의 실패가 다른 기업 처리를 막지 않게 한다
            error = f"처리 실패: {exc}"

        if context is None:
            context = {"source": "none", "summary": None, "sources": []}
            if error:
                context["error"] = error

        if rejected:  # 기업 분야와 다른 기술·시장이라 RAG 에서 제외한 문서 분야
            context = {**context, "rag_rejected": rejected}

        if inferred:  # 분야를 LLM 이 추정한 경우 다음 에이전트가 알 수 있게 남긴다
            context = {"inferred": inferred, **context}

        return {**company, "market_context": context}

    def run(self, companies):
        """기업 목록 각각에 시장 규모 / 성장성 / 수요 근거 요약(market_context)을 붙여서 반환한다.
        분야 추정과 요약·웹 검색은 기업별로 병렬 처리하고, RAG 검색은 질의를 모아 한 번에 한다. 입력 순서는 유지된다."""
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            prepared = list(pool.map(self.prepare, companies))
            rag_hits = self.search_rag([build_queries(work) for work, _ in prepared])

            return list(
                pool.map(
                    lambda args: self.process(*args),
                    [
                        (company, work, inferred, hits)
                        for company, (work, inferred), hits in zip(companies, prepared, rag_hits)
                    ],
                )
            )

    def run_folder(self, input_dir=INPUT_DIR, output_dir=OUTPUT_DIR, limit=None):
        """입력 폴더의 JSON 파일마다 시장 정보를 붙여 출력 폴더에 <파일명>_enriched.json 으로 저장한다.
        limit 가 있으면 파일마다 앞에서 limit 개 기업만 처리한다."""
        input_dir, output_dir = Path(input_dir), Path(output_dir)
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        results = {}
        for path in sorted(input_dir.glob("*.json")):
            data, key, companies = read_input(path)
            enriched = self.run(companies[:limit])

            # 상태 JSON({"eligible_companies": [...], ...})이면 다른 필드는 그대로 두고 기업 목록만 교체한다
            output = {**data, key: enriched} if key else enriched

            output_path = output_dir / f"{path.stem}_enriched.json"
            output_path.write_text(
                json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results[output_path] = enriched

        return results


def print_summary(enriched):
    for company in enriched:
        context = company["market_context"]
        print(
            f"  [{context['source']:>4}] {get_name(company)} ({len(context['sources'])}건 근거)"
            + (f" - {context['error']}" if context.get("error") else "")
        )


def main():
    parser = argparse.ArgumentParser(description="시장성 평가 에이전트")
    parser.add_argument(
        "--input-dir", type=Path, default=INPUT_DIR,
        help=f"스타트업 JSON 이 들어오는 폴더 (기본 {INPUT_DIR})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR,
        help=f"결과 JSON 저장 폴더 (기본 {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--limit", type=int, help="파일마다 앞에서 N개 기업만 처리",
    )
    parser.add_argument(
        "--workers", type=int, default=MAX_WORKERS, help=f"동시에 처리할 기업 수 (기본 {MAX_WORKERS})",
    )
    args = parser.parse_args()

    agent = MarketAgent(workers=args.workers)
    results = agent.run_folder(args.input_dir, args.output_dir, args.limit)

    if not results:
        print(f"처리할 JSON 파일이 없습니다. {args.input_dir} 에 넣어주세요.")
        return

    for output_path, enriched in results.items():
        print(f"{output_path.name} ({len(enriched)}개 기업)")
        print_summary(enriched)


if __name__ == "__main__":
    main()
