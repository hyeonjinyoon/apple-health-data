-- Apple Health storage. `{db}` is replaced by the migration runner.
-- All timestamps are stored in UTC; dashboards convert to local time.
--
-- ReplacingMergeTree(ingested_at, deleted):
--   * re-sending the same HealthKit object (same sorting key) keeps only the newest row
--   * a row with deleted = 1 and a newer ingested_at acts as a tombstone; `FINAL` hides it

CREATE TABLE IF NOT EXISTS {db}.samples
(
    uuid          UUID,
    type          LowCardinality(String),
    value         Float64,
    unit          LowCardinality(String),
    category      LowCardinality(String),
    start_time    DateTime64(3, 'UTC'),
    end_time      DateTime64(3, 'UTC'),
    source_name   LowCardinality(String),
    source_bundle LowCardinality(String),
    device        LowCardinality(String),
    metadata      Map(String, String),
    deleted       UInt8 DEFAULT 0,
    ingested_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at, deleted)
PARTITION BY toYear(start_time)
ORDER BY (type, start_time, uuid);

CREATE TABLE IF NOT EXISTS {db}.workouts
(
    uuid           UUID,
    activity_type  LowCardinality(String),
    start_time     DateTime64(3, 'UTC'),
    end_time       DateTime64(3, 'UTC'),
    duration_s     Float64,
    energy_kcal    Nullable(Float64),
    distance_m     Nullable(Float64),
    avg_heart_rate Nullable(Float64),
    max_heart_rate Nullable(Float64),
    source_name    LowCardinality(String),
    source_bundle  LowCardinality(String),
    device         LowCardinality(String),
    metadata       Map(String, String),
    deleted        UInt8 DEFAULT 0,
    ingested_at    DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at, deleted)
PARTITION BY toYear(start_time)
ORDER BY (start_time, uuid);

-- Hourly totals of cumulative quantities (steps, energy, distance ...), computed on the
-- phone by HKStatisticsCollectionQuery so iPhone + Apple Watch overlap is de-duplicated.
CREATE TABLE IF NOT EXISTS {db}.quantity_stats
(
    type         LowCardinality(String),
    bucket_start DateTime64(3, 'UTC'),
    bucket_end   DateTime64(3, 'UTC'),
    value        Float64,
    unit         LowCardinality(String),
    ingested_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYear(bucket_start)
ORDER BY (type, bucket_start);

CREATE TABLE IF NOT EXISTS {db}.sync_log
(
    received_at       DateTime64(3, 'UTC') DEFAULT now64(3),
    device_id         String,
    device_name       String,
    app_version       String,
    client_ip         String,
    samples           UInt32,
    workouts          UInt32,
    stats             UInt32,
    deleted           UInt32,
    deleted_requested UInt32,
    min_time          Nullable(DateTime64(3, 'UTC')),
    max_time          Nullable(DateTime64(3, 'UTC')),
    duration_ms       UInt32
)
ENGINE = MergeTree
ORDER BY received_at
TTL toDateTime(received_at) + INTERVAL 400 DAY;
