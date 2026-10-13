# Subject

AI 데이터센터 전력·에너지 인프라 기업을 재무·시장·경쟁 근거로 가려, 투자 적합 여부만 보고서로 남기는 **Orchestrator-Workers** 프로젝트다. 기존 순차 파이프라인은 `app_legacy.py`에 둔다.

## Overview

- **Objective:** 기업 하나를 재무, 시장 자료, 시장성, 경쟁으로 나누어 조사하고, 근거가 있는 기업만 적합 여부를 판단한다.
- **Pattern:** Orchestrator-Workers. 네 관점은 서로 독립이고, 부족한 관점만 다시 조사하면 된다. 고정된 호출 순서에 묶을 이유가 없다.
- **동적 처리:** 워커 수는 계획 이후에 정해진다. 서브태스크(기업 × 관점)를 State에 저장하고 `Send`로 fan-out 한다. 실패한 (기업, 관점)만 재계획하고, 유효한 결과는 재사용한다. 프로필이 약하면 프로필 보완이 먼저다. 재계획은 최대 2회. 일부 워커 실패는 재시도 후 제외하거나, 경고를 남기고 계속한다.

## Selected Technologies

- **SW:** LangGraph Orchestrator-Workers와 Chroma RAG. 관점별 병렬 배정과, 실패한 관점만 다시 도는 루프에 쓴다.
- **HW:** 평가 대상은 ESS, 냉각, 전력 공급 등 AI 데이터센터 에너지 인프라다. 특정 장비의 우열은 정하지 않는다. 기업 비교 기준은 DART 재무(VBM)로 맞춘다.



## Features

- DART에서 에너지 키워드 법인 중 비상장·코넥스, 2015년 이후 설립, 자산 500억 원 이하를 거른 뒤 재무를 수집한다. 공시 값이 비면 감사보고서 PDF에서 항목을 읽는다.
- 시장 PDF 18건을 Chroma에 넣어 규모·성장·수요를 추출한다. 거리 0.4 이내 청크가 없으면 웹으로 보완한다.
- 사업 설명과 분야는 웹 검색 스니펫으로만 확정한다. DART에 그 문장이 없기 때문이다.
- 시장성 10개 질문(100점, 적합 하한 50점)과 VBM(ROIC–WACC)을 함께 본다.
- **확증 편향 방지:** 경쟁사는 검색으로 확인된 타사만 인정한다. 시장성 근거의 도메인 다양성과 저품질 출처 비중을 검사한다. 보고서에는 한계·반대 근거가 있어야 한다.
- **보고서 품질 평가:** 초안 이후 Groundedness, 중립성, 편향 통제를 규칙으로 판정한다. 미달이면 해당 관점만 재계획한다. 예산이 끝나면 미해결을 저장하고, 통과분으로 보고서를 마친다.



## Tech Stack

- **Framework:** LangGraph (`Send` fan-out, SQLite checkpoint)
- **LLM/Generator:** GPT-4o-mini (스크리닝·프로필·경쟁사), GPT-4.1-mini (시장 요약)
- **LLM/Judge:** GPT-4o-mini (시장성 10문항). 보고서 품질은 규칙 검사이며 LLM 심사를 쓰지 않는다.
- **Retrieval:** ChromaDB(cosine). Hit Rate@4 **0.981**, MRR@10 **0.915** (질의 54개, `docs/retrieval_eval.md`)
- **Embedding:** BAAI/bge-m3



## Agents

기존 7단계를 기능 단위 노드로 나누고, 계획 이후에만 워커를 띄운다.

- **Orchestrator:** `orchestrator/nodes/planner.py`. 서브태스크를 State에 두고, 실패 사유만 다음 계획에 반영한다.
- **DART·스크리닝·프로필:** `dart_lookup`, `startup_screen`, `profile_prep`
- **Workers:** `worker_financial`(VBM), `worker_market_rag`(시장 근거), `worker_market_score`(10문항 채점), `worker_competitor`(후보 검색 확인)
- **Synthesizer:** `merge_results` → `validate_results` → `check_sufficiency` → `competitor_compare` → `investment_decision` → `check_quota` → `report_draft`
- **품질·종료:** `quality_gate`, `record_failure`, `apply_fallback`, `finalize_report`, `save_unresolved`



## State Schema

상세는 `orchestrator/state.py`.

- **제어 vs 페이로드 분리:** State는 계획, 상태, 요약, ref만 가진다. 프로필·워커 원문·보고서 본문은 `outputs/runs/{run_id}/`에 둔다.
- **관측성 위치:** 결정·실패·품질 이력은 `events.jsonl`에 `{trace_id, node, event, reason, ts}`로 남긴다. State에는 `status`, `step_count`, `error_count`, `last_error`만 둔다.
- **지속성 비용:** 체크포인트에 오르는 State는 요약 크기다. 이력은 State에 쌓지 않는다.
- **상관:** `run_id`(trace_id)를 State, 저장 경로, 이벤트 로그, LangSmith metadata에 공통으로 쓴다.
- **재개/복구:** `status`, `node_status`, `step_count`, `replan_count`, 결과 ref를 State에 둔다. `--checkpoint sqlite --resume RUN_ID`로 이어서 실행한다.
- **동시 처리:** 워커가 동시에 쓰는 `results`, `errors`, `error_count`, `last_error`, `node_status`, `step_count`에 리듀서를 둔다.
- **종료 보장:** `replan_count ≤ max_replans(2)`, `step_count < max_steps`. `recursion_limit`은 마지막 안전장치다.



## Architecture



## Directory Structure

```text
├── data/                  # 시장 PDF, 문서 메타, 검색 평가 QA
├── agents/                # DART, RAG, 재무, 시장성 채점, 경쟁사, 투자 판단, 보고서
├── orchestrator/          # State, planner, 라우팅, 노드, 품질 규칙, 그래프
├── prompts/               # 프롬프트
├── scripts/               # eval_retrieval.py
├── tests/                 # 오케스트레이터 테스트
├── outputs/               # runs/{YYYYMMDD_HHMMSS}/ 보고서·이벤트·워커 결과
├── docs/                  # 검색 평가, 보고서 계약
├── chroma_db/             # 시장 벡터 DB
├── assets/                # PDF 폰트
├── app.py                 # 실행
├── app_legacy.py          # 기존 순차 파이프라인
└── README.md
```



## Usage

```bash
pip install -r requirements.txt
python app.py
```



## Contributors

- **신종민:** State·동적 작업 배정
- **손민재:** State·동적 작업 배정
- **이우성:** 오케스트리이터 워커 패턴 설계
- **김가빈:** 오케스트리이터 워커 패턴 설계, 보고서 품질 평가 지표 생성
- **권태현:** 보고서 품질 평가 지표 생성 및 작업 연결

