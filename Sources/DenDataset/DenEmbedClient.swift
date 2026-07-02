import Foundation

/// Client for the **den-embed** service (FP-2) — the single embedding path for the whole Den stack. It serves
/// bge-m3 (1024-dim dense, L2-normalized, int8-quantized ×127) for BOTH the corpus (this producer, batch) and
/// live search queries (the app), so corpus and query vectors are guaranteed comparable. The int8
/// quantization lives in the SERVICE and nowhere else: this client returns the service's int8 vector verbatim
/// and must NOT re-quantize it.
///
/// Contract: `GET /embed?text=<urlenc>` → `{"vector":[int8×dims],"dims":Int,"model":String}`.
public struct DenEmbedClient: Sendable {
    private let baseURL: URL
    private let session: URLSession

    /// Default base URL from `DEN_EMBED_URL`, else `http://localhost:8791`.
    public static func defaultBaseURL() -> URL {
        if let raw = ProcessInfo.processInfo.environment["DEN_EMBED_URL"],
           !raw.isEmpty, let url = URL(string: raw) {
            return url
        }
        return URL(string: "http://localhost:8791")!
    }

    public init(baseURL: URL = DenEmbedClient.defaultBaseURL(), session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Embed one document to the service's canonical int8 vector. Returned as-is (already quantized upstream).
    public func embedInt8(_ text: String) async throws -> [Int8] {
        guard var components = URLComponents(url: baseURL.appendingPathComponent("embed"),
                                             resolvingAgainstBaseURL: false) else {
            throw DenEmbedError.badURL
        }
        components.queryItems = [URLQueryItem(name: "text", value: text)]
        guard let url = components.url else { throw DenEmbedError.badURL }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw DenEmbedError.http(http.statusCode)
        }
        return try Self.decode(data)
    }

    /// Decode the `{"vector":[...]}` payload to `[Int8]`. The service always sends values in [-127, 127].
    static func decode(_ data: Data) throws -> [Int8] {
        let payload = try JSONDecoder().decode(EmbedResponse.self, from: data)
        return payload.vector.map { Int8(clamping: $0) }
    }

    private struct EmbedResponse: Decodable {
        let vector: [Int]
        let dims: Int?
        let model: String?
    }
}

public enum DenEmbedError: Error, Sendable {
    case badURL
    case http(Int)
}
