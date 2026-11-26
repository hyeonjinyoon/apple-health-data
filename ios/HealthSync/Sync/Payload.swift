import Foundation

// Wire format — see docs/api.md. Property names are converted to snake_case by the encoder.

struct SamplePayload: Encodable, Equatable {
    var uuid: String
    var type: String
    var value: Double
    var unit: String
    var start: Date
    var end: Date
    var category: String = ""
    var source: String = ""
    var sourceBundle: String = ""
    var device: String = ""
    var metadata: [String: String] = [:]
}

struct WorkoutPayload: Encodable, Equatable {
    var uuid: String
    var activityType: String
    var start: Date
    var end: Date
    var durationS: Double
    var energyKcal: Double?
    var distanceM: Double?
    var avgHeartRate: Double?
    var maxHeartRate: Double?
    var source: String = ""
    var sourceBundle: String = ""
    var device: String = ""
    var metadata: [String: String] = [:]
}

struct StatPayload: Encodable, Equatable {
    var type: String
    var start: Date
    var end: Date
    var value: Double
    var unit: String
}

struct IngestPayload: Encodable {
    var deviceId: String
    var deviceName: String
    var appVersion: String
    var samples: [SamplePayload] = []
    var workouts: [WorkoutPayload] = []
    var stats: [StatPayload] = []
    var deleted: [String] = []

    var isEmpty: Bool { samples.isEmpty && workouts.isEmpty && stats.isEmpty && deleted.isEmpty }
}

struct IngestResponse: Decodable, Equatable {
    var samples: Int
    var workouts: Int
    var stats: Int
    var deleted: Int
}

enum PayloadCoder {
    /// e.g. `2026-11-25T12:00:00.123Z`
    static let isoFormat = Date.ISO8601FormatStyle(includingFractionalSeconds: true)

    static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var c = encoder.singleValueContainer()
            try c.encode(isoFormat.format(date))
        }
        return try encoder.encode(value)
    }

    /// Raw DEFLATE (RFC 1951). Sent with `Content-Encoding: deflate`; the server accepts it.
    static func deflate(_ data: Data) throws -> Data {
        try (data as NSData).compressed(using: .zlib) as Data
    }
}
