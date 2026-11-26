import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var engine: SyncEngine
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            List {
                if engine.config == nil {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("서버 연결이 필요합니다", systemImage: "server.rack").font(.headline)
                            Text("Mac mini에서 `python3 deploy/show_config.py` 를 실행하고 화면의 QR 코드를 iPhone 카메라로 스캔하면 자동으로 설정됩니다.")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 4)
                        Button("직접 입력하기") { showSettings = true }
                    }
                }

                Section("상태") {
                    LabeledContent("서버", value: engine.config?.baseURL.absoluteString ?? "미설정")
                    LabeledContent("마지막 동기화") {
                        if let last = engine.lastSync {
                            Text(last, style: .relative) + Text(" 전")
                        } else {
                            Text("없음")
                        }
                    }
                    if engine.isSyncing {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text(engine.progress).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    if let error = engine.lastError, !engine.isSyncing {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline).foregroundStyle(.red)
                    }
                }

                Section {
                    Button {
                        Task { await engine.sync(reason: "수동") }
                    } label: {
                        Label("지금 동기화", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(engine.isSyncing || engine.config == nil)

                    Button {
                        Task { await engine.requestAuthorization() }
                    } label: {
                        Label("건강 데이터 접근 권한", systemImage: "heart.text.square")
                    }
                } footer: {
                    Text("권한은 설정 앱 › 건강 › 데이터 접근 및 기기 › HealthSync 에서도 바꿀 수 있습니다.")
                }

                Section("누적 전송량") {
                    LabeledContent("샘플", value: engine.totals.samples.formatted())
                    LabeledContent("시간별 통계", value: engine.totals.stats.formatted())
                    LabeledContent("운동", value: engine.totals.workouts.formatted())
                    LabeledContent("삭제 반영", value: engine.totals.deleted.formatted())
                }

                Section("기록") {
                    if engine.log.isEmpty {
                        Text("아직 기록이 없습니다").foregroundStyle(.secondary)
                    }
                    ForEach(engine.log.reversed()) { entry in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(entry.message)
                                .font(.footnote)
                                .foregroundStyle(entry.isError ? .red : .primary)
                            Text(entry.date, format: .dateTime.month().day().hour().minute().second())
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("HealthSync")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("설정")
            }
            .refreshable { await engine.sync(reason: "새로고침") }
            .sheet(isPresented: $showSettings) {
                SettingsView().environmentObject(engine)
            }
            .alert("HealthSync", isPresented: Binding(
                get: { engine.notice != nil }, set: { if !$0 { engine.notice = nil } }
            )) {
                Button("확인", role: .cancel) {}
            } message: {
                Text(engine.notice ?? "")
            }
        }
    }
}

#Preview {
    ContentView().environmentObject(SyncEngine.shared)
}
