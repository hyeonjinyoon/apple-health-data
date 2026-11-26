import HealthKit

struct SyncTotals: Codable, Equatable {
    var samples = 0
    var workouts = 0
    var stats = 0
    var deleted = 0

    mutating func add(_ r: IngestResponse) {
        samples += r.samples
        workouts += r.workouts
        stats += r.stats
        deleted += r.deleted
    }
}

struct LogEntry: Codable, Identifiable, Equatable {
    var id = UUID()
    var date = Date()
    var message: String
    var isError = false
}

/// Persistent sync progress: one HealthKit anchor per type and a cursor per hourly-stats type.
/// Progress is saved after every successful upload, so an interrupted sync resumes where it stopped.
final class SyncState {
    static let shared = SyncState()
    private let defaults = UserDefaults.standard
    private let anchorPrefix = "anchor."
    private let cursorPrefix = "statsCursor."

    func anchor(for key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: anchorPrefix + key) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    func setAnchor(_ anchor: HKQueryAnchor, for key: String) {
        if let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true) {
            defaults.set(data, forKey: anchorPrefix + key)
        }
    }

    func statsCursor(for key: String) -> Date? {
        defaults.object(forKey: cursorPrefix + key) as? Date
    }

    func setStatsCursor(_ date: Date, for key: String) {
        defaults.set(date, forKey: cursorPrefix + key)
    }

    /// Forget all progress → the next sync re-sends full history (server de-duplicates).
    func reset() {
        for key in defaults.dictionaryRepresentation().keys
        where key.hasPrefix(anchorPrefix) || key.hasPrefix(cursorPrefix) {
            defaults.removeObject(forKey: key)
        }
    }

    var lastSuccess: Date? {
        get { defaults.object(forKey: "lastSuccess") as? Date }
        set { defaults.set(newValue, forKey: "lastSuccess") }
    }

    var authorizationRequested: Bool {
        get { defaults.bool(forKey: "authorizationRequested") }
        set { defaults.set(newValue, forKey: "authorizationRequested") }
    }

    var totals: SyncTotals {
        get { decode("totals") ?? SyncTotals() }
        set { encode(newValue, "totals") }
    }

    var log: [LogEntry] {
        get { decode("log") ?? [] }
        set { encode(Array(newValue.suffix(60)), "log") }
    }

    private func decode<T: Decodable>(_ key: String) -> T? {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(T.self, from: $0) }
    }

    private func encode<T: Encodable>(_ value: T, _ key: String) {
        defaults.set(try? JSONEncoder().encode(value), forKey: key)
    }
}
