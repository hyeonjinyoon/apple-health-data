import HealthKit

/// One page of an anchored (incremental) HealthKit query, already mapped to wire payloads.
struct AnchoredBatch {
    var samples: [SamplePayload] = []
    var workouts: [WorkoutPayload] = []
    var deleted: [String] = []
    var newAnchor: HKQueryAnchor
    /// Objects HealthKit returned (added + deleted); a full page means "there may be more".
    var fetchedCount: Int
}

final class HealthReader {
    static let shared = HealthReader()
    let store = HKHealthStore()

    var isAvailable: Bool { HKHealthStore.isHealthDataAvailable() }

    func requestAuthorization() async throws {
        try await store.requestAuthorization(toShare: [], read: HealthCatalog.readTypes)
    }

    // MARK: anchored queries — return objects added/deleted since `anchor`

    func fetch(_ spec: QuantitySpec, anchor: HKQueryAnchor?, limit: Int) async throws -> AnchoredBatch {
        let query = HKAnchoredObjectQueryDescriptor(
            predicates: [.quantitySample(type: spec.type)], anchor: anchor, limit: limit)
        let r = try await query.result(for: store)
        return AnchoredBatch(
            samples: r.addedSamples.compactMap { PayloadMapper.sample($0, spec: spec) },
            deleted: r.deletedObjects.map(\.uuid.uuidString),
            newAnchor: r.newAnchor,
            fetchedCount: r.addedSamples.count + r.deletedObjects.count)
    }

    func fetch(_ spec: CategorySpec, anchor: HKQueryAnchor?, limit: Int) async throws -> AnchoredBatch {
        let query = HKAnchoredObjectQueryDescriptor(
            predicates: [.categorySample(type: spec.type)], anchor: anchor, limit: limit)
        let r = try await query.result(for: store)
        return AnchoredBatch(
            samples: r.addedSamples.map { PayloadMapper.sample($0, spec: spec) },
            deleted: r.deletedObjects.map(\.uuid.uuidString),
            newAnchor: r.newAnchor,
            fetchedCount: r.addedSamples.count + r.deletedObjects.count)
    }

    func fetchWorkouts(anchor: HKQueryAnchor?, limit: Int) async throws -> AnchoredBatch {
        let query = HKAnchoredObjectQueryDescriptor(predicates: [.workout()], anchor: anchor, limit: limit)
        let r = try await query.result(for: store)
        return AnchoredBatch(
            workouts: r.addedSamples.map(PayloadMapper.workout),
            deleted: r.deletedObjects.map(\.uuid.uuidString),
            newAnchor: r.newAnchor,
            fetchedCount: r.addedSamples.count + r.deletedObjects.count)
    }

    // MARK: statistics

    func earliestSampleDate(_ type: HKSampleType) async throws -> Date? {
        let query = HKSampleQueryDescriptor(
            predicates: [.sample(type: type)],
            sortDescriptors: [SortDescriptor(\HKSample.startDate, order: .forward)],
            limit: 1)
        return try await query.result(for: store).first?.startDate
    }

    /// Hourly sums for a cumulative type in [from, to). HealthKit merges overlapping sources
    /// (iPhone + Watch) by the user's source priority, exactly like the Health app.
    func hourlySums(_ spec: QuantitySpec, from: Date, to: Date, includeEmpty: Bool) async throws -> [StatPayload] {
        let window = HKQuery.predicateForSamples(withStart: from, end: to, options: [])
        let query = HKStatisticsCollectionQueryDescriptor(
            predicate: .quantitySample(type: spec.type, predicate: window),
            options: .cumulativeSum,
            anchorDate: Calendar.current.startOfDay(for: from),
            intervalComponents: DateComponents(hour: 1))
        let collection = try await query.result(for: store)
        var out: [StatPayload] = []
        collection.enumerateStatistics(from: from, to: to) { stats, _ in
            let value = stats.sumQuantity()?.doubleValue(for: spec.unit)
            if value != nil || includeEmpty {
                out.append(StatPayload(type: spec.key, start: stats.startDate, end: stats.endDate,
                                       value: value ?? 0, unit: spec.unit.unitString))
            }
        }
        return out
    }
}
