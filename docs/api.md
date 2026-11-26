# Ingest API 계약 (v1)

iPhone 앱(HealthSync) → Mac mini(health-ingest) 사이의 전송 규격입니다.

## 인증

모든 `/api/*` 요청은 `Authorization: Bearer <HEALTH_API_TOKEN>` 헤더가 필요합니다.
토큰은 설치 스크립트가 `~/.local/apple-health/secrets/api_token` 에 생성합니다.

## 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| GET | `/health` | 서버·ClickHouse 상태 (인증 불필요) |
| GET | `/api/v1/ping` | 토큰 확인용 (앱의 "연결 테스트") |
| POST | `/api/v1/ingest` | 데이터 업로드 |
| GET | `/api/v1/summary` | 서버에 저장된 타입별 개수/최신 시각 |

## POST /api/v1/ingest

- `Content-Type: application/json`
- 선택: `Content-Encoding: deflate` (raw DEFLATE 또는 zlib) 또는 `gzip`
- 모든 시각은 ISO-8601 (타임존 포함, 예: `2026-11-25T12:00:00.123Z`)

```json
{
  "device_id": "identifierForVendor UUID",
  "device_name": "iPhone 15 Pro",
  "app_version": "1.0.0 (1)",
  "samples": [
    {
      "uuid": "6F0C...",
      "type": "heart_rate",
      "value": 72.0,
      "unit": "count/min",
      "start": "2026-11-25T12:00:00.000Z",
      "end": "2026-11-25T12:00:00.000Z",
      "category": "",
      "source": "Apple Watch",
      "source_bundle": "com.apple.health.81A1...",
      "device": "Watch7,1",
      "metadata": {"HKMetadataKeyHeartRateMotionContext": "1"}
    }
  ],
  "workouts": [
    {
      "uuid": "A1B2...",
      "activity_type": "running",
      "start": "2026-11-25T07:00:00Z",
      "end": "2026-11-25T07:30:00Z",
      "duration_s": 1800,
      "energy_kcal": 310.5,
      "distance_m": 5020.0,
      "avg_heart_rate": 152.0,
      "max_heart_rate": 178.0,
      "source": "Apple Watch",
      "source_bundle": "com.apple.health...",
      "device": "Watch7,1",
      "metadata": {}
    }
  ],
  "stats": [
    {"type": "step_count", "start": "2026-11-25T03:00:00Z", "end": "2026-11-25T04:00:00Z", "value": 1234, "unit": "count"}
  ],
  "deleted": ["6F0C..."]
}
```

### 필드 의미

- **samples**: HealthKit의 개별 샘플 (심박, HRV, 체중, 수면 등). `uuid` 기준으로 멱등. 같은 샘플을 여러 번 보내도 1건으로 저장됩니다.
  - 카테고리 샘플(수면 등)은 `value` = HealthKit 원시 정수값, `category` = 사람이 읽을 수 있는 라벨 (`asleep_core`, `asleep_deep`, `asleep_rem`, `awake`, `in_bed` …)
  - 퍼센트 타입(`oxygen_saturation`, `body_fat_percentage`)은 0~1 비율값
- **workouts**: HKWorkout. `uuid` 기준 멱등.
- **stats**: 누적형 지표(걸음 수, 거리, 활동/기초 에너지, 오른 층수, 운동/스탠드 시간)의 **시간 단위 합계**.
  HealthKit `HKStatisticsCollectionQuery` 결과라 iPhone/Watch 중복이 제거되어 있습니다.
  `(type, start)` 기준으로 마지막 값이 덮어씁니다.
- **deleted**: HealthKit에서 삭제된 샘플/운동의 UUID. 서버가 tombstone을 기록해 대시보드에서 제외합니다.

### 응답

```json
{"samples": 1200, "workouts": 2, "stats": 48, "deleted": 3}
```

- `401` 토큰 오류, `413` 본문 과대, `422` 스키마 오류, `502` ClickHouse 오류
