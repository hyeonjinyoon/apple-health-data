import HealthKit

/// A HealthKit quantity type and how it is sent to the server.
struct QuantitySpec {
    /// Wire name, e.g. `heart_rate` (the `type` column in ClickHouse).
    let key: String
    let title: String
    let identifier: HKQuantityTypeIdentifier
    let unit: HKUnit

    var type: HKQuantityType { HKQuantityType(identifier) }
}

struct CategorySpec {
    let key: String
    let title: String
    let identifier: HKCategoryTypeIdentifier

    var type: HKCategoryType { HKCategoryType(identifier) }
}

/// Every type the app reads. Keys must match the Grafana dashboards (grafana/build_dashboards.py).
enum HealthCatalog {
    static let bpm = HKUnit.count().unitDivided(by: .minute())
    static let kg = HKUnit.gramUnit(with: .kilo)

    /// Discrete measurements: every sample is uploaded as-is (anchored, incremental).
    static let discrete: [QuantitySpec] = [
        .init(key: "heart_rate", title: "심박수", identifier: .heartRate, unit: bpm),
        .init(key: "resting_heart_rate", title: "안정시 심박수", identifier: .restingHeartRate, unit: bpm),
        .init(key: "walking_heart_rate_average", title: "걷기 평균 심박수", identifier: .walkingHeartRateAverage, unit: bpm),
        .init(key: "heart_rate_variability", title: "심박 변이도", identifier: .heartRateVariabilitySDNN, unit: .secondUnit(with: .milli)),
        .init(key: "oxygen_saturation", title: "혈중 산소", identifier: .oxygenSaturation, unit: .percent()),
        .init(key: "respiratory_rate", title: "호흡수", identifier: .respiratoryRate, unit: bpm),
        .init(key: "vo2_max", title: "VO₂ max", identifier: .vo2Max, unit: HKUnit(from: "ml/kg*min")),
        .init(key: "body_mass", title: "체중", identifier: .bodyMass, unit: kg),
        .init(key: "body_mass_index", title: "BMI", identifier: .bodyMassIndex, unit: .count()),
        .init(key: "body_fat_percentage", title: "체지방률", identifier: .bodyFatPercentage, unit: .percent()),
        .init(key: "lean_body_mass", title: "제지방량", identifier: .leanBodyMass, unit: kg),
        .init(key: "height", title: "신장", identifier: .height, unit: .meter()),
        .init(key: "walking_speed", title: "걷기 속도", identifier: .walkingSpeed, unit: .meter().unitDivided(by: .second())),
        .init(key: "walking_step_length", title: "보폭", identifier: .walkingStepLength, unit: .meter()),
        .init(key: "walking_asymmetry", title: "걷기 비대칭", identifier: .walkingAsymmetryPercentage, unit: .percent()),
        .init(key: "blood_pressure_systolic", title: "수축기 혈압", identifier: .bloodPressureSystolic, unit: .millimeterOfMercury()),
        .init(key: "blood_pressure_diastolic", title: "이완기 혈압", identifier: .bloodPressureDiastolic, unit: .millimeterOfMercury()),
        .init(key: "blood_glucose", title: "혈당", identifier: .bloodGlucose, unit: HKUnit(from: "mg/dL")),
        .init(key: "body_temperature", title: "체온", identifier: .bodyTemperature, unit: .degreeCelsius()),
        .init(key: "sleeping_wrist_temperature", title: "수면 중 손목 체온", identifier: .appleSleepingWristTemperature, unit: .degreeCelsius()),
        .init(key: "environmental_audio_exposure", title: "환경 소음", identifier: .environmentalAudioExposure, unit: .decibelAWeightedSoundPressureLevel()),
        .init(key: "headphone_audio_exposure", title: "헤드폰 음량", identifier: .headphoneAudioExposure, unit: .decibelAWeightedSoundPressureLevel()),
    ]

    /// Cumulative quantities: iPhone and Apple Watch both record these, so raw samples overlap.
    /// We upload hourly sums from HKStatisticsCollectionQuery, which de-duplicates sources.
    static let cumulative: [QuantitySpec] = [
        .init(key: "step_count", title: "걸음 수", identifier: .stepCount, unit: .count()),
        .init(key: "distance_walking_running", title: "걷기·달리기 거리", identifier: .distanceWalkingRunning, unit: .meter()),
        .init(key: "distance_cycling", title: "사이클 거리", identifier: .distanceCycling, unit: .meter()),
        .init(key: "active_energy", title: "활동 에너지", identifier: .activeEnergyBurned, unit: .kilocalorie()),
        .init(key: "basal_energy", title: "휴식 에너지", identifier: .basalEnergyBurned, unit: .kilocalorie()),
        .init(key: "flights_climbed", title: "오른 층수", identifier: .flightsClimbed, unit: .count()),
        .init(key: "exercise_time", title: "운동 시간", identifier: .appleExerciseTime, unit: .minute()),
        .init(key: "stand_time", title: "스탠드 시간", identifier: .appleStandTime, unit: .minute()),
        .init(key: "time_in_daylight", title: "햇빛 노출 시간", identifier: .timeInDaylight, unit: .minute()),
    ]

    static let categories: [CategorySpec] = [
        .init(key: "sleep_analysis", title: "수면", identifier: .sleepAnalysis),
        .init(key: "mindful_session", title: "마음 챙기기", identifier: .mindfulSession),
        .init(key: "high_heart_rate_event", title: "높은 심박수 알림", identifier: .highHeartRateEvent),
        .init(key: "low_heart_rate_event", title: "낮은 심박수 알림", identifier: .lowHeartRateEvent),
        .init(key: "irregular_heart_rhythm_event", title: "불규칙한 심장 리듬 알림", identifier: .irregularHeartRhythmEvent),
    ]

    static var sampleTypes: [HKSampleType] {
        discrete.map(\.type) + cumulative.map(\.type) + categories.map(\.type) + [HKObjectType.workoutType()]
    }

    static var readTypes: Set<HKObjectType> { Set(sampleTypes) }
}
