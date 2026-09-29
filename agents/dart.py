# 작성자: 이우성
# 파일 설명: 무작위 기업 식별자를 입력받아 기업 정보를 조회하고,
# DART 검색 결과를 JSON 형식으로 반환하며, 
# 1) 외부 데이터 없이 오직 DART만으로 에너지 Seed~Series C 스타트업을 직접 탐색하는 파이프라인
# 2) 특정 기업명 즉시 검색 및 재무제표 JSON 반환
# 3) 공시자료 PDF 다운로드 및 LangChain PyPDFLoader 데이터 추출 기능을 담당합니다.
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
from typing import List, Dict, Any, Optional
import aiohttp
from langchain_community.document_loaders import PyPDFLoader

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
PDF_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "pdfs")


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
                    if line.startswith("dart_api_key") or line.startswith("DART_API_KEY"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            return parts[1].strip().strip("\"' ")

    raise ValueError("DART API 키를 찾을 수 없습니다. .env 파일에 DART_API_KEY를 설정해 주세요.")


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
                raise RuntimeError(f"DART 고유번호 다운로드 실패 (상태코드: {response.status})")
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
                        clean_name.replace("(주)", "").replace("주식회사", "").replace(" ", "").strip()
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

        simplified = clean_name.replace("(주)", "").replace("주식회사", "").replace(" ", "")
        return self.corp_map.get(simplified)

    async def get_company_overview(self, session: aiohttp.ClientSession, corp_code: str) -> Dict[str, Any]:
        """기업 기본 개요 정보(company.json) 조회"""
        url = "https://opendart.fss.or.kr/api/company.json"
        params = {"crtfc_key": self.api_key, "corp_code": corp_code}
        async with self.semaphore:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return {}
                    content = await resp.read()

                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    xml_files = [f for f in z.namelist() if f.endswith(".xml")]
                    if not xml_files:
                        return {}
                    raw_xml = z.read(xml_files[0]).decode("utf-8", errors="ignore")

                clean_text = re.sub(r"<[^>]+>", " ", raw_xml)
                clean_text = re.sub(r"&nbsp;", " ", clean_text)
                clean_text = re.sub(r"\s+", " ", clean_text)

                patterns = {
                    "자산총계": r"(?:자\s*산\s*총\s*계)\s*([0-9,]+|\([0-9,]+\))",
                    "부채총계": r"(?:부\s*채\s*총\s*계)\s*([0-9,]+|\([0-9,]+\))",
                    "자본총계": r"(?:자\s*본\s*총\s*계)\s*([0-9,]+|\([0-9,]+\))",
                    "매출액": r"(?:[Ⅰ-Ⅹ\d\.\s]*(?:매\s*출\s*액|수익\(매출액\)|영\s*업\s*수\s*익))(?:\s*\(주석[^\)]*\))?\s*([0-9,]+|\([0-9,]+\))",
                    "영업이익": r"(?:[Ⅰ-Ⅹ\d\.\s]*(?:영\s*업\s*이\s*익(?:\(손실\))?))(?:\s*\(주석[^\)]*\))?\s*([0-9,]+|\([0-9,]+\))",
                    "당기순이익": r"(?:[Ⅰ-Ⅹ\d\.\s]*(?:당\s*기\s*순\s*이\s*익(?:\(손실\))?))(?:\s*\(주석[^\)]*\))?\s*([0-9,]+|\([0-9,]+\))",
                }

                extracted = {}
                for key, pattern in patterns.items():
                    m = re.search(pattern, clean_text)
                    extracted[key] = m.group(1).strip() if m else None

                return extracted
            except Exception:
                return {}


def _extract_financial_summary_from_list(financial_items: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """정기 재무제표 계정 리스트에서 핵심 지표 추출"""
    if not financial_items or not isinstance(financial_items, list):
        return {}

    summary = {
        "자산총계": None,
        "부채총계": None,
        "자본총계": None,
        "매출액": None,
        "영업이익": None,
        "당기순이익": None,
    }
    target_names = {
        "자산총계": ["자산총계"],
        "부채총계": ["부채총계"],
        "자본총계": ["자본총계"],
        "매출액": ["매출액", "수익(매출액)", "영업수익"],
        "영업이익": ["영업이익", "영업이익(손실)"],
        "당기순이익": ["당기순이익", "당기순이익(손실)"],
    }
    for item in financial_items:
        account_name = item.get("account_nm", "").strip()
        current_amount = item.get("thstrm_amount")
        for key, aliases in target_names.items():
            if summary[key] is None and account_name in aliases:
                summary[key] = current_amount
    return summary


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
    target_keywords = keywords or ["에너지", "배터리", "수소", "솔라", "신재생", "전력", "ess", "태양광"]
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
            print(f"  🎲 DART 에너지 법인 {len(candidate_corps)}개를 무작위(Random)로 섞어 탐색 루프를 시작합니다...")
        else:
            print(f"  🔍 DART 내 에너지 관련 법인 {len(candidate_corps)}개 발견. Seed~Series C 필터링 시작...")


        # 2. 비동기 배치 검사 (20개씩 묶어서 개요·재무·감사를 병렬 조회)
        batch_size = 20
        print(f"  ⚡ DART 병렬 탐색: 목표 {limit}개, 배치 {batch_size}개씩 동시 조회")
        for i in range(0, len(candidate_corps), batch_size):
            batch = candidate_corps[i : i + batch_size]
            overview_tasks = [service.get_company_overview(session, code) for _, code in batch]
            overviews = await asyncio.gather(*overview_tasks, return_exceptions=True)
            overviews = [
                item if isinstance(item, dict) else {"status": "ERROR", "message": str(item)}
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
                item if isinstance(item, dict) else {"status": "ERROR", "message": str(item)}
                for item in fin_results
            ]
            audit_results = [
                item if isinstance(item, dict) else {"status": "ERROR", "message": str(item)}
                for item in audit_results
            ]

            audit_doc_tasks = []
            audit_doc_indexes = []
            parsed_rows = []
            for index, ((company_name, corp_code, overview), financials, audits) in enumerate(
                zip(eligible_batch, fin_results, audit_results)
            ):
                fin_items = financials.get("list") if financials.get("status") == "000" else None
                audit_items = audits.get("list") if audits.get("status") == "000" else []
                fin_summary = _extract_financial_summary_from_list(fin_items)
                source_type = "정기보고서(사업보고서)" if any(fin_summary.values()) else "없음"
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
                if not any(fin_summary.values()) and audit_items:
                    latest_rcept_no = audit_items[0].get("rcept_no")
                    if latest_rcept_no:
                        audit_doc_indexes.append(index)
                        audit_doc_tasks.append(
                            service.extract_financials_from_audit_doc(session, latest_rcept_no)
                        )

            if audit_doc_tasks:
                doc_financials_list = await asyncio.gather(
                    *audit_doc_tasks,
                    return_exceptions=True,
                )
                for row_index, doc_financials in zip(audit_doc_indexes, doc_financials_list):
                    if isinstance(doc_financials, Exception):
                        continue
                    if doc_financials:
                        parsed_rows[row_index]["fin_summary"] = doc_financials
                        report_nm = parsed_rows[row_index]["audit_items"][0].get("report_nm")
                        parsed_rows[row_index]["source_type"] = f"감사보고서 원문({report_nm})"

            for row in parsed_rows:
                overview = row["overview"]
                fin_summary = row["fin_summary"]
                audit_items = row["audit_items"]
                company_name = row["company_name"]
                corp_code = row["corp_code"]
                est_dt = overview.get("est_dt", "19000101")
                corp_cls = overview.get("corp_cls")

                assets_int = None
                assets_str = fin_summary.get("자산총계")
                if assets_str:
                    try:
                        clean_num = assets_str.replace(",", "").replace("(", "").replace(")", "").strip()
                        assets_int = int(clean_num)
                        if assets_int > max_assets_krw:
                            continue
                    except Exception:
                        pass

                if any(fin_summary.values()) or audit_items:
                    stage_label = estimate_investment_stage(assets_int)
                    company_data = {
                        "company_name": company_name,
                        "corp_code": corp_code,
                        "ceo": overview.get("ceo_nm"),
                        "established_date": est_dt,
                        "address": overview.get("adres"),
                        "industry_code": overview.get("induty_code"),
                        "legal_class": "비상장 외감기업" if corp_cls == "E" else "코넥스 상장벤처",
                        "asset_estimated_stage": stage_label,
                        "estimated_investment_stage": stage_label,
                        "stage_source": "asset_estimate",
                        "financial_summary": fin_summary,
                        "financial_source": row["source_type"],
                        "audit_reports_count": len(audit_items),
                        "dart_viewer_link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={audit_items[0].get('rcept_no')}" if audit_items else None,
                    }
                    recommended_startups.append(company_data)
                    print(f"    ✔ 에너지 스타트업 발굴 성공: {company_name} (단계: {stage_label}, 자산: {fin_summary.get('자산총계', '확인중')})")

                if len(recommended_startups) >= limit:
                    break

            if len(recommended_startups) >= limit:
                break

    return recommended_startups


# ===========================================================================
# [PDF 다운로드 및 PyPDFLoader 처리 함수]
# ===========================================================================
def parse_rcp_and_dcm(link_or_url: str, default_dcm: Optional[str] = None) -> tuple[str, str]:
    m_rcp = re.search(r'rcp_?no=([0-9]+)', link_or_url, re.IGNORECASE)
    m_dcm = re.search(r'dcm_?no=([0-9]+)', link_or_url, re.IGNORECASE)

    rcp_no = m_rcp.group(1) if m_rcp else link_or_url.strip()
    dcm_no = m_dcm.group(1) if m_dcm else default_dcm

    if not dcm_no:
        viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
        req = urllib.request.Request(viewer_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m_btn = re.search(r"openPdfDownload\s*\(\s*['\"]([0-9]+)['\"]\s*,\s*['\"]([0-9]+)['\"]\s*\)", html)
        if m_btn:
            rcp_no, dcm_no = m_btn.group(1), m_btn.group(2)
        else:
            m_dcm2 = re.search(r"dcmNo[\'\"]?\s*[:=]\s*[\'\"]?([0-9]+)", html)
            if m_dcm2:
                dcm_no = m_dcm2.group(1)

    if not rcp_no or not dcm_no:
        raise ValueError(f"rcp_no 또는 dcm_no를 추출할 수 없습니다: {link_or_url}")

    return rcp_no, dcm_no


def download_dart_pdf(link_or_url: str, dcm_no: Optional[str] = None, output_dir: str = PDF_OUTPUT_DIR) -> str:
    os.makedirs(output_dir, exist_ok=True)
    rcp_no, dcm_no = parse_rcp_and_dcm(link_or_url, dcm_no)

    pdf_download_url = f"https://dart.fss.or.kr/pdf/download/pdf.do?rcp_no={rcp_no}&dcm_no={dcm_no}"
    referer_url = f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcp_no}&dcm_no={dcm_no}"
    output_path = os.path.join(output_dir, f"dart_{rcp_no}_{dcm_no}.pdf")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer_url,
    }

    req = urllib.request.Request(pdf_download_url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        content = resp.read()

    if len(content) == 0:
        raise RuntimeError(f"PDF 다운로드 실패 (0 바이트 수신): {pdf_download_url}")

    with open(output_path, "wb") as f:
        f.write(content)

    return output_path


def load_dart_pdf_with_loader(pdf_path: str):
    loader = PyPDFLoader(pdf_path)
    return loader.load()


def download_and_extract_pdf_data(link_or_url: str, dcm_no: Optional[str] = None) -> Dict[str, Any]:
    pdf_path = download_dart_pdf(link_or_url, dcm_no)
    docs = load_dart_pdf_with_loader(pdf_path)
    return {
        "pdf_path": pdf_path,
        "total_pages": len(docs),
        "documents": docs,
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
        fin_task = service.get_financial_statements(session, corp_code, bsns_year=bsns_year)
        audit_task = service.get_audit_reports(session, corp_code)

        overview, financials, audits = await asyncio.gather(overview_task, fin_task, audit_task)

        fin_items = financials.get("list") if financials.get("status") == "000" else None
        audit_items = audits.get("list") if audits.get("status") == "000" else []

        fin_summary = _extract_financial_summary_from_list(fin_items)
        source_type = "정기보고서(사업보고서)" if any(fin_summary.values()) else "없음"

        if not any(fin_summary.values()) and audit_items:
            latest_rcept_no = audit_items[0].get("rcept_no")
            if latest_rcept_no:
                doc_financials = await service.extract_financials_from_audit_doc(session, latest_rcept_no)
                if doc_financials:
                    fin_summary = doc_financials
                    source_type = f"감사보고서 원문({audit_items[0].get('report_nm')})"

        return {
            "query_name": clean_name,
            "matched": True,
            "corp_code": corp_code,
            "startup_info": extra_startup_info or None,
            "overview": overview if overview.get("status") == "000" else {},
            "financial_summary": fin_summary,
            "financial_source": source_type,
            "financial_statements_raw": fin_items or [],
            "audit_reports": audit_items,
            "latest_report_link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={audit_items[0].get('rcept_no')}" if audit_items else None,
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

    print("\n[노드 실행] lookup_dart (agents/dart.py)")
    print(
        f"  입력 State : 기존={len(already_selected)}개, "
        f"추가 필요={needed_count}개, DART 탐색={fetch_count}개(x{DART_FETCH_MULTIPLIER})"
    )
    print(
        f"  반환 값    : 신규 기업={passed_names}, "
        f"누적={len(eligible_companies)}개"
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
        print("   - 조건: 법인구분=비상장(E)/코넥스(N), 설립=2015년 이후, 자산=500억 이하")
        print("=" * 70)

        results = await search_energy_startups_dart_only(limit=DEFAULT_COMPANY_COUNT)
        print(f"\n🎉 최종 발굴된 에너지 스타트업: 총 {len(results)}개\n")

        for idx, comp in enumerate(results, 1):
            print(f"[{idx}] {comp['company_name']} (대표: {comp['ceo']})")
            print(f"    • 법인구분: {comp['legal_class']} | 설립일: {comp['established_date']}")
            print(f"    • 추정 단계: {comp['estimated_investment_stage']}")
            print(f"    • 재무제표: {comp['financial_summary']}")
            print(f"    • DART 공시 링크: {comp['dart_viewer_link']}\n")

    asyncio.run(run_test())
