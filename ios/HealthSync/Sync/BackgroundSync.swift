import BackgroundTasks
import HealthKit

/// Two background triggers:
///  1. HealthKit background delivery — iOS wakes the app when new samples are saved
///     (at most about hourly for most types). Observers must be registered at every launch.
///  2. BGAppRefreshTask — opportunistic periodic refresh as a safety net.
enum BackgroundSync {
    static var refreshTaskID: String { (Bundle.main.bundleIdentifier ?? "HealthSync") + ".refresh" }
    @MainActor private static var observersStarted = false

    /// Call from application(_:didFinishLaunchingWithOptions:).
    @MainActor
    static func registerTasks() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTaskID, using: nil) { task in
            Task { @MainActor in
                scheduleAppRefresh()
                let work = Task { await SyncEngine.shared.sync(reason: "백그라운드 새로고침") }
                task.expirationHandler = { work.cancel() }
                await work.value
                task.setTaskCompleted(success: !work.isCancelled)
            }
        }
    }

    static func scheduleAppRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 60 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    @MainActor
    static func startObservers() {
        guard !observersStarted, SyncState.shared.authorizationRequested, HealthReader.shared.isAvailable else { return }
        observersStarted = true
        let store = HealthReader.shared.store
        for type in HealthCatalog.sampleTypes {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { _, completion, error in
                guard error == nil else {
                    completion()
                    return
                }
                // Many types fire together; SyncEngine coalesces them into one run.
                Task { @MainActor in
                    await SyncEngine.shared.sync(reason: "새 건강 데이터")
                    completion()
                }
            }
            store.execute(query)
            store.enableBackgroundDelivery(for: type, frequency: .hourly) { _, _ in }
        }
    }
}
