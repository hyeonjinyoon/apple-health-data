# Apple Health → Mac mini → Grafana

iPhone의 Apple 건강 데이터를 직접 만든 iOS 앱(**HealthSync**)으로 Mac mini에 보내고,
ClickHouse에 저장해 Grafana 대시보드로 보는 프로젝트입니다.

```
iPhone ─ HealthSync 앱 (SwiftUI + HealthKit)
   │   HTTP POST /api/v1/ingest · Bearer 토큰 · deflate 압축
   │   집 Wi-Fi(LAN) 또는 Tailscale(외부에서도)
   ▼
Mac mini
   ├─ health-ingest   FastAPI, :8787, launchd 상주 (local.apple-health.ingest)
   ├─ ClickHouse      DB `health` (실데이터), `health_demo` (데모)
   └─ Grafana         폴더 "Apple Health" — 대시보드 2종
```

## 무엇이 수집되나

| 구분 | 타입 | 전송 방식 |
|---|---|---|
| 심장 | 심박수, 안정시/걷기 평균 심박, HRV, 혈중 산소, 호흡수, VO₂ max, 높은/낮은 심박·불규칙 리듬 알림 | 원본 샘플 |
| 신체 | 체중, BMI, 체지방률, 제지방량, 신장, 체온, 수면 중 손목 체온, 혈압, 혈당 | 원본 샘플 |
| 이동성 | 걷기 속도, 보폭, 걷기 비대칭 | 원본 샘플 |
| 소리 | 환경 소음, 헤드폰 음량 | 원본 샘플 |
| 수면·마음 | 수면 단계(코어/깊은/렘/깸/침대), 마음 챙기기 | 원본 샘플 |
| 활동 | 걸음 수, 걷기·달리기/사이클 거리, 활동·휴식 에너지, 오른 층수, 운동·스탠드 시간, 햇빛 노출 | **시간별 합계** |
| 운동 | 모든 운동 기록 (종류, 시간, kcal, 거리, 평균/최고 심박) | 원본 |

> **왜 활동 데이터는 시간별 합계인가?** 걸음 수·거리·칼로리는 iPhone과 Apple Watch가 **동시에**
> 기록합니다. 원본을 그대로 더하면 두 배가 됩니다. 앱은 `HKStatisticsCollectionQuery`로
> 건강 앱과 똑같이 소스 우선순위로 중복을 제거한 **시간별 합계**를 보냅니다.

동기화는 **증분 방식**입니다(HealthKit anchored query). 첫 동기화 때만 전체 기록을 보내고,
이후에는 바뀐 것만 보냅니다. 건강 앱에서 지운 기록은 서버에서도 지워집니다.

---

## 1. Mac mini 설치

요구사항: macOS, [uv](https://docs.astral.sh/uv/), 로컬 ClickHouse(HTTP 8123 / native 9000),
Homebrew Grafana + `grafana-clickhouse-datasource` 플러그인.
기본값은 이 Mac 환경(`~/.local/clickhouse`, `~/.local/grafana/provisioning`)에 맞춰져 있고,
다른 환경이면 환경변수로 바꿀 수 있습니다(`deploy/install.py` 상단 `CONFIG` 참고).

```bash
make demo        # = python3 deploy/install.py --demo   (데모 데이터 포함)
# 또는
make install     # 실데이터용만
```

설치 스크립트는 여러 번 실행해도 안전합니다(코드 업데이트 후 재배포할 때도 같은 명령).

1. `~/.local/apple-health/` 아래에 서버 코드, 비밀값, 로그를 둡니다.
   launchd 서비스는 `~/Documents`에 접근할 수 없어서 이 위치를 씁니다.
2. 비밀값은 처음 한 번만 생성합니다(`secrets/`, 권한 600).
   - `api_token`: iPhone 앱이 쓰는 토큰
   - `ch_writer_password` / `ch_reader_password`: ClickHouse 전용 계정 비밀번호
3. ClickHouse 계정을 만듭니다. 둘 다 localhost에서만 접속됩니다.
   - `health_writer`: SELECT, INSERT만 허용
   - `health_reader`: SELECT만 허용
4. 스키마 마이그레이션을 적용하고, LaunchAgent `local.apple-health.ingest`(KeepAlive, 0.0.0.0:8787)를 등록합니다.
5. Grafana에 데이터소스 `ClickHouse (Apple Health)`와 대시보드를 프로비저닝합니다.
   기존 Grafana 설정은 건드리지 않고 파일만 추가하며, 변경이 있을 때만 Grafana를 재시작합니다.

## 2. iPhone 앱 설치

**처음 한 번**
```bash
make -C ios signing TEAM=<Apple Developer Team ID> PREFIX=com.<yourname>
```
이 명령은 `ios/Config/Signing.xcconfig`를 만들며, 이 파일은 git에서 제외됩니다.

**설치**: iPhone을 케이블로 연결하고 *설정 › 개인정보 보호 및 보안 › 개발자 모드*를 켭니다. 그다음 둘 중 하나로 설치합니다.
```bash
make ios-device              # 명령줄로 빌드 후 설치
# 또는
make -C ios open             # Xcode에서 ▶︎ Run
```
유료 개발자 계정이면 서명이 1년간 유효합니다. 무료 계정은 7일마다 다시 설치해야 합니다.

**서버 연결**
```bash
make qr                      # = python3 deploy/show_config.py
```
화면의 QR 코드를 **iPhone 기본 카메라**로 찍으면 HealthSync가 열리고 서버 주소와 토큰이 자동으로 입력됩니다.
- 기본값은 **Tailscale 주소**입니다(집 밖에서도 동기화됨). 집 Wi-Fi 주소를 쓰려면 `--lan` 옵션을 붙입니다.
- 앱의 ⚙︎ 설정에서 직접 입력하고 "연결 테스트"를 눌러도 됩니다.

**건강 데이터 권한 (중요)**

권한 시트는 여러 단계로 나옵니다.
1. **Select All (모두 선택)** → **Continue**
2. *"How much data would you like to share?"* 화면에서 반드시 **All Recorded Data and Future Data (기록된 모든 데이터)**를 고릅니다.
   "Past 30 Days"를 고르면 과거 기록이 전송되지 않습니다.
3. **Allow**

권한은 나중에 *설정 › 건강 › 데이터 접근 및 기기 › HealthSync*에서 바꿀 수 있습니다.

**백그라운드 동기화**: *설정 › 일반 › 백그라운드 앱 새로고침*에서 HealthSync를 켜세요.
켜 두면 새 건강 데이터가 생길 때 iOS가 앱을 깨워서 보냅니다(대략 시간 단위).
앱을 열거나 목록을 아래로 당겨도 즉시 동기화됩니다.

> 첫 동기화는 기록 양에 따라 몇 분 걸릴 수 있습니다. 앱을 켜 둔 채 기다려 주세요.
> 중간에 끊겨도 진행 위치가 저장되므로 다음 동기화에서 이어집니다.
> iPhone이 잠겨 있으면 iOS가 건강 데이터를 암호화하므로 그동안은 동기화되지 않습니다.

## 3. Grafana 대시보드

`http://<mac-mini>:8180` → Dashboards → **Apple Health** 폴더

| 대시보드 | 내용 |
|---|---|
| **Apple Health** (`/d/apple-health-overview`) | 오늘 요약, 일별 걸음·에너지·운동·스탠드·층수·거리, 시간대별 걸음, 심박(평균+범위), 안정시 심박, HRV(7일 평균), SpO₂, 호흡수, VO₂ max, 수면 단계·추세·기록표, 체중·체지방·BMI, 운동 기록·종류별 시간, 동기화 상태 |
| **Apple Health — 데이터 탐색기** (`/d/apple-health-explorer`) | 드롭다운에서 고른 아무 타입이나 원본 값·일별 평균·소스별 개수·최근 샘플 표로 표시 |

- **데모 데이터 보기**: URL 끝에 `?var-db=health_demo`를 붙입니다(60일치 가상 데이터).
  필요 없으면 `DROP DATABASE health_demo`로 지웁니다.
- 일/주 경계는 시스템 시간대(`Asia/Seoul`)를 기준으로 합니다. 수면은 **기상일** 기준이며, 같은 밤을
  여러 앱이 기록했다면 수면 시간이 가장 긴 소스 하나만 사용합니다.
- 대시보드는 코드(`grafana/build_dashboards.py`)로 만듭니다. 수정했으면 `make dashboards`를 실행하세요.
  30초 안에 자동 반영됩니다.

## 운영

```bash
make status      # 서비스 상태 + /health
make logs        # 서버 로그 (~/.local/apple-health/logs/ingest.log)
make install     # 코드 변경 후 재배포
make uninstall   # 서비스·프로비저닝 제거 (데이터는 유지)
```

## 보안

- 모든 업로드는 Bearer 토큰으로 인증합니다. ClickHouse는 `127.0.0.1`에만 열려 있고, 쓰기·읽기 전용 계정으로 권한을 나눴습니다.
- 앱은 사설망(192.168.x, 10.x, 172.16–31.x), Tailscale(100.64–127.x, `*.ts.net`), `*.local`
  주소에만 `http://`를 허용합니다. 공용 주소는 `https://`만 받습니다.
- 토큰은 iPhone 키체인(이 기기 전용)에 저장되고, Mac에서는 `secrets/`(600)에 있습니다.
  토큰을 바꾸려면 `~/.local/apple-health/secrets/api_token`을 지우고 `make install`과 `make qr`을 다시 실행하세요.

## 문제 해결

| 증상 | 확인 |
|---|---|
| 앱 "연결 테스트" 실패 | `make status`로 서비스가 도는지 확인합니다. iPhone이 같은 Wi-Fi 또는 Tailscale에 연결돼 있는지도 확인합니다. LAN IP는 바뀔 수 있으니 Tailscale 주소를 권장합니다 |
| 401 | 토큰 불일치입니다. `make qr`로 다시 설정하세요 |
| 과거 기록이 없음 | 권한에서 "Past 30 Days"를 골랐을 수 있습니다. 설정 › 건강 › 데이터 접근 및 기기 › HealthSync에서 바꾼 뒤, 앱 설정의 "전체 기록 다시 보내기"를 누르세요 |
| 백그라운드에서 안 보냄 | 백그라운드 앱 새로고침이 켜져 있는지, 저전력 모드가 아닌지 확인하세요 |
| 대시보드가 비어 있음 | "동기화 상태" 행에서 마지막 수신 시각을 확인하세요. 데모는 `?var-db=health_demo`로 봅니다 |

## 개발

```
server/     FastAPI ingest 서버 (app/), ClickHouse 마이그레이션, pytest
grafana/    대시보드 생성기 (build_dashboards.py) → dashboards/*.json
deploy/     install.py (Mac mini 설치), show_config.py (앱 설정 QR)
ios/        HealthSync — XcodeGen(project.yml), 앱/단위/UI 테스트
tools/      demo_data.py (가짜 iPhone 클라이언트), check_dashboards.py (대시보드 SQL 검증), make_app_icon.py
docs/api.md 앱 ↔ 서버 API 계약
```

```bash
make test                    # 서버 단위 + 실제 ClickHouse 통합 테스트(임시 DB) + 대시보드 SQL 44개 검증
make ios-test                # iOS 단위 테스트 (시뮬레이터)
```

**전체 E2E (시뮬레이터 HealthKit → 앱 → 서버)**: Debug 빌드에는 `--seed-demo-health-data`
실행 인자가 있습니다. 이 인자를 주면 시뮬레이터 HealthKit에 정해진 데이터를 쓴 뒤 동기화합니다.
UI 테스트가 권한 시트를 자동으로 통과시킵니다.

```bash
# 임시 DB에 연결한 ingest 서버를 따로 띄운 뒤:
TEST_RUNNER_HEALTHSYNC_IT_URL=http://127.0.0.1:8789 TEST_RUNNER_HEALTHSYNC_IT_TOKEN=<token> \
  xcodebuild test -project ios/HealthSync.xcodeproj -scheme HealthSync \
  -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HealthSyncUITests
```
