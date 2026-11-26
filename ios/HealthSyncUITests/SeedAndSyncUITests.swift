import XCTest

/// End-to-end on a simulator: writes known data into HealthKit (DemoSeeder), grants the
/// HealthKit permission sheet, and waits until the app reports a successful upload.
///
///   TEST_RUNNER_HEALTHSYNC_IT_URL=http://127.0.0.1:8789 TEST_RUNNER_HEALTHSYNC_IT_TOKEN=... \
///     xcodebuild test -only-testing:HealthSyncUITests ...
final class SeedAndSyncUITests: XCTestCase {
    func testSeedHealthKitAndSync() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["HEALTHSYNC_IT_URL"], let token = env["HEALTHSYNC_IT_TOKEN"] else {
            throw XCTSkip("set TEST_RUNNER_HEALTHSYNC_IT_URL / _TOKEN to run")
        }
        let app = XCUIApplication()
        app.launchArguments = ["--seed-demo-health-data"]
        app.launchEnvironment = ["HEALTHSYNC_SERVER": url, "HEALTHSYNC_TOKEN": token]
        app.launch()

        grantHealthAccessIfAsked(app)

        let uploaded = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS '[데모 시드]'")).firstMatch
        let ok = uploaded.waitForExistence(timeout: 120)
        add(XCTAttachment(screenshot: app.screenshot()))
        XCTAssertTrue(ok, "sync result did not appear")
        XCTAssertFalse(uploaded.label.contains("실패"), uploaded.label)
    }

    /// Walks through the HealthKit permission sheet. Its layout differs by iOS version:
    ///  - older: "Turn On All" + "Allow"
    ///  - newer: "Select All N Topics" → "Continue" → history scope ("All Recorded Data and
    ///    Future Data") → "Allow", with buttons at the bottom of a scrolling sheet.
    /// It can take ~30 s to appear on a freshly booted simulator (healthd warm-up).
    private func grantHealthAccessIfAsked(_ app: XCUIApplication) {
        let any = app.descendants(matching: .any)
        func first(_ format: String) -> XCUIElement { any.matching(NSPredicate(format: format)).firstMatch }
        let dontAllow = first("(label BEGINSWITH 'Don' AND label ENDSWITH 'Allow') OR label == '허용 안 함'")
        guard dontAllow.waitForExistence(timeout: 90) else { return }  // already granted
        for _ in 0..<8 {
            let selectAll = first("label BEGINSWITH 'Turn On All' OR label BEGINSWITH 'Select All' OR label BEGINSWITH '모두'")
            if selectAll.exists && selectAll.isHittable { selectAll.tap() }
            let allHistory = first("label BEGINSWITH 'All Recorded Data' OR label BEGINSWITH '기록된 모든'")
            if allHistory.exists && allHistory.isHittable { allHistory.tap() }
            let next = first("label IN {'Allow', 'Continue', '허용', '계속'}")
            guard next.waitForExistence(timeout: 5) else { break }
            for _ in 0..<20 where !next.isHittable { app.swipeUp() }
            add(XCTAttachment(screenshot: app.screenshot()))
            next.tap()
            if dontAllow.waitForNonExistence(timeout: 3) { return }
        }
        XCTFail("Health permission sheet did not close")
    }
}
