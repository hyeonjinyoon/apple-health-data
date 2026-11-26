import HealthKit

/// HealthKit objects → wire payloads.
enum PayloadMapper {
    static func sample(_ s: HKQuantitySample, spec: QuantitySpec) -> SamplePayload? {
        // doubleValue(for:) raises an ObjC exception on incompatible units — never let that happen.
        guard s.quantity.is(compatibleWith: spec.unit) else { return nil }
        var meta = metadata(s.metadata)
        if s.count > 1 { meta["_series_count"] = String(s.count) }  // value is the series average
        let src = source(of: s)
        return SamplePayload(
            uuid: s.uuid.uuidString, type: spec.key, value: s.quantity.doubleValue(for: spec.unit),
            unit: spec.unit.unitString, start: s.startDate, end: s.endDate,
            source: src.name, sourceBundle: src.bundle, device: src.device, metadata: meta
        )
    }

    static func sample(_ s: HKCategorySample, spec: CategorySpec) -> SamplePayload {
        let src = source(of: s)
        return SamplePayload(
            uuid: s.uuid.uuidString, type: spec.key, value: Double(s.value), unit: "",
            start: s.startDate, end: s.endDate,
            category: spec.identifier == .sleepAnalysis ? sleepLabel(s.value) : "",
            source: src.name, sourceBundle: src.bundle, device: src.device, metadata: metadata(s.metadata)
        )
    }

    static func workout(_ w: HKWorkout) -> WorkoutPayload {
        let bpm = HealthCatalog.bpm
        let energy = w.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie())
        var distanceTypes: [HKQuantityTypeIdentifier] = [
            .distanceWalkingRunning, .distanceCycling, .distanceSwimming, .distanceWheelchair,
            .distanceDownhillSnowSports,
        ]
        if #available(iOS 18.0, *) {
            distanceTypes += [.distanceCrossCountrySkiing, .distanceRowing, .distancePaddleSports]
        }
        let distance = distanceTypes.lazy
            .compactMap { w.statistics(for: HKQuantityType($0))?.sumQuantity()?.doubleValue(for: .meter()) }
            .first
        let hr = w.statistics(for: HKQuantityType(.heartRate))
        let src = source(of: w)
        return WorkoutPayload(
            uuid: w.uuid.uuidString, activityType: activityName(w.workoutActivityType),
            start: w.startDate, end: w.endDate, durationS: w.duration,
            energyKcal: energy, distanceM: distance,
            avgHeartRate: hr?.averageQuantity()?.doubleValue(for: bpm),
            maxHeartRate: hr?.maximumQuantity()?.doubleValue(for: bpm),
            source: src.name, sourceBundle: src.bundle, device: src.device, metadata: metadata(w.metadata)
        )
    }

    static func source(of object: HKObject) -> (name: String, bundle: String, device: String) {
        let src = object.sourceRevision.source
        let dev = object.device
        return (src.name, src.bundleIdentifier, dev?.hardwareVersion ?? dev?.model ?? dev?.name ?? "")
    }

    static func metadata(_ md: [String: Any]?) -> [String: String] {
        guard let md else { return [:] }
        var out: [String: String] = [:]
        for (key, value) in md {
            switch value {
            case let v as String: out[key] = v
            case let v as Date: out[key] = PayloadCoder.isoFormat.format(v)
            case let v as NSNumber: out[key] = v.stringValue
            case let v as HKQuantity: out[key] = v.description
            default: out[key] = String(describing: value)
            }
        }
        return out
    }

    static func sleepLabel(_ value: Int) -> String {
        switch HKCategoryValueSleepAnalysis(rawValue: value) {
        case .inBed: "in_bed"
        case .asleepUnspecified: "asleep_unspecified"
        case .awake: "awake"
        case .asleepCore: "asleep_core"
        case .asleepDeep: "asleep_deep"
        case .asleepREM: "asleep_rem"
        default: "unknown_\(value)"
        }
    }

    /// snake_case names; the dashboards translate them to Korean (ACTIVITY_KO in build_dashboards.py).
    static func activityName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: "running"
        case .walking: "walking"
        case .hiking: "hiking"
        case .cycling: "cycling"
        case .swimming: "swimming"
        case .yoga: "yoga"
        case .pilates: "pilates"
        case .socialDance: "dance"
        case .cardioDance: "cardio_dance"
        case .functionalStrengthTraining: "functional_strength_training"
        case .traditionalStrengthTraining: "traditional_strength_training"
        case .coreTraining: "core_training"
        case .highIntensityIntervalTraining: "high_intensity_interval_training"
        case .mixedCardio: "mixed_cardio"
        case .elliptical: "elliptical"
        case .rowing: "rowing"
        case .stairClimbing: "stair_climbing"
        case .stairs: "stairs"
        case .stepTraining: "step_training"
        case .jumpRope: "jump_rope"
        case .flexibility: "flexibility"
        case .cooldown: "cooldown"
        case .mindAndBody: "mind_and_body"
        case .taiChi: "tai_chi"
        case .tennis: "tennis"
        case .badminton: "badminton"
        case .tableTennis: "table_tennis"
        case .pickleball: "pickleball"
        case .soccer: "soccer"
        case .basketball: "basketball"
        case .baseball: "baseball"
        case .volleyball: "volleyball"
        case .golf: "golf"
        case .climbing: "climbing"
        case .boxing: "boxing"
        case .kickboxing: "kickboxing"
        case .martialArts: "martial_arts"
        case .skatingSports: "skating_sports"
        case .snowboarding: "snowboarding"
        case .downhillSkiing: "downhill_skiing"
        case .crossCountrySkiing: "cross_country_skiing"
        case .fitnessGaming: "fitness_gaming"
        case .swimBikeRun: "swim_bike_run"
        case .handCycling: "hand_cycling"
        case .wheelchairWalkPace: "wheelchair_walk_pace"
        case .wheelchairRunPace: "wheelchair_run_pace"
        case .other: "other"
        default: "type_\(t.rawValue)"
        }
    }
}
