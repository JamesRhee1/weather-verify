# weather-verify

한국 기상청 예보 vs 글로벌 모델 예보를 **실측(정답값) 대비** 정량 평가하는 시스템입니다.

**최종 목표:** KMA 강수확률(POP) 예보 vs 글로벌 확률예보의 **Brier score / reliability diagram** 비교.

현재는 서울 1지점 · 기온·강수 기초 파이프라인과 첫 수직 슬라이스(기온 MAE)까지 구현되어 있습니다.

## 현재 구현 상태

| 구성요소 | 상태 | 설명 |
|---|---|---|
| 표준 스키마 (`src/schema.py`) | ✅ | long-format 6컬럼 규약 |
| 정렬 (`src/core/align.py`) | ✅ | 예보·정답 join, 순수 pandas |
| 연속형 지표 (`metrics.py`) | ✅ | MAE, RMSE, 리드타임별 MAE |
| 확률 지표 (`metrics.py`) | ✅ | Brier score, BSS, reliability table |
| 강수 이진 변환 (`precip.py`) | ✅ | ≥0.1mm → 1/0 (Brier용) |
| Open-Meteo 소스 | ✅ | ECMWF 예보 + `openmeteo_self_proxy` |
| ASOS 실측 (`asos.py`) | ✅ | 기온·강수량 → `ground_truth_asos` |
| KMA 적재 (`kma.py`) | ✅ | TMP/POP/PCP parquet·raw 저장 |
| KMA 진단 (`kma_probe.py`) | ✅ | 키 검증 + 아카이브 깊이 |
| 첫 슬라이스 (`run_slice.py`) | ✅ | `--truth asos\|proxy`, ECMWF MAE |
| **POP Brier 파이프라인 end-to-end** | ⏳ | 적재 데이터 + align + 리포트 |
| KMA vs 글로벌 MAE 비교 표 | ⏳ | 기온 중심 확장 |
| 대시보드·LLM | ⏳ | 범위 밖 |
| CI (ruff + pytest) | ✅ | GitHub Actions, `@network` 제외 |

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
│   ├── core/                  # align, metrics, precip
│   └── sources/               # asos, kma, openmeteo, kma_auth, kma_probe
├── tests/
│   ├── fixtures/
│   ├── test_align.py
│   ├── test_asos.py
│   ├── test_kma.py
│   ├── test_kma_auth.py
│   ├── test_metrics.py
│   ├── test_precip.py
│   ├── test_prob_metrics.py
│   └── test_schema.py
├── data/                      # KMA 적재 (gitignore)
└── run_slice.py
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
python -m src.sources.kma --collect
```

### 개발 (lint · test)

```bash
ruff check . && ruff format .
pytest -q                    # 기본: network 마커 제외
pytest -q -m network         # 라이브 API 테스트만 (로컬)
```

## 테스트

현재 **44개** 오프라인 테스트. CI는 `ruff check`, `ruff format --check`, `pytest -m "not network"` 를 실행합니다.

## 로드맵 (최종 목표 중심)

### Phase 1 — 기반 ✅ (대부분 완료)

- [x] 표준 스키마, align, MAE/RMSE
- [x] KMA 적재 (POP/PCP/TMP), ASOS 실측
- [x] Brier / BSS / reliability (`core/metrics.py`)
- [x] 강수 이진 변환 (`core/precip.py`)

### Phase 2 — POP 검증 파이프라인 ⏳

- [ ] 적재된 KMA POP + ASOS 강수 이진 → align
- [ ] 글로벌 모델 강수확률 소스 연동 (Open-Meteo 등)
- [ ] 리드타임별 **Brier score · BSS · reliability diagram** 리포트
- [ ] KMA vs 글로벌 확률예보 비교 표/차트

### Phase 3 — 확장

- [ ] 기온 MAE: KMA vs 글로벌 비교 표
- [ ] 대시보드 UI

## 라이선스

MIT — [LICENSE](LICENSE) 참고
