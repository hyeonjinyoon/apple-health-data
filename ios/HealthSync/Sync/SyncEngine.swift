import HealthKit
import UIKit

/// Orchestrates HealthKit → server sync. One sync runs at a time; requests made while a sync
/// is running wait for it and trigger one follow-up run (so nothing new is missed).
@MainActor
final class SyncEngine: ObservableObject {
    static let shared = SyncEngine()

    @Published private(set) var config: ServerConfig? = ServerConfig.load()
    @Published private(set) var isSyncing = false
    @Published private(set) var progress = ""
    @Published private(set) var lastSync: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var totals: SyncTotals
    @Published private(set) var log: [LogEntry]
    /// One-shot message for an alert (e.g. after scanning the setup QR code).
    @Published var notice: String?

    /// Objects per HealthKit page / upload request.
    private let pageSize = 4000
    /// Days per hourly-statistics upload.
    private let statsChunkDays = 60
    /// Hourly stats are recomputed for this many days back on every sync (late Watch data, edits).
    private let statsRecomputeDays = 2

    private let health = HealthReader.shared
    private let state = SyncState.shared
    private var currentRun: Task<Void, Never>?
    private var rerunRequested = false

    private init() {
        lastSync = state.lastSuccess
        totals = state.totals
        log = state.log
    }

    // MARK: configuration & permission

    func updateConfig(_ newConfig: ServerConfig) {
        newConfig.save()
        config = newConfig
        append("서버 설정 저장: \(newConfig.baseURL.absoluteString)")
    }

    func handleConfigURL(_ url: URL) {
        do {
            updateConfig(try ServerConfig.fromLink(url))
            notice = "서버 설정 완료\n\(config?.baseURL.absoluteString ?? "")"
            Task {
                await requestAuthorization()
                await sync(reason: "QR 설정")
            }
        } catch {
            notice = error.localizedDescription
        }
    }

    func requestAuthorization() async {
        guard health.isAvailable else {
            fail("이 기기에서는 건강 데이터를 사용할 수 없습니다.")
            return
        }
        do {
            try await health.requestAuthorization()
            state.authorizationRequested = true
            BackgroundSync.startObservers()
            append("건강 데이터 접근 권한 요청 완료")
        } catch {
            fail("권한 요청 실패: \(error.localizedDescription)")
        }
    }

    func resetAndResync() {
        state.reset()
        append("동기화 위치 초기화 → 전체 기록을 다시 보냅니다")
        Task { await sync(reason: "전체 재전송") }
    }

    // MARK: sync

    func syncIfConfigured(reason: String) {
        guard config != nil, state.authorizationRequested else { return }
        Task { await sync(reason: reason) }
    }

    /// Runs a sync (or joins the one in progress) and returns when the data is uploaded.
    func sync(reason: String) async {
        if let running = currentRun {
            rerunRequested = true
            await running.value
            // The run may have finished just before our request was noticed.
            if rerunRequested && currentRun == nil { await sync(reason: reason) }
            return
        }
        let run = Task { await runLoop(reason: reason) }
        currentRun = run
        await run.value
        currentRun = nil
    }

    private func runLoop(reason: String) async {
        guard let config else {
            fail("서버가 설정되지 않았습니다.")
            return
        }
        if !state.authorizationRequested { await requestAuthorization() }

        isSyncing = true
        lastError = nil
        // Ask iOS for extra time if the app is backgrounded mid-sync; when it runs out, stop
        // cleanly (progress is saved per page, the next sync resumes).
        var bgTask = UIBackgroundTaskIdentifier.invalid
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "HealthSync") { [weak self] in
            self?.currentRun?.cancel()
            UIApplication.shared.endBackgroundTask(bgTask)
            bgTask = .invalid
        }
        defer {
            isSyncing = false
            progress = ""
            if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask) }
        }

        repeat {
            rerunRequested = false
            let started = Date()
            var sent = SyncTotals()
            do {
                try await syncAll(api: APIClient(config: config), sent: &sent)
                lastSync = Date()
                state.lastSuccess = lastSync
                let secs = Int(Date().timeIntervalSince(started))
                append(sent == SyncTotals()
                       ? "[\(reason)] 새 데이터 없음 (\(secs)초)"
                       : "[\(reason)] 샘플 \(sent.samples.formatted()) · 통계 \(sent.stats.formatted()) · 운동 \(sent.workouts) · 삭제 \(sent.deleted) 전송 (\(secs)초)")
            } catch is CancellationError {
                append("[\(reason)] 중단됨 — 다음 동기화에서 이어서 진행합니다")
                return
            } catch let error as HKError where error.code == .errorDatabaseInaccessible {
                fail("iPhone이 잠겨 있어 건강 데이터를 읽을 수 없습니다. 잠금 해제 후 다시 시도합니다.")
                return
            } catch {
                fail("[\(reason)] 실패: \(error.localizedDescription)")
                return
            }
        } while rerunRequested
    }

    private func syncAll(api: APIClient, sent: inout SyncTotals) async throws {
        for spec in HealthCatalog.discrete {
            try await syncAnchored(key: spec.key, title: spec.title, api: api, sent: &sent) { anchor in
                try await health.fetch(spec, anchor: anchor, limit: pageSize)
            }
        }
        for spec in HealthCatalog.categories {
            try await syncAnchored(key: spec.key, title: spec.title, api: api, sent: &sent) { anchor in
                try await health.fetch(spec, anchor: anchor, limit: pageSize)
            }
        }
        try await syncAnchored(key: "workouts", title: "운동", api: api, sent: &sent) { anchor in
            try await health.fetchWorkouts(anchor: anchor, limit: pageSize)
        }
        for spec in HealthCatalog.cumulative {
            try await syncHourlyStats(spec, api: api, sent: &sent)
        }
    }

    /// Pages through everything added/deleted since the stored anchor. The anchor is saved only
    /// after the server accepted the page, so failures never lose data.
    private func syncAnchored(
        key: String, title: String, api: APIClient, sent: inout SyncTotals,
        fetch: (HKQueryAnchor?) async throws -> AnchoredBatch
    ) async throws {
        var anchor = state.anchor(for: key)
        var count = 0
        while true {
            try Task.checkCancellation()
            progress = count == 0 ? "\(title) 확인 중…" : "\(title) \(count.formatted())건 전송 중…"
            let batch = try await fetch(anchor)
            if !batch.samples.isEmpty || !batch.workouts.isEmpty || !batch.deleted.isEmpty {
                var payload = makePayload()
                payload.samples = batch.samples
                payload.workouts = batch.workouts
                payload.deleted = batch.deleted
                record(try await api.ingest(payload), into: &sent)
                count += batch.fetchedCount
            }
            state.setAnchor(batch.newAnchor, for: key)
            anchor = batch.newAnchor
            if batch.fetchedCount < pageSize { break }
        }
    }

    private func syncHourlyStats(_ spec: QuantitySpec, api: APIClient, sent: inout SyncTotals) async throws {
        let calendar = Calendar.current
        let now = Date()
        let recomputeFrom = calendar.startOfDay(for: now).addingTimeInterval(-Double(statsRecomputeDays) * 86_400)
        var start: Date
        if let cursor = state.statsCursor(for: spec.key) {
            start = min(calendar.startOfDay(for: cursor), recomputeFrom)
        } else {
            progress = "\(spec.title) 기록 시작일 확인 중…"
            // No data of this type yet: skip (no cursor, so the next sync checks again — a
            // cheap 1-row query — instead of re-sending empty hours forever).
            guard let earliest = try await health.earliestSampleDate(spec.type) else { return }
            start = calendar.startOfDay(for: earliest)
        }
        while start < now {
            try Task.checkCancellation()
            let end = min(calendar.date(byAdding: .day, value: statsChunkDays, to: start) ?? now, now)
            progress = "\(spec.title) 시간별 합계 \(start.formatted(.dateTime.year().month().day())) ~"
            // Inside the recompute window send zeros too, so values removed on the phone are cleared.
            let stats = try await health.hourlySums(spec, from: start, to: end, includeEmpty: end > recomputeFrom)
            if !stats.isEmpty {
                var payload = makePayload()
                payload.stats = stats
                record(try await api.ingest(payload), into: &sent)
            }
            state.setStatsCursor(end, for: spec.key)
            start = end
        }
    }

    // MARK: helpers

    private func makePayload() -> IngestPayload {
        let info = Bundle.main.infoDictionary
        let version = "\(info?["CFBundleShortVersionString"] as? String ?? "?") (\(info?["CFBundleVersion"] as? String ?? "?"))"
        return IngestPayload(
            deviceId: UIDevice.current.identifierForVendor?.uuidString ?? "",
            deviceName: UIDevice.current.name,
            appVersion: version)
    }

    private func record(_ response: IngestResponse, into sent: inout SyncTotals) {
        sent.add(response)
        totals.add(response)
        state.totals = totals
    }

    private func append(_ message: String, isError: Bool = false) {
        log.append(LogEntry(message: message, isError: isError))
        if log.count > 60 { log.removeFirst(log.count - 60) }
        state.log = log
    }

    private func fail(_ message: String) {
        lastError = message
        append(message, isError: true)
    }
}
