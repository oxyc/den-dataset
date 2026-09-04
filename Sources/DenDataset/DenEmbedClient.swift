import Foundation

/// Client for the **den-embed** service (FP-2) — the single embedding path for the whole Den stack. It serves
/// bge-m3 (1024-dim dense, L2-normalized, int8-quantized ×127) for BOTH the corpus (this producer, batch) and
/// live search queries (the app), so corpus and query vectors are guaranteed comparable. The int8
/// quantization lives in the SERVICE and nowhere else: this client returns the service's int8 vector verbatim
/// and must NOT re-quantize it.
///
/// Contract: `POST /embed` `{"text":"…"}` → `{"vector":[int8×dims],"dims":Int,"model":String}` (POST, not the
/// GET ?text= form, so a multi-thousand-char plot doc can't 414 as a query param).
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
    /// POST the text in the JSON body (not the GET ?text= form): a composed corpus doc can carry a multi-
    /// thousand-char plot that would risk a 414 (URI too long) as a query param. Retries transient 5xx/timeouts.
    public func embedInt8(_ text: String) async throws -> [Int8] {
        var request = URLRequest(url: baseURL.appendingPathComponent("embed"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(EmbedRequestBody(text: text))

        return try await Transport.retrying {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                throw DenEmbedError.http(http.statusCode)
            }
            return try Self.decode(data)
        }
    }

    private struct EmbedRequestBody: Encodable { let text: String }

    /// Embed many documents in one round-trip via `POST /embed/batch` — the service batches them through the
    /// model (far faster than N single calls for a corpus build). Returns int8 vectors 1:1 with `texts`.
    public func embedManyInt8(_ texts: [String]) async throws -> [[Int8]] {
        guard !texts.isEmpty else { return [] }
        var request = URLRequest(url: baseURL.appendingPathComponent("embed/batch"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(BatchRequestBody(texts: texts))

        return try await Transport.retrying {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                throw DenEmbedError.http(http.statusCode)
            }
            let payload = try JSONDecoder().decode(BatchResponse.self, from: data)
            return payload.vectors.map { $0.map { Int8(clamping: $0) } }
        }
    }

    private struct BatchRequestBody: Encodable { let texts: [String] }
    private struct BatchResponse: Decodable { let vectors: [[Int]] }

    /// Who is embedding — model, dimensions, service version and token cap, as `/health` reports them.
    ///
    /// The corpus and live queries MUST be embedded by the same thing (docs/OPERATE.md's alignment rule),
    /// and `model` + `dims` alone cannot tell two of our own runtimes apart: both say bge-m3/1024 while
    /// returning different vectors for the same text. `runtime` and `maxTokens` are what distinguish
    /// them, so the corpus build records this and refuses to mix two.
    public struct Identity: Codable, Equatable, Sendable {
        public let model: String
        public let dims: Int
        /// Which generation of vectors the service produces — see den-embed's `VECTOR_EPOCH`. 0 for the
        /// Python service that predates the field, which is correctly not equal to any Rust build.
        public let vectorEpoch: Int
        /// Build identity for logs and messages. Deliberately NOT part of equality below.
        public let runtime: String
        public let maxTokens: Int

        /// One line, for the manifest and for error messages.
        public var label: String {
            "\(model)/\(dims) epoch \(vectorEpoch) (\(runtime)) max_tokens=\(maxTokens)"
        }

        /// Two services are the same embedder when they produce the same NUMBERS — not when they are the
        /// same build. The version string moves on every release, so comparing it would invalidate a
        /// 37.5k-title corpus over a log-line fix and demand hours of re-embedding; den-embed carries an
        /// epoch that is bumped only when output actually moves. `maxTokens` is part of it because
        /// truncation changes the vector for anything longer than the cap.
        public static func == (a: Identity, b: Identity) -> Bool {
            a.model == b.model && a.dims == b.dims
                && a.vectorEpoch == b.vectorEpoch && a.maxTokens == b.maxTokens
        }

        /// Parse a `/health` body. Split out from the request so the mapping is testable without a service:
        /// this is a hard gate on the pipeline, and the one field whose JSON name differs from its Swift
        /// name (`max_tokens`) is exactly the kind of thing that fails silently as a nil default.
        /// Tolerant decode. `Identity` is not only a wire type — it is written to `index/embedder.json` and
        /// read back on every later run — so a new key with synthesized `Decodable` makes every file
        /// already on disk fail with `keyNotFound`. `ClassifyCheckpoint.Totals` carries a comment about
        /// exactly this and I added `vectorEpoch` without it: `finalize` then refused to ship the out-dir,
        /// and `recordEmbedder`, which reads with `try?`, saw the file as ABSENT and told the operator to
        /// write a literal that itself does not decode — advice that loops forever, with a full 37.5k-title
        /// re-embed as the only way out.
        ///
        /// Absent means the file predates the field, which is the same thing an absent `vector_epoch` from
        /// /health means: epoch 0, the Python generation.
        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            model = try c.decode(String.self, forKey: .model)
            dims = try c.decode(Int.self, forKey: .dims)
            vectorEpoch = try c.decodeIfPresent(Int.self, forKey: .vectorEpoch) ?? 0
            runtime = try c.decode(String.self, forKey: .runtime)
            maxTokens = try c.decode(Int.self, forKey: .maxTokens)
        }

        enum CodingKeys: String, CodingKey { case model, dims, vectorEpoch, runtime, maxTokens }

        public init(model: String, dims: Int, vectorEpoch: Int = 0, runtime: String, maxTokens: Int) {
            self.model = model; self.dims = dims; self.vectorEpoch = vectorEpoch
            self.runtime = runtime; self.maxTokens = maxTokens
        }

        public init(healthJSON data: Data) throws {
            let health = try JSONDecoder().decode(HealthResponse.self, from: data)
            // A service predating the `runtime` field is a real answer, not an error — that is the Python
            // era, which is the generation the currently shipped corpus came from.
            self.init(model: health.model ?? "unknown", dims: health.dims ?? 0,
                      vectorEpoch: health.vectorEpoch ?? 0,
                      runtime: health.runtime ?? "pre-3.0.0", maxTokens: health.maxTokens ?? 0)
        }
    }

    public func identity() async throws -> Identity {
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return try await Transport.retrying {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                throw DenEmbedError.http(http.statusCode)
            }
            return try Identity(healthJSON: data)
        }
    }

    struct HealthResponse: Decodable {
        let model: String?
        let dims: Int?
        let vectorEpoch: Int?
        let runtime: String?
        let maxTokens: Int?

        enum CodingKeys: String, CodingKey {
            case model, dims, runtime
            case vectorEpoch = "vector_epoch"
            case maxTokens = "max_tokens"
        }
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
