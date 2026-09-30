# 작성자: 이우성
# 파일 설명: 무작위 기업 식별자를 입력받아 기업 정보를 조회하고,
# DART 검색 결과를 JSON 형식으로 반환하며,
# 1) 외부 데이터 없이 오직 DART만으로 에너지 Seed~Series C 스타트업을 직접 탐색하는 파이프라인
# 2) 특정 기업명 즉시 검색 및 재무제표 JSON 반환
# 3) 공시자료 PDF 다운로드 및 pdfplumber 기반 재무제표(표) 추출을 담당합니다.
#    (텍스트 보조 로더는 PyPDFLoader를 fallback으로 유지합니다.)
# 추가 담당: 프로젝트 설계 산출물 작성

import os
import json
import asyncio
import zipfile
import io
import re
import random
import sys
import urllib.request
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional, Tuple
import aiohttp

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

try:
    from langchain_community.document_loaders import PyPDFLoader
except ImportError:  # pragma: no cover
    PyPDFLoader = None

# dart.py를 직접 실행해도 프로젝트 루트의 state.py를 찾을 수 있게 합니다.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import DEFAULT_COMPANY_COUNT, DART_FETCH_MULTIPLIER
from state import GraphState

# 경로 설정
COMPANIES_JSON_PATH = os.path.join(PROJECT_ROOT, "data", "companies.json")
CACHE_DIR = os.path.join(PROJECT_ROOT, "data")
CORP_CODE_CACHE_FILE = os.path.join(CACHE_DIR, "corp_codes_cache.json")
PDF_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "pdfs")


def load_dart_api_key() -> str:
    """환경 변수 및 .env 파일에서 DART API KEY를 가져옵니다."""
    key = os.getenv("DART_API_KEY") or os.getenv("dart_api_key")
    if key:
        return key.strip("\"' ")

    candidates = [
        os.path.join(PROJECT_ROOT, ".env"),
        os.path.join(PROJECT_ROOT, "env."),
        os.path.join(PROJECT_ROOT, "..", "dart", ".env"),
        os.path.join(PROJECT_ROOT, "..", "dart", "env."),
    ]
    for filepath in candidates:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("dart_api_key") or line.startswith(
                        "DART_API_KEY"
                    ):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            return parts[1].strip().strip("\"' ")

    raise ValueError(
        "DART API 키를 찾을 수 없습니다. .env 파일에 DART_API_KEY를 설정해 주세요."
    )


class AsyncDartService:
    """DART Open API와 비동기로 통신하는 서비스 클래스"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or load_dart_api_key()
        self.corp_map: Dict[str, str] = {}  # {회사명: 8자리 corp_code}
        self.semaphore = asyncio.Semaphore(20)  # DART API 동시 요청 수 제한

    async def init_corp_codes(self, session: aiohttp.ClientSession) -> None:
        """DART 전체 기업 고유번호(corp_code) 목록을 비동기로 로드 및 캐싱합니다."""
        if self.corp_map:
            return

        if os.path.exists(CORP_CODE_CACHE_FILE):
            with open(CORP_CODE_CACHE_FILE, "r", encoding="utf-8") as f:
                self.corp_map = json.load(f)
            return

        url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={self.api_key}"
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"DART 고유번호 다운로드 실패 (상태코드: {response.status})"
                )
            content = await response.read()

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                xml_data = z.read("CORPCODE.xml")
            root = ET.fromstring(xml_data)

            for item in root.findall("list"):
                corp_code = item.findtext("corp_code")
                corp_name = item.findtext("corp_name")
                if corp_code and corp_name:
                    clean_name = corp_name.strip()
                    self.corp_map[clean_name] = corp_code.strip()

                    # (주), 주식회사 등 접두/접미어 제거 버전도 인덱싱
                    simplified = (
                        clean_name.replace("(주)", "")
                        .replace("주식회사", "")
                        .replace(" ", "")
                        .strip()
                    )
                    if simplified and simplified not in self.corp_map:
                        self.corp_map[simplified] = corp_code.strip()

            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(CORP_CODE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.corp_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            raise RuntimeError(f"DART 고유번호 파싱 오류: {e}")

    def find_corp_code(self, company_name: str) -> Optional[str]:
        """기업명으로 DART 8자리 corp_code를 찾습니다."""
        if not company_name:
            return None
        clean_name = company_name.strip()
        if clean_name in self.corp_map:
            return self.corp_map[clean_name]

        simplified = (
            clean_name.replace("(주)", "").replace("주식회사", "").replace(" ", "")
        )
        return self.corp_map.get(simplified)

    async def get_company_overview(
        self, session: aiohttp.ClientSession, corp_code: str
    ) -> Dict[str, Any]:
        """기업 기본 개요 정보(company.json) 조회"""
        url = "https://opendart.fss.or.kr/api/company.json"
        params = {"crtfc_key": self.api_key, "corp_code": corp_code}
        async with self.semaphore:
            try:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}
        return {"status": "NO_RESPONSE"}

    async def get_financial_statements(
        self, session: aiohttp.ClientSession, corp_code: str, bsns_year: str = "2023"
    ) -> Dict[str, Any]:
        """단일회사 전체 재무제표(fnlttSinglAcntAll.json) 조회 (최근 연도 자동 탐색)"""
        url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
        years_to_try = [bsns_year, "2022", "2024"]

        async with self.semaphore:
            for yr in years_to_try:
                for fs_div in ["OFS", "CFS"]:
                    params = {
                        "crtfc_key": self.api_key,
                        "corp_code": corp_code,
                        "bsns_year": yr,
                        "reprt_code": "11011",  # 사업보고서
                        "fs_div": fs_div,
                    }
                    try:
                        async with session.get(
                            url, params=params, timeout=aiohttp.ClientTimeout(total=8)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json(content_type=None)
                                if data.get("status") == "000":
                                    data["matched_year"] = yr
                                    return data
                    except Exception:
                        pass
        return {"status": "013", "message": "조회된 정기보고서 재무제표가 없습니다."}

    async def get_audit_reports(
        self, session: aiohttp.ClientSession, corp_code: str, bgn_de: str = "20200101"
    ) -> Dict[str, Any]:
        """감사보고서 등 공시 목록(list.json) 조회"""
        url = "https://opendart.fss.or.kr/api/list.json"
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "pblntf_detail_ty": "F001",  # 감사보고서
            "page_count": "10",
        }
        async with self.semaphore:
            try:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}
        return {"status": "NO_RESPONSE"}

    async def extract_financials_from_audit_doc(
        self, session: aiohttp.ClientSession, rcept_no: str
    ) -> Dict[str, Any]:
        """비상장 스타트업 감사보고서 원문(document.xml)에서 재무 지표 추출"""
        url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={self.api_key}&rcept_no={rcept_no}"
        async with self.semaphore:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return empty_financial_summary()
                    content = await resp.read()

                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    xml_files = [f for f in z.namelist() if f.endswith(".xml")]
                    if not xml_files:
                        return empty_financial_summary()
                    raw_xml = z.read(xml_files[0]).decode("utf-8", errors="ignore")

                clean_text = re.sub(r"<[^>]+>", " ", raw_xml)
                clean_text = re.sub(r"&nbsp;", " ", clean_text)
                clean_text = re.sub(r"\s+", " ", clean_text)
                return extract_financials_from_text(clean_text)
            except Exception:
                return empty_financial_summary()


# DART-style Korean keys are primary; normalized English keys are also allowed.
FINANCIAL_KEYS = {
    "assets": ("자산총계", "total_assets", "자산 총계"),
    "liabilities": ("부채총계", "total_liabilities", "부채 총계"),
    "equity": ("자본총계", "total_equity", "자본 총계"),
    "revenue": ("매출액", "revenue", "수익(매출액)", "영업수익", "영업 수익"),
    "operating_profit": (
        "영업이익",
        "operating_income",
        "영업이익(손실)",
        "영업이익손실",
    ),
    "net_income": ("당기순이익", "net_income", "당기순이익(손실)", "당기순손실"),
    "tax_expense": ("법인세비용", "income_tax_expense", "법인세등", "법인세 비용"),
    "cash": (
        "현금및현금성자산",
        "cash_and_cash_equivalents",
        "현금및현금성자산등",
        "현금및현금성자산(주석",
    ),
    "debt": (
        "이자부부채",
        "이자부채",
        "interest_bearing_debt",
        "차입금",
        "단기차입금",
        "장기차입금",
        "유동성장기차입금",
        "유동성장기부채",
        "사채",
    ),
    "interest_expense": ("이자비용", "interest_expense", "이자 비용"),
    "operating_cf": (
        "영업활동현금흐름",
        "operating_cash_flow",
        "영업활동으로인한현금흐름",
        "영업활동으로 인한 현금흐름",
        "영업활동으로인한현금흐름(간접법)",
    ),
    "capex": (
        "CAPEX",
        "설비투자",
        "유형자산의취득",
        "유형자산의 취득",
        "유형자산취득",
        "capital_expenditure",
    ),
    "current_assets": ("유동자산", "current_assets"),
    "current_liabilities": ("유동부채", "current_liabilities"),
}

# 이자부부채로 합산할 세부 계정 (PDF에 '이자부부채' 한 줄이 없을 때 사용)
DEBT_COMPONENT_ALIASES = (
    "단기차입금",
    "장기차입금",
    "유동성장기차입금",
    "유동성장기부채",
    "사채",
    "전환사채",
    "차입금",
)

# 계정명 매칭용: 한글 1차 키 → 허용 별칭(공백·영문 포함)
_ACCOUNT_ALIASES: Dict[str, tuple[str, ...]] = {
    aliases[0]: aliases for aliases in FINANCIAL_KEYS.values()
}


def empty_financial_summary() -> Dict[str, Any]:
    """한글 1차 키 기준으로 빈 재무 요약 객체를 만듭니다."""

    return {aliases[0]: None for aliases in FINANCIAL_KEYS.values()}


def financial_summary_normalized(summary: Dict[str, Any]) -> Dict[str, Any]:
    """한글 financial_summary를 영문 키로도 읽을 수 있게 변환합니다."""

    return {
        eng_key: summary.get(aliases[0]) for eng_key, aliases in FINANCIAL_KEYS.items()
    }


def _normalize_account_name(name: str) -> str:
    return re.sub(r"[\s\*※·ㆍ\.]", "", str(name or "")).lower()


def _parse_amount_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    clean = (
        text.replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("원", "")
        .strip()
    )
    clean = re.sub(r"[^\d\-]", "", clean)
    if not clean or clean == "-":
        return None
    try:
        amount = int(clean)
        return -amount if negative and amount > 0 else amount
    except ValueError:
        return None


def has_any_financial_value(summary: Optional[Dict[str, Any]]) -> bool:
    if not summary:
        return False
    return any(value not in {None, "", "-"} for value in summary.values())


def missing_financial_keys(summary: Optional[Dict[str, Any]]) -> list[str]:
    summary = summary or {}
    return [
        aliases[0]
        for aliases in FINANCIAL_KEYS.values()
        if summary.get(aliases[0]) in {None, "", "-"}
    ]


def merge_financial_summaries(*summaries: Dict[str, Any]) -> Dict[str, Any]:
    """앞선 값이 비어 있을 때만 뒤 요약으로 채웁니다."""

    merged = empty_financial_summary()
    for summary in summaries:
        if not summary:
            continue
        for eng_key, aliases in FINANCIAL_KEYS.items():
            primary = aliases[0]
            if merged.get(primary) not in {None, "", "-"}:
                continue
            # 한글 키 또는 영문 키/별칭으로 들어온 값도 수용합니다.
            value = summary.get(primary)
            if value in {None, "", "-"}:
                for alias in aliases:
                    if summary.get(alias) not in {None, "", "-"}:
                        value = summary.get(alias)
                        break
            if value not in {None, "", "-"}:
                merged[primary] = value
    return merged


def _spaced_label_pattern(label: str) -> str:
    """'자산총계' → '자\\s*산\\s*총\\s*계' 형태로 변환합니다."""

    chars = [re.escape(ch) for ch in label if not ch.isspace()]
    return r"\s*".join(chars)


def _build_alias_lookup() -> Dict[str, str]:
    """정규화된 계정명 → FINANCIAL_KEYS 한글 1차 키."""

    lookup: Dict[str, str] = {}
    for primary, aliases in _ACCOUNT_ALIASES.items():
        for alias in aliases:
            if re.fullmatch(r"[a-z_]+", alias):
                continue
            lookup[_normalize_account_name(alias)] = primary
        lookup[_normalize_account_name(f"{primary}(손실)")] = primary
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()
_DEBT_COMPONENT_NORMS = {
    _normalize_account_name(name) for name in DEBT_COMPONENT_ALIASES
}


def _format_amount(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:,}"


def _split_label_and_amounts(line: str) -> Tuple[str, List[Optional[str]]]:
    """재무제표 한 줄에서 계정명과 금액(당기/전기)을 분리합니다.

    `-` 단독은 해당 연도 값 없음을 의미합니다.
    예: ``유형자산의 취득 (8,333,032) -`` → 당기만
         ``건설중인자산의 증가 - (1,728,350,471)`` → 전기만
    """

    text = re.sub(r"\s+", " ", str(line or "")).strip()
    if not text:
        return "", []

    # 줄 끝의 당기/전기 토큰: 금액 또는 '-'
    token_re = re.compile(
        r"(\([0-9,]+\)|[0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?|[0-9]{5,}|(?<![0-9])-(?![0-9]))"
    )
    tokens = list(token_re.finditer(text))
    if not tokens:
        return text, []

    trailing: List[re.Match[str]] = []
    cursor = len(text)
    for match in reversed(tokens):
        gap = text[match.end() : cursor]
        if gap.strip() and not re.fullmatch(r"[\s\|]*", gap):
            break
        trailing.append(match)
        cursor = match.start()
    trailing.reverse()
    if not trailing:
        return text, []

    # 계정명 뒤에 붙는 최근 1~2개 토큰만 사용
    trailing = trailing[-2:]
    label = text[: trailing[0].start()].strip(" :-|·ㆍ.")
    values: List[Optional[str]] = []
    for match in trailing:
        token = match.group(1)
        values.append(None if token == "-" else token)
    return label, values


def _strip_account_prefix(label: str) -> str:
    text = re.sub(r"^[Ⅰ-ⅩIVXivx\d\.\-\(\)\s]+", "", str(label or ""))
    text = re.sub(r"\(주석[^)]*\)", "", text)
    return text.strip()


def _match_primary_account(label: str) -> Optional[str]:
    normalized = _normalize_account_name(_strip_account_prefix(label))
    if not normalized:
        return None
    if normalized in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[normalized]
    candidates: List[Tuple[int, str]] = []
    for alias_norm, primary in _ALIAS_LOOKUP.items():
        if not alias_norm or alias_norm not in normalized:
            continue
        remainder = normalized.replace(alias_norm, "", 1)
        # '법인세비용차감전순이익'이 '법인세비용'으로 잡히지 않게 합니다.
        if any(token in remainder for token in ("차감전", "전이익", "비용차감")):
            continue
        if len(alias_norm) >= 4:
            candidates.append((len(alias_norm), primary))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _is_debt_component(label: str) -> bool:
    normalized = _normalize_account_name(label)
    if normalized in _DEBT_COMPONENT_NORMS:
        return True
    return any(token in normalized for token in _DEBT_COMPONENT_NORMS)


def _detect_statement_years(text: str) -> Tuple[Optional[str], Optional[str]]:
    """본문에서 당기/전기 연도를 추정합니다. (당기, 전기)"""

    years = re.findall(r"(20\d{2})\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일", text)
    uniq: List[str] = []
    for year in years:
        if year not in uniq:
            uniq.append(year)
    if len(uniq) >= 2:
        # 보통 당기 연도가 먼저 나옵니다.
        return uniq[0], uniq[1]
    if len(uniq) == 1:
        current = int(uniq[0])
        return uniq[0], str(current - 1)
    return None, None


def extract_financials_from_text(text: str) -> Dict[str, Any]:
    """감사보고서 텍스트에서 FINANCIAL_KEYS(당기) 값을 추출합니다."""

    statements = extract_financial_statements_from_text(text)
    if not statements:
        return empty_financial_summary()
    # 최신연도(목록 마지막)를 대표 summary로 사용합니다.
    latest = dict(statements[-1])
    latest.pop("year", None)
    return merge_financial_summaries(latest)


def extract_financial_statements_from_text(text: str) -> List[Dict[str, Any]]:
    """텍스트에서 당기/전기 재무 행을 연도별 리스트로 추출합니다."""

    if not text:
        return []

    current_year, prior_year = _detect_statement_years(text)
    current: Dict[str, Any] = empty_financial_summary()
    prior: Dict[str, Any] = empty_financial_summary()
    current_debt_specific: List[int] = []
    prior_debt_specific: List[int] = []
    current_debt_generic: Optional[int] = None
    prior_debt_generic: Optional[int] = None

    for raw_line in text.splitlines():
        label, amounts = _split_label_and_amounts(raw_line)
        if not label or not amounts:
            continue
        primary = _match_primary_account(label)
        if len(amounts) == 1:
            current_amount = _parse_amount_to_int(amounts[0])
            prior_amount = None
        else:
            current_amount = _parse_amount_to_int(amounts[0])
            prior_amount = _parse_amount_to_int(amounts[1])
        norm = _normalize_account_name(_strip_account_prefix(label))

        # 현금흐름표의 차입금 증가/상환·주석 세부표는 이자부부채 합산에서 제외합니다.
        if any(token in norm for token in ("증가", "감소", "상환", "유입", "유출")):
            debt_line = False
        else:
            debt_line = norm in {
                "단기차입금",
                "장기차입금",
                "유동성장기차입금",
                "유동성장기부채",
                "사채",
                "전환사채",
                "차입금",
                "이자부부채",
                "이자부채",
            }

        if (
            debt_line
            and current_amount is not None
            and norm
            in {
                "단기차입금",
                "장기차입금",
                "유동성장기차입금",
                "유동성장기부채",
                "사채",
                "전환사채",
            }
        ):
            current_debt_specific.append(current_amount)
            if prior_amount is not None:
                prior_debt_specific.append(prior_amount)
        elif (
            debt_line
            and current_amount is not None
            and norm in {"차입금", "이자부부채", "이자부채"}
        ):
            if current_debt_generic is None:
                current_debt_generic = current_amount
            if prior_amount is not None and prior_debt_generic is None:
                prior_debt_generic = prior_amount

        if not primary or primary == "이자부부채":
            continue
        if current.get(primary) in {None, "", "-"} and current_amount is not None:
            current[primary] = _format_amount(current_amount)
        if prior.get(primary) in {None, "", "-"} and prior_amount is not None:
            prior[primary] = _format_amount(prior_amount)

    if current_debt_specific:
        current["이자부부채"] = _format_amount(sum(current_debt_specific))
    elif current_debt_generic is not None:
        current["이자부부채"] = _format_amount(current_debt_generic)
    if prior_debt_specific:
        prior["이자부부채"] = _format_amount(sum(prior_debt_specific))
    elif prior_debt_generic is not None:
        prior["이자부부채"] = _format_amount(prior_debt_generic)

    statements: List[Dict[str, Any]] = []
    if has_any_financial_value(prior):
        prior_row = dict(prior)
        prior_row["year"] = prior_year
        statements.append(prior_row)
    if has_any_financial_value(current):
        current_row = dict(current)
        current_row["year"] = current_year
        statements.append(current_row)
    return statements


def extract_financials_from_text_legacy_regex(text: str) -> Dict[str, Any]:
    """정규식 보조 추출기. 라인 파서가 놓친 키만 채울 때 사용합니다."""

    extracted = empty_financial_summary()
    if not text:
        return extracted

    clean_text = re.sub(r"\s+", " ", text)
    amount = r"([0-9,]{2,}|\([0-9,]{2,}\))"

    for aliases in FINANCIAL_KEYS.values():
        primary = aliases[0]
        for alias in aliases:
            if re.fullmatch(r"[a-z_]+", alias):
                continue
            label = _spaced_label_pattern(alias)
            patterns = [
                rf"(?:[Ⅰ-ⅩIVXivx\d\.\-\(\)\s]*)(?:{label})(?:\s*\(주석[^\)]*\))?\s*{amount}",
                rf"(?:{label})\s*{amount}",
            ]
            for pattern in patterns:
                match = re.search(pattern, clean_text, re.IGNORECASE)
                if match:
                    extracted[primary] = match.group(1).strip()
                    break
            if extracted[primary] not in {None, "", "-"}:
                break
    return extracted


def _extract_pdf_text_with_pdfplumber(pdf_path: str) -> Tuple[str, int]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber가 설치되어 있지 않습니다.")
    pages: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages), len(pages)


def _extract_pdf_text_with_pypdf(pdf_path: str) -> Tuple[str, int]:
    if PyPDFLoader is None:
        raise RuntimeError("PyPDFLoader를 사용할 수 없습니다.")
    docs = PyPDFLoader(pdf_path).load()
    text = "\n".join(str(getattr(doc, "page_content", "") or "") for doc in docs)
    return text, len(docs)


def extract_financial_payload_from_pdf_file(pdf_path: str) -> Dict[str, Any]:
    """로컬 PDF에서 재무 summary + 연도별 financials를 추출합니다."""

    errors: List[str] = []
    text = ""
    total_pages = 0
    loader_used = "none"

    if pdfplumber is not None:
        try:
            text, total_pages = _extract_pdf_text_with_pdfplumber(pdf_path)
            loader_used = "pdfplumber"
        except Exception as error:
            errors.append(f"pdfplumber: {error}")
    if not text:
        try:
            text, total_pages = _extract_pdf_text_with_pypdf(pdf_path)
            loader_used = "pypdf"
        except Exception as error:
            errors.append(f"pypdf: {error}")

    statements = extract_financial_statements_from_text(text)
    summary = empty_financial_summary()
    if statements:
        latest = dict(statements[-1])
        latest.pop("year", None)
        summary = merge_financial_summaries(latest)
    # 라인 파서가 놓친 키는 정규식으로 한 번 더 보완합니다.
    summary = merge_financial_summaries(
        summary, extract_financials_from_text_legacy_regex(text)
    )
    if statements:
        statements[-1] = {
            **statements[-1],
            **{
                key: summary.get(key)
                for key in empty_financial_summary().keys()
                if summary.get(key) not in {None, "", "-"}
            },
        }

    return {
        "pdf_path": pdf_path,
        "total_pages": total_pages,
        "financial_summary": summary,
        "financials": statements,
        "loader": loader_used,
        "text_preview": text[:1500],
        "extract_errors": errors,
    }


def _extract_financial_summary_from_list(
    financial_items: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """정기 재무제표 계정 리스트에서 FINANCIAL_KEYS 지표를 추출합니다."""

    summary = empty_financial_summary()
    if not financial_items or not isinstance(financial_items, list):
        return summary

    alias_lookup: Dict[str, str] = {}
    for primary, aliases in _ACCOUNT_ALIASES.items():
        for alias in aliases:
            alias_lookup[_normalize_account_name(alias)] = primary
        # 괄호 손실 표기까지 허용합니다.
        alias_lookup[_normalize_account_name(f"{primary}(손실)")] = primary

    for item in financial_items:
        account_name = str(item.get("account_nm") or "").strip()
        normalized = _normalize_account_name(account_name)
        primary = alias_lookup.get(normalized)
        if not primary:
            # 부분 일치: '유형자산의취득' ⊂ '현금흐름표-유형자산의취득'
            for alias_norm, mapped in alias_lookup.items():
                if alias_norm and alias_norm in normalized:
                    primary = mapped
                    break
        if not primary or summary.get(primary) not in {None, "", "-"}:
            continue
        amount = item.get("thstrm_amount")
        if amount not in {None, "", "-"}:
            summary[primary] = amount
    return summary


def build_overview_payload(overview: Dict[str, Any]) -> Dict[str, Any]:
    """DART company.json에서 RAG/스크리닝에 쓸 만한 필드를 정리합니다."""

    corp_cls = overview.get("corp_cls")
    legal_class = {
        "Y": "유가증권시장",
        "K": "코스닥",
        "N": "코넥스 상장벤처",
        "E": "비상장 외감기업",
    }.get(corp_cls, corp_cls)

    return {
        "company_name": overview.get("corp_name"),
        "company_name_eng": overview.get("corp_name_eng"),
        "corp_code": overview.get("corp_code"),
        "stock_code": overview.get("stock_code") or None,
        "stock_name": overview.get("stock_name") or None,
        "ceo": overview.get("ceo_nm"),
        "corp_cls": corp_cls,
        "legal_class": legal_class,
        "jurir_no": overview.get("jurir_no"),
        "bizr_no": overview.get("bizr_no"),
        "address": overview.get("adres"),
        "homepage_url": overview.get("hm_url") or None,
        "ir_url": overview.get("ir_url") or None,
        "phone": overview.get("phn_no") or None,
        "fax": overview.get("fax_no") or None,
        "industry_code": overview.get("induty_code"),
        "established_date": overview.get("est_dt"),
        "acc_month": overview.get("acc_mt"),
    }


def build_latest_report_payload(
    audit_items: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    if not audit_items:
        return {}
    latest = audit_items[0]
    rcept_no = latest.get("rcept_no")
    return {
        "rcept_no": rcept_no,
        "report_nm": latest.get("report_nm"),
        "rcept_dt": latest.get("rcept_dt"),
        "rm": latest.get("rm"),
        "dart_viewer_link": (
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            if rcept_no
            else None
        ),
    }


def estimate_investment_stage(assets_amount: Optional[int]) -> str:
    """자산 규모로 투자 단계를 추정합니다. 확정 Series가 아니라 fallback 라벨입니다."""

    if not assets_amount:
        return "자산추정 Seed~Pre-A"
    if assets_amount < 5_000_000_000:
        return "자산추정 Seed~Pre-A"
    if assets_amount < 20_000_000_000:
        return "자산추정 Series A~B"
    if assets_amount <= 50_000_000_000:
        return "자산추정 Series C"
    return "자산추정 Series D 이상"


# ===========================================================================
# [신규 핵심] 오직 DART만으로 에너지 Seed~Series C 스타트업을 직접 탐색하는 파이프라인
# ===========================================================================
async def search_energy_startups_dart_only(
    limit: int = 5,
    min_est_year: str = "2015",
    max_assets_krw: int = 50_000_000_000,
    keywords: Optional[List[str]] = None,
    shuffle: bool = True,
    exclude_corp_codes: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    외부 파일(companies.json 등) 없이 오직 DART 고유번호와 DART API만을 활용하여,
    1) 에너지 관련 키워드(에너지, 배터리, 수소, 신재생 등) 법인 1차 필터링
    2) shuffle=True 설정 시 후보군을 무작위로 섞어 매번 새로운 기업 5개를 랜덤 발굴
    3) 비상장(E)/코넥스(N) 및 설립 10년 이내(2015년 이후) 스타트업 검증
    4) DART 재무제표 조회 후 자산 500억 이하(Seed~Series C 단계) 검증
    5) 유효한 재무제표가 있는 5개 기업이 모두 채워질 때까지 반복 루프 수행
    """
    target_keywords = keywords or [
        "에너지",
        "배터리",
        "수소",
        "솔라",
        "신재생",
        "전력",
        "ess",
        "태양광",
    ]
    excluded = exclude_corp_codes or set()
    service = AsyncDartService()
    recommended_startups: List[Dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        await service.init_corp_codes(session)

        # 1. DART 캐시에서 에너지 키워드 기업 1차 선별 (고유번호 기준 중복 제거)
        seen_codes = set()
        candidate_corps: List[tuple[str, str]] = []
        for name, code in service.corp_map.items():
            if code in seen_codes or code in excluded:
                continue
            if any(kw in name for kw in target_keywords):
                seen_codes.add(code)
                candidate_corps.append((name, code))

        # [랜덤 셔플] 매번 다른 새로운 기업 5개를 찾도록 무작위 순서로 섞음
        if shuffle:
            random.shuffle(candidate_corps)
            print(
                f"  🎲 DART 에너지 법인 {len(candidate_corps)}개를 무작위(Random)로 섞어 탐색 루프를 시작합니다..."
            )
        else:
            print(
                f"  🔍 DART 내 에너지 관련 법인 {len(candidate_corps)}개 발견. Seed~Series C 필터링 시작..."
            )

        # 2. 비동기 배치 검사 (20개씩 묶어서 개요·재무·감사를 병렬 조회)
        batch_size = 20
        print(f"  ⚡ DART 병렬 탐색: 목표 {limit}개, 배치 {batch_size}개씩 동시 조회")
        for i in range(0, len(candidate_corps), batch_size):
            batch = candidate_corps[i : i + batch_size]
            overview_tasks = [
                service.get_company_overview(session, code) for _, code in batch
            ]
            overviews = await asyncio.gather(*overview_tasks, return_exceptions=True)
            overviews = [
                (
                    item
                    if isinstance(item, dict)
                    else {"status": "ERROR", "message": str(item)}
                )
                for item in overviews
            ]

            eligible_batch: List[tuple[str, str, Dict[str, Any]]] = []
            for (company_name, corp_code), overview in zip(batch, overviews):
                if overview.get("status") != "000":
                    continue
                corp_cls = overview.get("corp_cls")
                if corp_cls not in ["E", "N"]:
                    continue
                est_dt = overview.get("est_dt", "19000101")
                if est_dt < f"{min_est_year}0101":
                    continue
                eligible_batch.append((company_name, corp_code, overview))

            if not eligible_batch:
                if len(recommended_startups) >= limit:
                    break
                continue

            fin_results, audit_results = await asyncio.gather(
                asyncio.gather(
                    *[
                        service.get_financial_statements(session, code)
                        for _, code, _ in eligible_batch
                    ],
                    return_exceptions=True,
                ),
                asyncio.gather(
                    *[
                        service.get_audit_reports(session, code)
                        for _, code, _ in eligible_batch
                    ],
                    return_exceptions=True,
                ),
            )
            fin_results = [
                (
                    item
                    if isinstance(item, dict)
                    else {"status": "ERROR", "message": str(item)}
                )
                for item in fin_results
            ]
            audit_results = [
                (
                    item
                    if isinstance(item, dict)
                    else {"status": "ERROR", "message": str(item)}
                )
                for item in audit_results
            ]

            audit_doc_tasks = []
            audit_doc_indexes = []
            parsed_rows = []
            for index, (
                (company_name, corp_code, overview),
                financials,
                audits,
            ) in enumerate(zip(eligible_batch, fin_results, audit_results)):
                fin_items = (
                    financials.get("list")
                    if financials.get("status") == "000"
                    else None
                )
                audit_items = (
                    audits.get("list") if audits.get("status") == "000" else []
                )
                fin_summary = _extract_financial_summary_from_list(fin_items)
                source_type = (
                    "정기보고서(사업보고서)"
                    if has_any_financial_value(fin_summary)
                    else "없음"
                )
                parsed_rows.append(
                    {
                        "company_name": company_name,
                        "corp_code": corp_code,
                        "overview": overview,
                        "audit_items": audit_items,
                        "fin_summary": fin_summary,
                        "source_type": source_type,
                    }
                )
                # API 요약에 핵심 키가 비어 있으면 감사보고서 원문으로 보완합니다.
                if missing_financial_keys(fin_summary) and audit_items:
                    latest_rcept_no = audit_items[0].get("rcept_no")
                    if latest_rcept_no:
                        audit_doc_indexes.append(index)
                        audit_doc_tasks.append(
                            service.extract_financials_from_audit_doc(
                                session, latest_rcept_no
                            )
                        )

            if audit_doc_tasks:
                doc_financials_list = await asyncio.gather(
                    *audit_doc_tasks,
                    return_exceptions=True,
                )
                for row_index, doc_financials in zip(
                    audit_doc_indexes, doc_financials_list
                ):
                    if isinstance(doc_financials, Exception) or not doc_financials:
                        continue
                    before = parsed_rows[row_index]["fin_summary"]
                    merged = merge_financial_summaries(before, doc_financials)
                    parsed_rows[row_index]["fin_summary"] = merged
                    if has_any_financial_value(doc_financials):
                        report_nm = parsed_rows[row_index]["audit_items"][0].get(
                            "report_nm"
                        )
                        previous = parsed_rows[row_index]["source_type"]
                        if previous == "없음":
                            parsed_rows[row_index][
                                "source_type"
                            ] = f"감사보고서 원문({report_nm})"
                        else:
                            parsed_rows[row_index][
                                "source_type"
                            ] = f"{previous}+감사원문"

            for row in parsed_rows:
                overview = row["overview"]
                fin_summary = row["fin_summary"]
                audit_items = row["audit_items"]
                company_name = row["company_name"]
                corp_code = row["corp_code"]

                assets_int = _parse_amount_to_int(fin_summary.get("자산총계"))
                if assets_int is not None and assets_int > max_assets_krw:
                    continue

                if has_any_financial_value(fin_summary) or audit_items:
                    # overview에 corp_code가 없을 수 있어 탐색 시점 값을 보강합니다.
                    overview_with_code = {
                        **overview,
                        "corp_code": overview.get("corp_code") or corp_code,
                        "corp_name": overview.get("corp_name") or company_name,
                    }
                    company_data = build_company_dart_payload(
                        overview=overview_with_code,
                        fin_summary=fin_summary,
                        financial_source=row["source_type"],
                        audit_items=audit_items,
                        assets_int=assets_int,
                    )
                    recommended_startups.append(company_data)
                    print(
                        f"    ✔ 에너지 스타트업 발굴 성공: {company_name} "
                        f"(단계: {company_data['asset_estimated_stage']}, "
                        f"자산: {fin_summary.get('자산총계', '확인중')})"
                    )

                if len(recommended_startups) >= limit:
                    break

            if len(recommended_startups) >= limit:
                break

    # 최종 후보에 대해 공시 PDF로 FINANCIAL_KEYS를 최대한 채웁니다.
    if recommended_startups:
        print(
            f"  📄 공시 PDF로 FINANCIAL_KEYS 보완 중... ({len(recommended_startups)}개)"
        )
        recommended_startups = await enrich_companies_with_pdf(recommended_startups)
        # PDF로 자산이 뒤늦게 채워지면 Series D 이상(500억 초과)을 다시 걸러냅니다.
        filtered: List[Dict[str, Any]] = []
        for company in recommended_startups:
            assets_int = _parse_amount_to_int(
                (company.get("financial_summary") or {}).get("자산총계")
            )
            stage_label = estimate_investment_stage(assets_int)
            if assets_int is not None:
                company["asset_estimated_stage"] = stage_label
                if company.get("stage_source") in {None, "asset_estimate"}:
                    company["estimated_investment_stage"] = stage_label
            if assets_int is not None and assets_int > max_assets_krw:
                print(
                    f"    ✖ PDF 보완 후 자산 초과로 제외: {company.get('company_name')} "
                    f"(단계: {stage_label}, 자산: "
                    f"{(company.get('financial_summary') or {}).get('자산총계')})"
                )
                continue
            filtered.append(company)
            filled = len(FINANCIAL_KEYS) - len(
                missing_financial_keys(company.get("financial_summary"))
            )
            print(
                f"    · {company.get('company_name')}: "
                f"재무키 {filled}/{len(FINANCIAL_KEYS)}개, "
                f"출처={company.get('financial_source')}, "
                f"pdf={company.get('pdf_path') or company.get('pdf_error') or '없음'}"
            )
        recommended_startups = filtered

    return recommended_startups


# ===========================================================================
# [PDF 다운로드 및 pdfplumber 재무제표 추출]
# ===========================================================================
def parse_rcp_and_dcm(
    link_or_url: str, default_dcm: Optional[str] = None
) -> tuple[str, str]:
    m_rcp = re.search(r"rcp_?no=([0-9]+)", link_or_url, re.IGNORECASE)
    m_dcm = re.search(r"dcm_?no=([0-9]+)", link_or_url, re.IGNORECASE)

    rcp_no = m_rcp.group(1) if m_rcp else link_or_url.strip()
    dcm_no = m_dcm.group(1) if m_dcm else default_dcm

    if not dcm_no:
        viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
        req = urllib.request.Request(viewer_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m_btn = re.search(
            r"openPdfDownload\s*\(\s*['\"]([0-9]+)['\"]\s*,\s*['\"]([0-9]+)['\"]\s*\)",
            html,
        )
        if m_btn:
            rcp_no, dcm_no = m_btn.group(1), m_btn.group(2)
        else:
            m_dcm2 = re.search(r"dcmNo[\'\"]?\s*[:=]\s*[\'\"]?([0-9]+)", html)
            if m_dcm2:
                dcm_no = m_dcm2.group(1)

    if not rcp_no or not dcm_no:
        raise ValueError(f"rcp_no 또는 dcm_no를 추출할 수 없습니다: {link_or_url}")

    return rcp_no, dcm_no


def download_dart_pdf(
    link_or_url: str, dcm_no: Optional[str] = None, output_dir: str = PDF_OUTPUT_DIR
) -> str:
    os.makedirs(output_dir, exist_ok=True)
    rcp_no, dcm_no = parse_rcp_and_dcm(link_or_url, dcm_no)

    pdf_download_url = (
        f"https://dart.fss.or.kr/pdf/download/pdf.do?rcp_no={rcp_no}&dcm_no={dcm_no}"
    )
    referer_url = (
        f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcp_no}&dcm_no={dcm_no}"
    )
    output_path = os.path.join(output_dir, f"dart_{rcp_no}_{dcm_no}.pdf")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer_url,
    }

    req = urllib.request.Request(pdf_download_url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        content = resp.read()

    if len(content) == 0:
        raise RuntimeError(f"PDF 다운로드 실패 (0 바이트 수신): {pdf_download_url}")

    with open(output_path, "wb") as f:
        f.write(content)

    return output_path


def load_dart_pdf_with_loader(pdf_path: str):
    """하위 호환용. 가능하면 pdfplumber 텍스트를 Document 형태로 감쌉니다."""

    payload = extract_financial_payload_from_pdf_file(pdf_path)
    text = payload.get("text_preview") or ""
    if PyPDFLoader is not None:
        try:
            return PyPDFLoader(pdf_path).load()
        except Exception:
            pass

    # 최소 Document-like 객체
    class _Doc:
        def __init__(self, content: str):
            self.page_content = content
            self.metadata = {"source": pdf_path, "loader": payload.get("loader")}

    return [_Doc(text)]


def download_and_extract_pdf_data(
    link_or_url: str, dcm_no: Optional[str] = None
) -> Dict[str, Any]:
    pdf_path = download_dart_pdf(link_or_url, dcm_no)
    payload = extract_financial_payload_from_pdf_file(pdf_path)
    return {
        "pdf_path": pdf_path,
        "total_pages": payload.get("total_pages", 0),
        "financial_summary": payload.get("financial_summary")
        or empty_financial_summary(),
        "financials": payload.get("financials") or [],
        "loader": payload.get("loader"),
        "documents": load_dart_pdf_with_loader(pdf_path),
        "text_preview": payload.get("text_preview", ""),
    }


def extract_financials_from_pdf(
    link_or_url: str,
    dcm_no: Optional[str] = None,
) -> Dict[str, Any]:
    """download_dart_pdf로 공시 PDF를 받아 FINANCIAL_KEYS와 연도별 financials를 추출합니다."""

    payload = download_and_extract_pdf_data(link_or_url, dcm_no)
    return {
        "pdf_path": payload["pdf_path"],
        "total_pages": payload["total_pages"],
        "financial_summary": payload["financial_summary"],
        "financials": payload.get("financials") or [],
        "loader": payload.get("loader"),
        "text_preview": payload.get("text_preview", "")[:1500],
    }


def enrich_company_with_pdf(company: Dict[str, Any]) -> Dict[str, Any]:
    """부족한 FINANCIAL_KEYS를 공시 PDF에서 보완하고 2개년 financials를 붙입니다."""

    enriched = dict(company)
    link = enriched.get("dart_viewer_link") or (
        enriched.get("latest_report") or {}
    ).get("dart_viewer_link")
    if not link:
        return enriched

    try:
        pdf_payload = extract_financials_from_pdf(link)
    except Exception as error:
        enriched["pdf_error"] = str(error)
        return enriched

    before = enriched.get("financial_summary") or empty_financial_summary()
    merged = merge_financial_summaries(
        before, pdf_payload.get("financial_summary") or {}
    )
    enriched["financial_summary"] = merged
    enriched["financial_summary_normalized"] = financial_summary_normalized(merged)
    enriched["pdf_path"] = pdf_payload.get("pdf_path")
    enriched["pdf_page_count"] = pdf_payload.get("total_pages")
    enriched["pdf_loader"] = pdf_payload.get("loader")

    pdf_financials = [
        row for row in (pdf_payload.get("financials") or []) if isinstance(row, dict)
    ]
    existing_financials = [
        row for row in (enriched.get("financials") or []) if isinstance(row, dict)
    ]
    if pdf_financials:
        # PDF 2개년 결과가 있으면 우선 사용합니다.
        enriched["financials"] = pdf_financials
    elif not existing_financials and has_any_financial_value(merged):
        row = dict(merged)
        row["year"] = str(enriched.get("rcept_dt") or "")[:4] or None
        enriched["financials"] = [row]

    # PDF로 자산이 채워지면 자산추정 단계도 다시 계산합니다.
    assets_int = _parse_amount_to_int(merged.get("자산총계"))
    if assets_int is not None and enriched.get("stage_source") == "asset_estimate":
        stage_label = estimate_investment_stage(assets_int)
        enriched["asset_estimated_stage"] = stage_label
        enriched["estimated_investment_stage"] = stage_label
    if missing_financial_keys(before) and has_any_financial_value(
        pdf_payload.get("financial_summary")
    ):
        previous_source = enriched.get("financial_source") or "없음"
        enriched["financial_source"] = f"{previous_source}+공시PDF"
    elif has_any_financial_value(pdf_payload.get("financial_summary")):
        previous_source = enriched.get("financial_source") or "없음"
        if "공시PDF" not in str(previous_source):
            enriched["financial_source"] = f"{previous_source}+공시PDF"
    return enriched


async def enrich_companies_with_pdf(
    companies: List[Dict[str, Any]],
    *,
    concurrency: int = 4,
) -> List[Dict[str, Any]]:
    """발굴된 기업들에 대해 공시 PDF 재무 추출을 병렬로 수행합니다."""

    semaphore = asyncio.Semaphore(concurrency)

    async def _one(company: Dict[str, Any]) -> Dict[str, Any]:
        # 핵심 키가 이미 채워져 있어도 PDF에서 CAPEX·현금흐름 등을 보완합니다.
        async with semaphore:
            return await asyncio.to_thread(enrich_company_with_pdf, company)

    if not companies:
        return []
    return list(await asyncio.gather(*[_one(company) for company in companies]))


def build_company_dart_payload(
    *,
    overview: Dict[str, Any],
    fin_summary: Dict[str, Any],
    financial_source: str,
    audit_items: Optional[List[Dict[str, Any]]] = None,
    assets_int: Optional[int] = None,
) -> Dict[str, Any]:
    """탐색/단건조회가 공유하는 DART 기업 JSON을 만듭니다."""

    overview_payload = build_overview_payload(overview)
    latest_report = build_latest_report_payload(audit_items)
    stage_label = estimate_investment_stage(assets_int)
    summary = merge_financial_summaries(fin_summary)

    return {
        **overview_payload,
        "asset_estimated_stage": stage_label,
        "estimated_investment_stage": stage_label,
        "stage_source": "asset_estimate",
        "financial_summary": summary,
        "financial_summary_normalized": financial_summary_normalized(summary),
        "financial_source": financial_source,
        "audit_reports_count": len(audit_items or []),
        "latest_report": latest_report,
        "dart_viewer_link": latest_report.get("dart_viewer_link"),
        "rcept_no": latest_report.get("rcept_no"),
        "report_nm": latest_report.get("report_nm"),
        "rcept_dt": latest_report.get("rcept_dt"),
    }


# ===========================================================================
# [단일 기업명 검색 함수]
# ===========================================================================
async def get_company_dart_data(
    company_name: str,
    bsns_year: str = "2023",
    companies_file: str = COMPANIES_JSON_PATH,
) -> Dict[str, Any]:
    """
    입력받은 기업명을 DART에서 즉시 검색하여 기업 개요, 재무제표, 감사보고서 내역을 완전한 JSON 데이터로 반환합니다.
    """
    clean_name = company_name.strip()
    service = AsyncDartService()

    extra_startup_info = {}
    if os.path.exists(companies_file):
        try:
            with open(companies_file, "r", encoding="utf-8") as f:
                c_data = json.load(f).get("data", [])
                for item in c_data:
                    if item.get("name", "").strip() == clean_name:
                        extra_startup_info = item
                        break
        except Exception:
            pass

    async with aiohttp.ClientSession() as session:
        await service.init_corp_codes(session)
        corp_code = service.find_corp_code(clean_name)

        if not corp_code:
            return {
                "query_name": clean_name,
                "matched": False,
                "message": f"'{clean_name}' 기업은 DART 고유번호에 등록되지 않은 기업(비외감 법인 또는 상호 불일치)입니다.",
                "startup_info": extra_startup_info or None,
            }

        overview_task = service.get_company_overview(session, corp_code)
        fin_task = service.get_financial_statements(
            session, corp_code, bsns_year=bsns_year
        )
        audit_task = service.get_audit_reports(session, corp_code)

        overview, financials, audits = await asyncio.gather(
            overview_task, fin_task, audit_task
        )

        fin_items = (
            financials.get("list") if financials.get("status") == "000" else None
        )
        audit_items = audits.get("list") if audits.get("status") == "000" else []

        fin_summary = _extract_financial_summary_from_list(fin_items)
        source_type = (
            "정기보고서(사업보고서)" if has_any_financial_value(fin_summary) else "없음"
        )

        if missing_financial_keys(fin_summary) and audit_items:
            latest_rcept_no = audit_items[0].get("rcept_no")
            if latest_rcept_no:
                doc_financials = await service.extract_financials_from_audit_doc(
                    session, latest_rcept_no
                )
                if has_any_financial_value(doc_financials):
                    fin_summary = merge_financial_summaries(fin_summary, doc_financials)
                    if source_type == "없음":
                        source_type = (
                            f"감사보고서 원문({audit_items[0].get('report_nm')})"
                        )
                    else:
                        source_type = f"{source_type}+감사원문"

        overview_ok = overview if overview.get("status") == "000" else {}
        company_payload = build_company_dart_payload(
            overview={
                **overview_ok,
                "corp_code": corp_code,
                "corp_name": overview_ok.get("corp_name") or clean_name,
            },
            fin_summary=fin_summary,
            financial_source=source_type,
            audit_items=audit_items,
            assets_int=_parse_amount_to_int(fin_summary.get("자산총계")),
        )
        if company_payload.get("dart_viewer_link"):
            company_payload = await asyncio.to_thread(
                enrich_company_with_pdf, company_payload
            )

        return {
            "query_name": clean_name,
            "matched": True,
            "corp_code": corp_code,
            "startup_info": extra_startup_info or None,
            "overview": overview_ok,
            **company_payload,
            "financial_statements_raw": fin_items or [],
            "audit_reports": audit_items,
            "latest_report_link": company_payload.get("dart_viewer_link"),
        }


def print_companies_for_rag(
    companies: List[Dict[str, Any]],
    *,
    title: str = "DART → RAG 전달 데이터",
) -> None:
    """RAG로 넘길 기업 JSON을 터미널에 그대로 출력합니다."""

    print(f"\n{'=' * 70}")
    print(f"[RAG 전달] {title} ({len(companies)}개)")
    print("=" * 70)
    for index, company in enumerate(companies, 1):
        payload = build_rag_handoff_payload(company)
        print(
            f"\n--- [{index}] {payload.get('company_name') or payload.get('name')} ---"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"\n{'=' * 70}\n")


def build_rag_handoff_payload(company: Dict[str, Any]) -> Dict[str, Any]:
    """RAG 입력으로 쓸 필드만 골라 직렬화하기 쉬운 dict로 만듭니다."""

    dart = company.get("dart") if isinstance(company.get("dart"), dict) else {}
    source = {**dart, **company}
    return {
        "company_name": source.get("company_name") or source.get("name"),
        "corp_code": source.get("corp_code") or source.get("id"),
        "ceo": source.get("ceo"),
        "established_date": source.get("established_date")
        or source.get("established_at"),
        "legal_class": source.get("legal_class"),
        "bizr_no": source.get("bizr_no"),
        "industry_code": source.get("industry_code"),
        "address": source.get("address"),
        "homepage_url": source.get("homepage_url"),
        "estimated_investment_stage": source.get("estimated_investment_stage"),
        "asset_estimated_stage": source.get("asset_estimated_stage"),
        "stage_source": source.get("stage_source"),
        "screening": source.get("screening"),
        "financial_summary": source.get("financial_summary") or {},
        "financial_summary_normalized": source.get("financial_summary_normalized")
        or {},
        "financial_source": source.get("financial_source"),
        "dart_viewer_link": source.get("dart_viewer_link"),
        "rcept_no": source.get("rcept_no"),
        "report_nm": source.get("report_nm"),
        "rcept_dt": source.get("rcept_dt"),
        "pdf_path": source.get("pdf_path"),
        "audit_reports_count": source.get("audit_reports_count"),
        "latest_report": source.get("latest_report") or {},
    }


# ---------------------------------------------------------------------------
# LangGraph 연결 노드
# ---------------------------------------------------------------------------
async def dart_lookup_node(state: GraphState) -> Dict[str, Any]:
    """DART에서 에너지 스타트업을 찾아 RAG가 사용할 State에 누적합니다.

    ``search_energy_startups_dart_only``가 반환하는 JSON 구조를 그대로 유지하면서
    공통 State에서 사용하는 ``id``와 ``name`` 필드만 추가합니다. 따라서 다음
    RAG 노드는 ``financial_summary``와 ``dart_viewer_link``를 바로 사용할 수 있습니다.
    """

    already_selected = list(state.get("eligible_companies", []))
    target_count = int(state.get("target_company_count", DEFAULT_COMPANY_COUNT))
    needed_count = max(target_count - len(already_selected), 0)
    fetch_count = needed_count * DART_FETCH_MULTIPLIER
    search_attempts = int(state.get("search_attempts", 0)) + 1
    seen_corp_codes = set(state.get("dart_seen_corp_codes", []))
    seen_corp_codes.update(
        str(company.get("corp_code", ""))
        for company in already_selected
        if company.get("corp_code")
    )

    rejected = list(state.get("dart_rejections", []))
    try:
        dart_results = (
            await search_energy_startups_dart_only(
                limit=fetch_count,
                exclude_corp_codes=seen_corp_codes,
            )
            if needed_count
            else []
        )
    except Exception as error:
        dart_results = []
        rejected.append(
            {
                "name": "DART 에너지 스타트업 탐색",
                "reason": f"DART 조회 오류: {error}",
            }
        )

    newly_selected: List[Dict[str, Any]] = []
    for dart_company in dart_results:
        corp_code = str(dart_company.get("corp_code", ""))
        if not corp_code or corp_code in seen_corp_codes:
            continue

        # DART 반환 JSON은 그대로 두고 공통 State용 별칭만 추가합니다.
        newly_selected.append(
            {
                **dart_company,
                "id": corp_code,
                "name": dart_company.get("company_name", ""),
                "established_at": dart_company.get("established_date", ""),
                "dart": dart_company,
            }
        )
        seen_corp_codes.add(corp_code)

    eligible_companies = [*already_selected, *newly_selected]
    passed_names = [company.get("name", "") for company in newly_selected]
    message = (
        f"DART 에너지 스타트업 탐색: 신규 {len(newly_selected)}개, "
        f"누적 {len(eligible_companies)}/{target_count}개"
    )

    print("\n[작업] DART 공시·재무 탐색")
    print(
        f"  입력 State : 기존={len(already_selected)}개, "
        f"추가 필요={needed_count}개, DART 탐색={fetch_count}개(x{DART_FETCH_MULTIPLIER})"
    )
    print(
        f"  반환 값    : 신규 기업={passed_names}, " f"누적={len(eligible_companies)}개"
    )
    if newly_selected:
        print_companies_for_rag(
            newly_selected,
            title="lookup_dart 신규 발굴분 (스크리닝 전, RAG 후보 원본)",
        )

    return {
        "eligible_companies": eligible_companies,
        "search_attempts": search_attempts,
        "next_stage": "screen_startups",
        "execution_log": [*state.get("execution_log", []), message],
        "dart_rejections": rejected,
        "dart_seen_corp_codes": list(seen_corp_codes),
    }


# ---------------------------------------------------------------------------
# 테스트 실행부
# ---------------------------------------------------------------------------
if __name__ == "__main__":

    async def run_test():
        print("=" * 70)
        print(
            f"🚀 [DART 자체 필터링] 에너지 산업 분야 Seed~Series C 스타트업 "
            f"{DEFAULT_COMPANY_COUNT}개 탐색"
        )
        print("   - 외부 companies.json 의존 없이 오직 DART 11만 법인 풀에서 직접 추출")
        print(
            "   - 조건: 법인구분=비상장(E)/코넥스(N), 설립=2015년 이후, 자산=500억 이하"
        )
        print("=" * 70)

        results = await search_energy_startups_dart_only(limit=DEFAULT_COMPANY_COUNT)
        print(f"\n🎉 최종 발굴된 에너지 스타트업: 총 {len(results)}개\n")

        for idx, comp in enumerate(results, 1):
            filled = len(FINANCIAL_KEYS) - len(
                missing_financial_keys(comp.get("financial_summary"))
            )
            print(f"[{idx}] {comp['company_name']} (대표: {comp.get('ceo')})")
            print(
                f"    • 법인구분: {comp.get('legal_class')} | 설립일: {comp.get('established_date')}"
            )
            print(
                f"    • 사업자번호: {comp.get('bizr_no')} | 업종코드: {comp.get('industry_code')}"
            )
            print(f"    • 홈페이지: {comp.get('homepage_url')}")
            print(f"    • 추정 단계: {comp.get('estimated_investment_stage')}")
            print(
                f"    • 재무출처: {comp.get('financial_source')} | 키 {filled}/{len(FINANCIAL_KEYS)}"
            )
            print(f"    • 재무제표: {comp.get('financial_summary')}")
            print(
                f"    • PDF: {comp.get('pdf_path') or comp.get('pdf_error') or '없음'}"
            )
            print(f"    • DART 공시 링크: {comp.get('dart_viewer_link')}\n")

    asyncio.run(run_test())
