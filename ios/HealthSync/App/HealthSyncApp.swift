import SwiftUI
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Both must happen during launch, including background launches by HealthKit.
        BackgroundSync.registerTasks()
        BackgroundSync.startObservers()
        return true
    }
}

@main
struct HealthSyncApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var engine = SyncEngine.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(engine)
                .onOpenURL { engine.handleConfigURL($0) }
                #if DEBUG
                .task { await DemoSeeder.runIfRequested(engine) }
                #endif
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active: engine.syncIfConfigured(reason: "앱 열기")
            case .background: BackgroundSync.scheduleAppRefresh()
            default: break
            }
        }
    }
}
