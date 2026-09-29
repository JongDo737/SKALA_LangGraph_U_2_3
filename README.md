# AI Startup Investment Evaluation Agent
본 프로젝트는 에너지 스타트업에 대한 투자 가능성을 자동으로 평가하는 에이전트를 설계하고 구현한 실습 프로젝트입니다.


## Overview
- Objective : 친환경·에너지·핵융합 스타트업의 시장성, 경쟁력, 투자 적합성 분석
- Method : LangGraph Multi Agent, Agentic RAG, DART 및 웹 검색


## Features
- 평가 기업 수 설정
- TIPS 기업 후보 탐색 및 DART 법인 확인
- 기업별 시장성·경쟁사·투자 판단 병렬 처리
- 단계별 검증과 제한된 재시도
- 근거가 부족한 판단은 `review_required`로 보류


## Tech Stack 
- Framework : LangGraph 
- LLM/Generator : {GPT version}
- LLM/Judge : {GPT version}
- Retrieval : {VectorDB} - {Hit Rate@K}, {MRR} 
- Embedding : {Open-source embedding}


## Agents
- DART Agent: 기업 식별, 기업개황, 공시 및 재무정보 수집
- Market Agent: PDF RAG와 웹 검색을 이용한 시장성 평가
- Competition Agent: 경쟁사 탐색과 동일 기준 비교
- Judge Agent: VC·PE 평가 기준에 따른 투자 판단
- Report Agent: 근거와 판단 결과를 보고서 형식으로 구성


## Architecture
```text
스타트업 탐색 → TIPS 정보 정규화 → DART 확인 → 기업 수 검증
                                            ├─ 부족: 후보 재탐색
                                            └─ 충족: 기업별 병렬 평가
                                                      ├─ 시장성 RAG
                                                      ├─ 경쟁사 비교
                                                      └─ 투자 판단
                                                               ↓
                                                        보고서 생성
```

`target_company_count`는 메인 State에 저장되어 모든 노드가 같은 기업 수를
사용합니다. 기업별 병렬 결과는 LangGraph reducer로 합치며, 각 단계는 검증 실패
시 정해진 횟수만 재실행합니다.


## Directory Structure
├── data/                  # 문서 풀
│   └── companies.json     # 스타트업 기업 원본 데이터
├── agents/                # 평가 기준별 Agent 모듈
│   ├── __init__.py
│   ├── dart.py             # 기업 공시 검색
│   ├── rag.py              # 시장 조사 및 기업 정보 추출
│   ├── compitition.py      # 경쟁사 조사 및 비교
│   ├── judge.py            # 투자 적합성 판단
│   └── report.py           # 투자 보고서 PDF 생성
├── prompts/               # 프롬프트 템플릿
├── outputs/               # 평가 결과 저장
├── app.py                 # Agent 노드 연결 및 전체 실행
├── state.py               # 공통 기업 객체 및 LangGraph State
├── .gitignore
└── README.md


## State

기업 객체는 원본을 덮어쓰지 않고 단계별 필드를 추가합니다.

```text
기본 정보
└─ tips
   └─ dart
      └─ market
         └─ competition
            └─ judgement
```

각 근거에는 출처 URL, 조회 시각, 매칭 방식, 신뢰도, 검증 상태를 함께 저장합니다.
DART 미등록과 투자 부적합은 같은 의미가 아니므로 별도 상태로 관리합니다.


## Usage
```bash
# 기본 10개 기업, DART 엄격 검증
python app.py

# 기업 수 변경
python app.py --company-count 5

# API 구현 전 그래프 연결을 확인하는 개발용 실행
python app.py --company-count 5 --allow-unverified-dart
```


## Contributors 
- 신종민 : 메인 그래프 연결, 단계별 검증 및 산출물 통합
- 이우성 : DART 검색, 설계 산출물
- 손민재 : 시장조사 RAG, 기업 정보 추출
- 권태현 : 경쟁사 비교, 투자 판단
- 김가빈 : 투자 보고서 생성
