# weather-verify

한국 기상청 예보 vs 글로벌 모델 예보를 **실측(정답값) 대비 오차**로 정량 평가하는 시스템의 첫 수직 슬라이스입니다.

현재 범위는 **서울 1지점 · 기온(`temperature_2m`) 1변수 · 최근 2주**이며, Open-Meteo ECMWF 모델의 리드타임별 MAE를 계산합니다. 기상청 API는 별도 진단 스크립트로 키 검증 및 아카이브 깊이를 확인합니다.

## 현재 구현 상태

| 구성요소 | 상태 | 설명 |
|---|---|---|
| 표준 스키마 (`src/schema.py`) | ✅ | long-format DataFrame 규약 |
| 정렬·지표 (`src/core/`) | ✅ | 순수 pandas, 외부 의존성 0 |
| Open-Meteo 소스 | ✅ | Previous Runs API → 표준 스키마 |
| 첫 슬라이스 실행 (`run_slice.py`) | ✅ | 리드타임별 글로벌 MAE 표 출력 |
| KMA 진단 (`kma_probe.py`) | ✅ | 인증키 검증 + 아카이브 깊이 프로브 |
| 기상청 예보 소스·MAE 비교 | ⏳ | 미구현 |
| cron 적재 | ⏳ | 미구현 (KMA API가 과거 3일만 제공) |
| 대시보드·LLM | ⏳ | 범위 밖 |

### KMA 아카이브 진단 결과 (2026-07-03 기준)

기상청 단기예보 API(`getVilageFcst`)는 **최근 약 3일치 발표 예보만** 조회 가능합니다.

- 오늘·어제 `base_date`: 정상 (`resultCode 00`)
- 3일 이전: `최근 3일 간의 자료만 제공합니다` (`resultCode 10`)
- **판정: cron 적재 필요** — 백테스트를 위해 오늘부터 예보를 저장해야 합니다.

## 설계 원칙

1. **숫자는 코드가, 말은 LLM이** — 모든 계산은 pandas로 결정론적 처리 (LLM 없음)
2. **가장 위험한 곳을 순수 함수로 격리** — `core/align.py`, `core/metrics.py`는 네트워크·파일 의존성 0
3. **외부 API 지저분함은 sources에 격리** — 코어로 새어 나가지 않음

## 프로젝트 구조

```
weather-verify/
├── src/
│   ├── schema.py              # 표준 long-format 스키마
│   ├── core/
│   │   ├── align.py           # 예보·정답 join
│   │   └── metrics.py         # MAE / RMSE / 리드타임별 MAE
│   └── sources/
│       ├── openmeteo.py       # Open-Meteo Previous Runs API
│       └── kma_probe.py       # 기상청 API 키·아카이브 진단
├── tests/
│   ├── test_align.py
│   └── test_metrics.py
├── run_slice.py               # 첫 수직 슬라이스 엔트리포인트
├── requirements.txt
└── .env.example
```

### 표준 스키마

| 컬럼 | 설명 |
|---|---|
| `station` | 지점 ID (예: `seoul`) |
| `valid_time` | 예보 유효시각 (UTC, timezone-aware) |
| `lead_time_h` | 리드타임 시간 (정답 프록시는 `0`) |
| `variable` | 변수명 (예: `temperature_2m`) |
| `value` | float |
| `source` | 출처 (예: `openmeteo_ecmwf`, `ground_truth`) |

정답값(v0)은 Open-Meteo `previous_day0` 프록시이며, 추후 ASOS 실측으로 교체할 때도 동일 스키마를 유지합니다.

## 요구 사항

- Python 3.10+
- 인터넷 연결 (라이브 API 호출 시)

## 설치

```bash
git clone https://github.com/JamesRhee1/weather-verify.git
cd weather-verify
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 사용법

### 1. Open-Meteo 교차검증 슬라이스

API 키 없이 실행 가능합니다.

```bash
python run_slice.py
```

출력 예시:

```
리드타임별 글로벌 모델 MAE (°C)
 lead_time_h    mae    n
          24  0.544  504
          48  0.771  504
```

API 실패 시 합성 데이터로 자동 fallback하며 `[fallback]` 메시지가 stderr에 출력됩니다.

### 2. 기상청 API 진단

[공공데이터포털](https://www.data.go.kr/data/15084084/openapi.do)에서 단기예보 API 활용신청 후 키를 설정합니다.

```bash
cp .env.example .env
# .env 편집 — 일반 인증키(디코딩)를 KMA_API_KEY_DECODING에 입력
python -m src.sources.kma_probe
```

| 변수 | 설명 |
|---|---|
| `KMA_API_KEY_DECODING` | 일반 인증키 (`+`, `=` 포함 원본) |
| `KMA_API_KEY_ENCODING` | 인코딩 인증키 (`%2B`, `%3D` 등, 선택) |

진단 스크립트는 키 검증 → 오늘부터 10일 전까지 `base_date` 프로브 → 자동 판정을 수행합니다.

## 테스트

```bash
pytest -q
```

현재 10개 테스트 (align 4, metrics 6).

## 로드맵

- [ ] 기상청 예보 → 표준 스키마 변환 (`sources/kma.py`)
- [ ] cron 적재 파이프라인 (발표시각 02/05/08/11/14/17/20/23시)
- [ ] 기상청 MAE vs 글로벌 MAE 비교 표
- [ ] ASOS 실측 정답값 연동
- [ ] 대시보드 UI

## 라이선스

MIT — [LICENSE](LICENSE) 참고
