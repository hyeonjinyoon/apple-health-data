import Foundation
import HealthKit
import Testing
@testable import HealthSync

struct PayloadTests {
    @Test func encodesSnakeCaseKeysAndUTCDatesWithMillis() throws {
        var payload = IngestPayload(deviceId: "dev", deviceName: "iPhone", appVersion: "1.0 (1)")
        payload.samples = [SamplePayload(
            uuid: "6F0C3C1E-9D7B-4A51-8F0E-2B1F4C5D6E7F", type: "heart_rate", value: 72, unit: "count/min",
            start: Date(timeIntervalSince1970: 1_764_072_000.25), end: Date(timeIntervalSince1970: 1_764_072_000.25),
            source: "Apple Watch", sourceBundle: "com.apple.health", device: "Watch7,1", metadata: ["k": "v"])]
        payload.workouts = [WorkoutPayload(
            uuid: "A", activityType: "running", start: Date(timeIntervalSince1970: 0),
            end: Date(timeIntervalSince1970: 60), durationS: 60, energyKcal: 10)]

        let json = try #require(try JSONSerialization.jsonObject(with: PayloadCoder.encode(payload)) as? [String: Any])
        #expect(json["device_id"] as? String == "dev")
        #expect(json["app_version"] as? String == "1.0 (1)")
        let sample = try #require((json["samples"] as? [[String: Any]])?.first)
        #expect(sample["source_bundle"] as? String == "com.apple.health")
        #expect(sample["start"] as? String == "2025-11-25T12:00:00.250Z")
        #expect(sample["metadata"] as? [String: String] == ["k": "v"])
        let workout = try #require((json["workouts"] as? [[String: Any]])?.first)
        #expect(workout["activity_type"] as? String == "running")
        #expect(workout["duration_s"] as? Double == 60)
        #expect(workout["energy_kcal"] as? Double == 10)
        #expect(workout["distance_m"] == nil)  // nil optionals are omitted
    }

    @Test func deflateRoundTrips() throws {
        let raw = Data(String(repeating: "{\"type\":\"heart_rate\"}", count: 500).utf8)
        let compressed = try PayloadCoder.deflate(raw)
        #expect(compressed.count < raw.count / 10)
        let restored = try (compressed as NSData).decompressed(using: .zlib) as Data
        #expect(restored == raw)
    }

    @Test func sleepLabels() {
        #expect(PayloadMapper.sleepLabel(HKCategoryValueSleepAnalysis.asleepDeep.rawValue) == "asleep_deep")
        #expect(PayloadMapper.sleepLabel(HKCategoryValueSleepAnalysis.asleepREM.rawValue) == "asleep_rem")
        #expect(PayloadMapper.sleepLabel(HKCategoryValueSleepAnalysis.inBed.rawValue) == "in_bed")
        #expect(PayloadMapper.sleepLabel(99) == "unknown_99")
    }

    @Test func activityNames() {
        #expect(PayloadMapper.activityName(.running) == "running")
        #expect(PayloadMapper.activityName(.traditionalStrengthTraining) == "traditional_strength_training")
        #expect(PayloadMapper.activityName(.highIntensityIntervalTraining) == "high_intensity_interval_training")
    }

    @Test func metadataIsStringified() {
        let md: [String: Any] = [
            "s": "text", "n": NSNumber(value: 1), "q": HKQuantity(unit: .meter(), doubleValue: 3),
            "d": Date(timeIntervalSince1970: 0),
        ]
        let out = PayloadMapper.metadata(md)
        #expect(out["s"] == "text")
        #expect(out["n"] == "1")
        #expect(out["q"] == "3 m")
        #expect(out["d"] == "1970-01-01T00:00:00.000Z")
    }

    @Test func catalogKeysAreUnique() {
        let keys = HealthCatalog.discrete.map(\.key) + HealthCatalog.cumulative.map(\.key) + HealthCatalog.categories.map(\.key)
        #expect(Set(keys).count == keys.count)
    }
}

struct ServerConfigTests {
    @Test(arguments: [
        "192.168.1.20", "10.0.0.2", "172.20.1.1", "100.101.102.103", "127.0.0.1",
        "mac-mini.local", "mac.tail1234.ts.net", "macmini", "fd7a:115c:a1e0::1",
    ])
    func privateHostsAllowHTTP(host: String) {
        #expect(ServerConfig.isPrivateHost(host))
    }

    @Test(arguments: ["8.8.8.8", "example.com", "172.32.0.1", "100.128.0.1", "2001:db8::1"])
    func publicHostsNeedHTTPS(host: String) {
        #expect(!ServerConfig.isPrivateHost(host))
    }

    @Test func normalizesInput() throws {
        let c = try ServerConfig.make(server: " 192.168.1.20:8787/ ", token: " abc ")
        #expect(c.baseURL.absoluteString == "http://192.168.1.20:8787")
        #expect(c.token == "abc")
    }

    @Test func rejectsBadInput() {
        #expect(throws: ConfigError.insecureRemote("example.com")) { try ServerConfig.make(server: "http://example.com", token: "t") }
        #expect(throws: ConfigError.missingToken) { try ServerConfig.make(server: "http://10.0.0.1:8787", token: " ") }
        #expect(throws: ConfigError.invalidURL) { try ServerConfig.make(server: "ftp://10.0.0.1", token: "t") }
        #expect(throws: Never.self) { try ServerConfig.make(server: "https://example.com", token: "t") }
    }

    @Test func parsesSetupLink() throws {
        let url = try #require(URL(string: "healthsync://configure?server=http%3A%2F%2F100.101.102.103%3A8787&token=abc-_123"))
        let c = try ServerConfig.fromLink(url)
        #expect(c.baseURL.absoluteString == "http://100.101.102.103:8787")
        #expect(c.token == "abc-_123")
        #expect(throws: ConfigError.notAConfigLink) { try ServerConfig.fromLink(URL(string: "https://example.com")!) }
    }
}

/// Talks to a real ingest server. Runs only when configured, e.g. against the demo database:
///   TEST_RUNNER_HEALTHSYNC_IT_URL=http://127.0.0.1:8788 TEST_RUNNER_HEALTHSYNC_IT_TOKEN=... make test
struct LiveServerTests {
    static let env = ProcessInfo.processInfo.environment

    @Test(.enabled(if: env["HEALTHSYNC_IT_URL"] != nil))
    func uploadsAndDeletesOnRealServer() async throws {
        let config = try ServerConfig.make(server: Self.env["HEALTHSYNC_IT_URL"] ?? "", token: Self.env["HEALTHSYNC_IT_TOKEN"] ?? "")
        let api = APIClient(config: config)
        _ = try await api.ping()

        let sampleID = UUID().uuidString, workoutID = UUID().uuidString
        var payload = IngestPayload(deviceId: "swift-it", deviceName: "Swift live test", appVersion: "test")
        payload.samples = [SamplePayload(uuid: sampleID, type: "it_probe", value: 1, unit: "count",
                                         start: .now, end: .now, metadata: ["from": "swift"])]
        payload.workouts = [WorkoutPayload(uuid: workoutID, activityType: "other", start: .now.addingTimeInterval(-60),
                                           end: .now, durationS: 60)]
        #expect(try await api.ingest(payload) == IngestResponse(samples: 1, workouts: 1, stats: 0, deleted: 0))

        var cleanup = IngestPayload(deviceId: "swift-it", deviceName: "Swift live test", appVersion: "test")
        cleanup.deleted = [sampleID, workoutID]
        #expect(try await api.ingest(cleanup).deleted == 2)

        let bad = APIClient(config: ServerConfig(baseURL: config.baseURL, token: "wrong-token-0000000000"))
        await #expect(throws: APIError.self) { try await bad.ping() }
    }
}
