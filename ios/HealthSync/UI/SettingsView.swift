import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var engine: SyncEngine
    @Environment(\.dismiss) private var dismiss

    @State private var server = ""
    @State private var token = ""
    @State private var revealToken = false
    @State private var error: String?
    @State private var testResult: String?
    @State private var testing = false
    @State private var confirmReset = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.0.10:8787", text: $server)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                    Group {
                        if revealToken {
                            TextField("API 토큰", text: $token)
                        } else {
                            SecureField("API 토큰", text: $token)
                        }
                    }
                    .font(.system(.body, design: .monospaced))
                    Toggle("토큰 표시", isOn: $revealToken)
                } header: {
                    Text("Mac mini 서버")
                } footer: {
                    Text("Mac mini에서 `python3 deploy/show_config.py` 를 실행해 나온 QR 코드를 카메라로 스캔하면 자동으로 채워집니다. 집 밖에서도 보내려면 Tailscale 주소(100.x.x.x)를 사용하세요.")
                }
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()

                if let error {
                    Section { Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red) }
                }

                Section {
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("연결 테스트")
                            if testing { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(testing)
                    if let testResult { Text(testResult).font(.footnote) }
                }

                Section {
                    Button("전체 기록 다시 보내기", role: .destructive) { confirmReset = true }
                } footer: {
                    Text("동기화 위치를 초기화하고 건강 앱의 전체 기록을 다시 보냅니다. 서버가 중복을 자동으로 제거하므로 데이터가 두 번 쌓이지 않습니다.")
                }
            }
            .navigationTitle("설정")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("닫기") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("저장") { save() }.bold() }
            }
            .onAppear {
                server = engine.config?.baseURL.absoluteString ?? ""
                token = engine.config?.token ?? ""
            }
            .confirmationDialog("전체 기록을 다시 보낼까요?", isPresented: $confirmReset, titleVisibility: .visible) {
                Button("다시 보내기", role: .destructive) {
                    engine.resetAndResync()
                    dismiss()
                }
            }
        }
    }

    private func validated() -> ServerConfig? {
        do {
            error = nil
            return try ServerConfig.make(server: server, token: token)
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    private func save() {
        guard let config = validated() else { return }
        engine.updateConfig(config)
        dismiss()
        Task {
            await engine.requestAuthorization()
            await engine.sync(reason: "설정 저장")
        }
    }

    private func test() async {
        guard let config = validated() else { return }
        testing = true
        defer { testing = false }
        do {
            let db = try await APIClient(config: config).ping()
            testResult = "✅ 연결 성공 (저장소: \(db))"
        } catch {
            testResult = "❌ \(error.localizedDescription)"
        }
    }
}
