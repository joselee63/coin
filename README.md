# 암호화폐 섹터 차트 (웹 버전)

주식 차트와 같은 화면으로, 테마별 코인의 일봉 차트·추세선·결과표를 보여줍니다.
매일 09:07에 자동 갱신되고, 페이지의 **↻ 업데이트** 버튼으로 바로 갱신할 수도 있습니다.

- 시세: **바이낸스 현물 USDT 마켓** → 없으면 **크라켄 USD 마켓** (가격 단위: 달러)
  - GitHub 서버는 미국에 있고 바이낸스는 미국 접속을 막을 수 있습니다. 막히면 자동으로 크라켄만 씁니다.
- 일봉 기준: UTC 00:00 = 한국 09:00
  - 자동 실행(매일 09:07): 막 마감된 어제 일봉까지 → 페이지에 **마감** 표시
  - 수동 실행(업데이트 버튼): 진행 중인 오늘 일봉까지 → 페이지에 **진행 중** 표시
- **거래대금 상위 20 (자동)**: 바이낸스 24시간 거래대금 순위를 매번 새로 뽑아 보여줍니다 (스테이블코인 제외).
  목록(coins.py)에 없는 코인도 여기에 잡히면 결과표에 '거래대금 상위 (목록 외)'로 나옵니다.

```
coin/
├─ build_site.py               시세 수집 · 차트 생성 · 페이지 만들기
├─ coins.py                    테마별 코인 목록 (여기만 고치면 코인 변경)
├─ requirements.txt
├─ web/index.html              웹페이지
└─ .github/workflows/daily.yml  자동 실행 일정 (매일 09:07)
```

---

## 처음 설정 (주식 차트와 같은 순서, 약 10분)

주소는 `https://joselee63.github.io/coin/` 이 됩니다. 주식 차트(`joselee63.github.io`)와 따로 돌아가서 서로 영향이 없습니다.

1. **저장소 만들기** — GitHub **+ → New repository** → 이름 `coin` → **Public** → Create
2. **파일 올리기** — 압축을 푼 `coin` 폴더 **안의** 항목 전부(`.github`, `web`, `build_site.py`, `coins.py`, `requirements.txt`, `README.md`)를
   **Add file → Upload files**로 끌어다 놓고 → **Commit changes**
   (저장소 첫 화면에 `.github`, `web`, `build_site.py`…가 바로 보여야 합니다)
3. **실행 권한** — Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save
4. **첫 실행** — Actions 탭 → **daily-build** → **Run workflow** → 3~5분 뒤 초록 체크
5. **페이지 켜기** — Settings → Pages → Branch **gh-pages** / **(root)** → Save → 1~2분 뒤 `https://joselee63.github.io/coin/`

### 업데이트 버튼 토큰
주식 차트에서 쓰던 토큰을 그대로 씁니다 (같은 브라우저면 다시 입력할 필요 없음). 토큰에 `coin` 저장소 권한만 추가하세요.

GitHub 프로필 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → 쓰던 토큰 클릭 →
**Repository access**에 `coin` 추가 → **Update** (Actions: Read and write 권한은 그대로)

---

## 평소 운영

| 상황 | 동작 |
|---|---|
| 매일 09:07 | 자동 업데이트 (어제 마감 일봉 기준) |
| 지금 시세로 보고 싶을 때 | **↻ 업데이트** → 4~6분 뒤 화면이 저절로 바뀜 (진행 중인 오늘 일봉 포함) |
| 코인 추가·삭제 | GitHub에서 `coins.py` 수정 → Commit → 다음 업데이트부터 반영 |
| 상장 폐지·심볼 변경 | 페이지 맨 아래 **점검 메모**에 "마켓이 없는 코인"으로 표시됨 → coins.py에서 수정 |

설정 바꾸기 (`build_site.py` 맨 위): 이동평균 `MA_LIST`, 차트 기간 `DISPLAY_BARS`, 추세선 `TREND_LONG`·`TREND_SHORT`
자동 섹션 개수: `coins.py`의 `AUTO_TOP_N` (0이면 끔)

## 문제 해결

| 증상 | 해결 |
|---|---|
| 배포 단계 `403` | 설정 3번(Read and write permissions) 확인 후 다시 실행 |
| `수신 성공률 … 미만` 으로 실패 | 거래소 응답 문제. 기존 페이지는 유지됨. 잠시 후 다시 실행 |
| 페이지 하단 "바이낸스 접속 제한 → 크라켄 사용" | 정상 동작. 크라켄에 없는 코인만 '마켓이 없는 코인'으로 빠지고, 거래대금 상위 자동 섹션은 생략됨 |
| 업데이트 버튼: "이 토큰에 … 권한이 없습니다" | 위 '업데이트 버튼 토큰'대로 토큰에 `coin` 저장소 추가 |
