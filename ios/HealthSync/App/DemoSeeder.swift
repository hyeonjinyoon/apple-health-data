#if DEBUG
import HealthKit

/// Simulator / UI-test helper (Debug builds only): writes a small, known data set into HealthKit,
/// then runs the normal sync against the server given in the environment.
///
/// Triggered by the launch argument `--seed-demo-health-data` with HEALTHSYNC_SERVER and
/// HEALTHSYNC_TOKEN in the launch environment (see HealthSyncUITests).
@MainActor
enum DemoSeeder {
    static let shareTypes: Set<HKSampleType> = [
        HKQuantityType(.heartRate), HKQuantityType(.stepCount), HKQuantityType(.activeEnergyBurned),
        HKQuantityType(.distanceWalkingRunning), HKQuantityType(.bodyMass), HKCategoryType(.sleepAnalysis),
        HKObjectType.workoutType(),
    ]

    static func runIfRequested(_ engine: SyncEngine) async {
        let info = ProcessInfo.processInfo
        guard info.arguments.contains("--seed-demo-health-data"),
              let server = info.environment["HEALTHSYNC_SERVER"],
              let token = info.environment["HEALTHSYNC_TOKEN"] else { return }
        do {
            engine.updateConfig(try ServerConfig.make(server: server, token: token))
            let store = HealthReader.shared.store
            // One permission sheet covering demo writes and all reads.
            try await store.requestAuthorization(toShare: shareTypes, read: HealthCatalog.readTypes)
            try await store.save(samples(now: Date()))
            try await saveWorkout(store, end: Date().addingTimeInterval(-600))
            await engine.requestAuthorization()
            await engine.sync(reason: "데모 시드")
        } catch {
            engine.notice = "데모 데이터 실패: \(error.localizedDescription)"
        }
    }

    /// 24 heart-rate readings, 10 × 500 steps, 1 weight, 6 sleep stages — all in the past 12 hours.
    static func samples(now: Date) -> [HKObject] {
        var out: [HKObject] = []
        for i in 0..<24 {
            let t = now.addingTimeInterval(-Double(i) * 1800 - 60)
            out.append(HKQuantitySample(type: HKQuantityType(.heartRate),
                                        quantity: HKQuantity(unit: HealthCatalog.bpm, doubleValue: 60 + Double(i % 7) * 4),
                                        start: t, end: t))
        }
        for h in 0..<10 {
            let end = now.addingTimeInterval(-Double(h) * 3600 - 120)
            out.append(HKQuantitySample(type: HKQuantityType(.stepCount), quantity: HKQuantity(unit: .count(), doubleValue: 500),
                                        start: end.addingTimeInterval(-1800), end: end))
        }
        let weighIn = now.addingTimeInterval(-7200)
        out.append(HKQuantitySample(type: HKQuantityType(.bodyMass), quantity: HKQuantity(unit: HealthCatalog.kg, doubleValue: 70.5),
                                    start: weighIn, end: weighIn))
        var t = now.addingTimeInterval(-12 * 3600)
        let stages: [(HKCategoryValueSleepAnalysis, Double)] = [
            (.asleepCore, 90), (.asleepDeep, 60), (.asleepREM, 45), (.awake, 5), (.asleepCore, 120), (.asleepREM, 60),
        ]
        for (stage, minutes) in stages {
            let end = t.addingTimeInterval(minutes * 60)
            out.append(HKCategorySample(type: HKCategoryType(.sleepAnalysis), value: stage.rawValue, start: t, end: end))
            t = end
        }
        return out
    }

    /// A 30-minute run with 320 kcal and 5 km.
    static func saveWorkout(_ store: HKHealthStore, end: Date) async throws {
        let start = end.addingTimeInterval(-1800)
        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .running
        let builder = HKWorkoutBuilder(healthStore: store, configuration: configuration, device: .local())
        try await builder.beginCollection(at: start)
        try await builder.addSamples([
            HKQuantitySample(type: HKQuantityType(.activeEnergyBurned), quantity: HKQuantity(unit: .kilocalorie(), doubleValue: 320),
                             start: start, end: end),
            HKQuantitySample(type: HKQuantityType(.distanceWalkingRunning), quantity: HKQuantity(unit: .meter(), doubleValue: 5000),
                             start: start, end: end),
        ])
        try await builder.endCollection(at: end)
        _ = try await builder.finishWorkout()
    }
}
#endif
