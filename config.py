"""프로젝트 전역 설정입니다."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# 평가할 기본 기업 수입니다. 실행 시 --company-count로 덮어쓸 수 있습니다.
DEFAULT_COMPANY_COUNT = 5
DEFAULT_MAX_SEARCH_ATTEMPTS = 5

# 스타트업 검증에 사용하는 저비용 검색 모델입니다.
SCREENING_MODEL = "gpt-4o-mini"

# 스크리닝 탈락을 대비해 DART에서 목표 개수의 몇 배를 미리 찾습니다.
DART_FETCH_MULTIPLIER = 4

# 스타트업 스크리닝 LLM 호출을 동시에 몇 개까지 보낼지 제한합니다.
SCREENING_CONCURRENCY = 8
