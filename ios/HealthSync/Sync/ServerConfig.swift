import Foundation
import Security

enum ConfigError: LocalizedError, Equatable {
    case invalidURL
    case insecureRemote(String)
    case missingToken
    case notAConfigLink

    var errorDescription: String? {
        switch self {
        case .invalidURL: "서버 주소가 올바르지 않습니다. 예: http://192.168.0.10:8787"
        case .insecureRemote(let host): "\(host) 은(는) 공용 주소입니다. 공용 주소는 https:// 만 허용됩니다."
        case .missingToken: "API 토큰을 입력해 주세요."
        case .notAConfigLink: "HealthSync 설정 링크가 아닙니다."
        }
    }
}

/// Server address + API token. The token is kept in the Keychain.
struct ServerConfig: Equatable {
    var baseURL: URL
    var token: String

    private static let urlKey = "serverURL"
    private static let tokenAccount = "apiToken"

    static func load() -> ServerConfig? {
        guard let raw = UserDefaults.standard.string(forKey: urlKey), let url = URL(string: raw),
              let token = Keychain.get(tokenAccount), !token.isEmpty else { return nil }
        return ServerConfig(baseURL: url, token: token)
    }

    func save() {
        UserDefaults.standard.set(baseURL.absoluteString, forKey: Self.urlKey)
        Keychain.set(token, for: Self.tokenAccount)
    }

    /// Validates user input. Plain http is only allowed to private/LAN/Tailscale hosts.
    static func make(server: String, token: String) throws -> ServerConfig {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        if !s.contains("://") { s = "http://" + s }
        guard let url = URL(string: s), let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = url.host(), !host.isEmpty else { throw ConfigError.invalidURL }
        if scheme == "http" && !isPrivateHost(host) { throw ConfigError.insecureRemote(host) }
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { throw ConfigError.missingToken }
        return ServerConfig(baseURL: url, token: t)
    }

    /// `healthsync://configure?server=http://100.x.y.z:8787&token=...` (deploy/show_config.py prints it as a QR code).
    static func fromLink(_ url: URL) throws -> ServerConfig {
        guard url.scheme == "healthsync", url.host() == "configure",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else {
            throw ConfigError.notAConfigLink
        }
        let value = { (name: String) in items.first { $0.name == name }?.value ?? "" }
        return try make(server: value("server"), token: value("token"))
    }

    static func isPrivateHost(_ host: String) -> Bool {
        let h = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h == "localhost" || h.hasSuffix(".local") || h.hasSuffix(".ts.net") || h.hasSuffix(".home.arpa") || !h.contains(".") && !h.contains(":") {
            return true
        }
        if h.contains(":") {  // IPv6: loopback, unique-local (incl. Tailscale fd7a:…), link-local
            return h == "::1" || h.hasPrefix("fc") || h.hasPrefix("fd") || h.hasPrefix("fe80")
        }
        let octets = h.split(separator: ".").compactMap { Int($0) }
        guard octets.count == 4, octets.allSatisfy({ (0...255).contains($0) }) else { return false }
        switch (octets[0], octets[1]) {
        case (10, _), (127, _), (192, 168), (169, 254): return true
        case (172, 16...31): return true
        case (100, 64...127): return true  // CGNAT range used by Tailscale
        default: return false
        }
    }
}

enum Keychain {
    private static let service = "HealthSync"

    static func set(_ value: String, for account: String) {
        delete(account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: Data(value.utf8),
            // Background sync runs while the phone is locked (after first unlock).
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        SecItemAdd(query as CFDictionary, nil)
    }

    static func get(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(_ account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
