import Foundation

enum APIError: LocalizedError {
    case http(Int, String)
    case notHTTP

    var errorDescription: String? {
        switch self {
        case .http(401, _): "API 토큰이 올바르지 않습니다 (401)."
        case .http(let code, let body): "서버 오류 \(code): \(body.prefix(200))"
        case .notHTTP: "서버 응답을 해석할 수 없습니다."
        }
    }
}

struct APIClient {
    let config: ServerConfig

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 60
        c.timeoutIntervalForResource = 300
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    private func request(_ path: String) -> URLRequest {
        var r = URLRequest(url: config.baseURL.appending(path: path))
        r.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        return r
    }

    private func check(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse else { throw APIError.notHTTP }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.http(http.statusCode, String(decoding: data, as: UTF8.self))
        }
    }

    /// Checks address + token. Returns the server's database name.
    func ping() async throws -> String {
        let (data, response) = try await Self.session.data(for: request("api/v1/ping"))
        try check(response, data)
        struct Pong: Decodable { let database: String }
        return try JSONDecoder().decode(Pong.self, from: data).database
    }

    func ingest(_ payload: IngestPayload) async throws -> IngestResponse {
        let body = try PayloadCoder.deflate(try PayloadCoder.encode(payload))
        var r = request("api/v1/ingest")
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue("deflate", forHTTPHeaderField: "Content-Encoding")
        let (data, response) = try await Self.session.upload(for: r, from: body)
        try check(response, data)
        return try JSONDecoder().decode(IngestResponse.self, from: data)
    }
}
