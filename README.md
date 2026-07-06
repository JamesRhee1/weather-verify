# weather-verify

한국 기상청 예보 vs 글로벌 모델 예보를 **실측(정답값) 대비** 정량 평가하는 시스템입니다.

**최종 목표:** KMA 강수확률(POP) 예보 vs 글로벌 확률예보의 **Brier score / reliability diagram** 비교.

현재는 서울 1지점 · cron 적재 parquet 기반 **POP Brier 리포트**(`run_pop_report.py`)와 임시 Streamlit 대시보드까지 구현되어 있습니다. ASOS 실측 적재·cron 운영이 붙으면 end-to-end 검증이 가능합니다.

## 현재 구현 상태 (2026-07-06)

| 구성요소 | 상태 | 설명 |
|---|---|---|
| 표준 스키마 (`src/schema.py`) | ✅ | long-format 6컬럼 규약 |
| 정렬 (`src/core/align.py`) | ✅ | 예보·정답 join, 순수 pandas |
| 연속형 지표 (`metrics.py`) | ✅ | MAE, RMSE, 리드타임별 MAE |
| 확률 지표 (`metrics.py`) | ✅ | Brier score, BSS, reliability table |
| POP 리포트 (`core/report.py`) | ✅ | parquet 로드·align·소스×6h 버킷 지표 |
| `run_pop_report.py` | ✅ | CLI 리포트 + `reports/` reliability CSV |
| 강수 이진 변환 (`precip.py`) | ✅ | ≥0.1mm → 1/0 (Brier용) |
| Open-Meteo 전향 적재 | ✅ | Forecast API POP·기온 (`--collect`) |
| Open-Meteo legacy | ✅ | Previous Runs 기온 백테스트 (`--collect-legacy`) |
| ASOS 실측 (`asos.py`) | ✅ | 기온·강수량 → `ground_truth_asos` (API 별도 신청) |
| KMA 적재 (`kma.py`) | ✅ | TMP/POP/PCP, `--collect` / `--backfill` |
| KMA 진단 (`kma_probe.py`) | ✅ | 키 검증 + 아카이브 깊이 |
| 첫 슬라이스 (`run_slice.py`) | ✅ | `--truth asos\|proxy`, ECMWF MAE |
| 임시 대시보드 (`app.py`) | ✅ | Streamlit parquet 뷰어 (`pip install -e ".[ui]"`) |
| **POP end-to-end (실데이터)** | ⏳ | ASOS parquet 적재 + cron 누적 필요 |
| KMA vs 글로벌 MAE 비교 표 | ⏳ | 기온 중심 확장 |
| CI (ruff + pytest) | ✅ | GitHub Actions, 70개 오프라인 테스트 |

### 운영 체크리스트

| 단계 | 명령 | 비고 |
|---|---|---|
| KMA 예보 | `python -m src.sources.kma --collect` | 3h마다 cron |
| KMA 소급 | `python -m src.sources.kma --backfill` | 일 1회 |
| 글로벌 POP | `python -m src.sources.openmeteo --collect` | UTC 00/06/12/18 직후 |
| ASOS 실측 | `python -m src.sources.asos --collect` | [ASOS API 활용신청](https://www.data.go.kr/data/15057210/openapi.do) 별도 필요 |
| POP 리포트 | `python run_pop_report.py` | ASOS·POP parquet 필요 |
| 대시보드 | `streamlit run app.py` | 내부 확인용 |

### KMA 아카이브 진단 (2026-07-03)

`getVilageFcst`는 **최근 약 3일치** 발표 예보만 조회 가능 → **cron 적재 필요** (`python -m src.sources.kma --collect`).

## 왜 `previous_day0`가 정답이 아닌가

Open-Meteo Previous Runs API의 `temperature_2m_previous_day0`(라벨: `openmeteo_self_proxy`)는 **같은 ECMWF 모델이 과거에 발표한 단기예보**입니다. 실제 관측이 아닙니다.

| 구분 | `openmeteo_self_proxy` | `ground_truth_asos` |
|---|---|---|
| 성격 | 모델 자기 예보 (과거 run) | 지상관측 실측 |
| 용도 | 파이프라인 스모크·자기일관성 | **실제 검증 정답** |
| 한계 | “맞췄다” ≠ “실제로 맞췄다” | API 키·D-1 지연 필요 |

따라서 `run_slice.py` 기본값은 `--truth asos`이며, 프록시는 키 없음·API 실패 시에만 fallback합니다. **POP Brier 검증에는 ASOS 강수 이진 실측이 필수**입니다.

## Open-Meteo 리드타임 근사 (`lead_time_h`)

Open-Meteo Previous Runs의 `temperature_2m_previous_day1` / `previous_day2`는 표준 스키마에 **`lead_time_h=24` / `48`** 으로 적재되지만, 이 값은 **정확한 리드타임이 아니라 근사 라벨**입니다.

| API 필드 | `lead_time_h` (근사) | 실제 의미 |
|---|---|---|
| `previous_day1` | 24 | **발표일이 1일 전**인 ECMWF run의 예보 (`days_ahead=1`) |
| `previous_day2` | 48 | **발표일이 2일 전**인 run의 예보 (`days_ahead=2`) |

각 run의 발표시각(UTC 00/06/12/18Z 등)에 따라 `valid_time`까지의 실제 리드는 대략 **24~47h**(day1), **48~71h**(day2) 구간에 걸칩니다. 스키마 안정성을 위해 컬럼명·값은 유지합니다.

**KMA 단기예보와 직접 비교**할 때는 `lead_time_h` 숫자를 1:1로 맞추지 말고, **발표일 차이 `days_ahead`**(Open-Meteo dayN ↔ KMA issue_date가 N일 앞선 슬롯)로 버킷팅·해석하세요. 동일 `valid_time`·`days_ahead`끼리 MAE/Brier를 계산하는 것이 맞습니다.

## 왜 Previous Runs로 POP을 못 쓰는가

Open-Meteo **Previous Runs API**는 `precipitation_probability_previous_dayN` 필드를 문서상 제공하지만, 실측 확인 시 **전 구간 null**입니다. 강수확률은 모델 출력에서 **파생 계산되는 변수**로, Previous Runs 아카이브에는 포함되지 않습니다.

| 용도 | API | POP |
|---|---|---|
| 기온 백테스트·`run_slice` | Previous Runs (`previous_dayN`) | ❌ |
| POP 검증·자체 아카이브 | **Forecast API** (전향 `--collect`) | ✅ |

따라서 KMA `--collect`와 동일하게 **수집 시점의 Forecast API 응답을 parquet에 쌓아** POP 검증용 자체 아카이브를 만듭니다.

### Open-Meteo 전향 예보 적재

```bash
python -m src.sources.openmeteo --collect
```

- **Forecast API** (`api.open-meteo.com/v1/forecast`): `temperature_2m`, `precipitation_probability`
- 모델: `ecmwf_ifs025` → `openmeteo_ecmwf`, `gfs_seamless` → `openmeteo_gfs`
- `issue_time` = **수집 시각(UTC, 시간 단위 절사)** — 실제 모델 init(00/06/12/18Z)과 다를 수 있는 근사
- `lead_time_h` = `valid_time - issue_time` (정수 시간, `< 1` 제외)
- **비교 기산일** = `issue_date` = `issue_time`의 UTC 날짜(수집 시작일)
- POP은 **0~100 % 그대로** 저장 (`/100`은 지표 호출부 책임)

저장 경로: `data/parquet/source=openmeteo_ecmwf|openmeteo_gfs/issue_date=YYYY-MM-DD/`

#### crontab 예시 (Open-Meteo 전향)

글로벌 모델 갱신(00/06/12/18 UTC) 직후 수집을 권장합니다.

```cron
# UTC 00/06/12/18시 15분 후 — 전향 예보 아카이브
15 0,6,12,18 * * * cd /path/weather-verify && .venv/bin/python -m src.sources.openmeteo --collect >> logs/openmeteo_collect.log 2>&1
```

legacy Previous Runs 적재(기온 백테스트용, POP 없음): `python -m src.sources.openmeteo --collect-legacy`

### POP 검증 리포트

```bash
python run_pop_report.py
python run_pop_report.py --start 2026-07-01 --end 2026-07-03
python run_pop_report.py --truth-mode point   # 정각 1시간 (기본: window_3h)
```

KMA·글로벌 POP vs ASOS 강수 이진 — 소스×6h 리드버킷별 Brier/BSS. `reports/` 에 reliability CSV.

## 알려진 한계

### ASOS 강수(`rn`) 결측

ASOS 시간자료 API는 `rn`(강수량)과 `rnQcflag`(품질검사: 0=정상, 1=오류, 9=결측)를 함께 제공합니다. `asos.py`는 **`rnQcflag==0`일 때만** 빈 `rn`을 무강수(0.0)로 해석하고, 오류·결측(1, 9)은 **행 자체를 생략**합니다.

`rnQcflag`가 응답에 없고 `rn`도 비어 있으면 무강수와 결측을 구분할 수 없어 **강수 행을 생략**합니다. 예전처럼 무조건 0.0으로 채우면 실제 강수 시각이 빠져 **강수 빈도가 과소추정**됩니다.

## 설계 원칙

1. **숫자는 코드가, 말은 LLM이** — 결정론적 pandas 처리
2. **위험한 계산은 `core/`에 격리** — 네트워크·파일 의존성 0
3. **API 지저분함은 `sources/`에 격리**

## 프로젝트 구조

```
weather-verify/
├── pyproject.toml             # 의존성, pytest, ruff
├── .github/workflows/ci.yml
├── src/
│   ├── schema.py
│   ├── core/                  # align, metrics, precip, report
│   └── sources/               # asos, kma, store, openmeteo, kma_auth, kma_probe
├── tests/
│   ├── fixtures/
│   ├── test_align.py
│   ├── test_app.py
│   ├── test_asos.py
│   ├── test_kma.py
│   ├── test_kma_backfill.py
│   ├── test_kma_auth.py
│   ├── test_metrics.py
│   ├── test_openmeteo.py
│   ├── test_precip.py
│   ├── test_pop_report.py
│   ├── test_prob_metrics.py
│   └── test_schema.py
├── data/                      # KMA 적재 (gitignore)
├── app.py                     # 임시 Streamlit 대시보드
├── run_slice.py
└── run_pop_report.py
```

## 요구 사항

- Python 3.10+
- 라이브 API: 공공데이터포털 키 (KMA·ASOS)

## 설치

```bash
git clone https://github.com/JamesRhee1/weather-verify.git
cd weather-verify
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 사용법

### Open-Meteo 교차검증 슬라이스

```bash
python run_slice.py                  # 기본: ASOS 실측 정답
python run_slice.py --truth proxy    # openmeteo_self_proxy (자기일관성만)
```

### KMA API 진단 · 적재

```bash
cp .env.example .env   # KMA_API_KEY_DECODING 설정
python -m src.sources.kma_probe
python -m src.sources.kma --collect    # 최신 발표 1건
python -m src.sources.kma --backfill   # 오늘~2일 전 미저장 슬롯 소급
```

저장 경로: `data/parquet/source=kma_vilage_fcst/issue_date=YYYY-MM-DD/`

#### crontab 예시 (KMA)

```cron
# 발표 10분 후 (02,05,08,11,14,17,20,23시 KST)
10 2,5,8,11,14,17,20,23 * * * cd /path/weather-verify && .venv/bin/python -m src.sources.kma --collect >> logs/collect.log 2>&1
# 일 1회 안전망 — 누락 슬롯 소급
0 6 * * * cd /path/weather-verify && .venv/bin/python -m src.sources.kma --backfill >> logs/backfill.log 2>&1
```

### ASOS 실측 적재

전일(D-1) 시간자료를 `source=ground_truth_asos` 파티션에 저장합니다.

```bash
python -m src.sources.asos --collect
```

저장 경로: `data/parquet/source=ground_truth_asos/issue_date=YYYY-MM-DD/`

#### crontab 예시 (ASOS)

```cron
# 매일 07:00 — 전일 실측 확정 후 적재
0 7 * * * cd /path/weather-verify && .venv/bin/python -m src.sources.asos --collect >> logs/asos_collect.log 2>&1
```

### 개발 (lint · test)

```bash
ruff check . && ruff format .
pytest -q                    # 기본: network 마커 제외
pytest -q -m network         # 라이브 API 테스트만 (로컬)
```

### 임시 대시보드 (Streamlit)

```bash
pip install -e ".[ui]"
streamlit run app.py
```

## 테스트

현재 **70개** 오프라인 테스트. CI는 `ruff check`, `ruff format --check`, `pytest -m "not network"` 를 실행합니다.

## 로드맵 (최종 목표 중심)

### Phase 1 — 기반 ✅ (대부분 완료)

- [x] 표준 스키마, align, MAE/RMSE
- [x] KMA 적재 (POP/PCP/TMP), ASOS 실측
- [x] Brier / BSS / reliability (`core/metrics.py`)
- [x] 강수 이진 변환 (`core/precip.py`)

### Phase 2 — POP 검증 파이프라인 ⏳ (코드 완료, 데이터 적재 중)

- [x] KMA·Open-Meteo POP parquet 적재 경로
- [x] ASOS 강수 이진 align (`core/report.py`, window_3h / point)
- [x] 리드타임별 **Brier score · BSS · reliability** (`run_pop_report.py`)
- [x] 임시 Streamlit 대시보드 (`app.py`)
- [ ] ASOS cron 적재 + 충분한 기간 누적 후 **실데이터 리포트 검증**
- [ ] KMA vs 글로벌 확률예보 비교 차트·자동 리포트

### Phase 3 — 확장

- [ ] 기온 MAE: KMA vs 글로벌 비교 표
- [ ] 정식 대시보드 UI (현재 `app.py`는 임시 뷰어)

## 라이선스

MIT — [LICENSE](LICENSE) 참고
