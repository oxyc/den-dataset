import CryptoKit
import Foundation

/// The known-answer test for the embedding space, on the Swift side of the producer.
///
/// `data/embed-canary.json` holds a handful of fixed texts and the exact int8 vectors den-embed is
/// supposed to return for them. `scripts/v2/embed_canary.py` is the same check for the Python embed path
/// and OWNS the file — it is what regenerates it. This reads it; it never writes it.
///
/// Why a known-answer test rather than a version comparison: `embeddingModel`, `dims` and
/// `embedderRuntime` say bge-m3, 1024 and a crate version for every generation of den-embed that has ever
/// run, including the ones whose output moved. `EmbedderGate` compares the fields `/health` reports, which
/// is strictly better and still not enough — a service can report an identical identity and return
/// different numbers, because the thing that moved was its configuration, its ONNX Runtime, or its CPU.
/// The only description of a vector space that cannot drift away from the space is a vector it produced.
public enum EmbedSpaceCanary {
    /// `DEN_EMBED_CANARY`, else `data/embed-canary.json` relative to the working directory — which is the
    /// repo root for every documented invocation of this tool.
    public static func defaultPath() -> String {
        if let raw = ProcessInfo.processInfo.environment["DEN_EMBED_CANARY"], !raw.isEmpty { return raw }
        return "data/embed-canary.json"
    }

    public struct Case: Codable, Equatable {
        public let id: String
        /// Why this text is in the set. Carried so a reader of the file knows what a case is for before
        /// deciding whether it may be dropped.
        public let why: String
        public let text: String
        /// base64 of the int8 row, one byte a dimension, two's complement.
        public let v: String

        public var vector: [Int8]? {
            guard let data = Data(base64Encoded: v) else { return nil }
            return data.map { Int8(bitPattern: $0) }
        }
    }

    public struct File: Codable {
        public let canarySet: String
        public let spaceId: String
        public let textsSha256: String
        public let dims: Int
        public let cases: [Case]

        /// The keys `embed_canary.py` writes that mean nothing here (`note`, `vectorEncoding`,
        /// `embedder`) are simply not modelled; an unlisted key decodes away without complaint.
        public static func read(_ path: String) throws -> File {
            try JSONDecoder().decode(File.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        }
    }

    /// One case's result. `cosine` exists so a reader of a failure can tell a rounding difference from a
    /// different space; it is never what decides, because int8 dot products have no tolerance for either.
    public struct CaseResult: Equatable {
        public let id: String
        public let dims: Int
        public let dimsDiffering: Int
        public let cosine: Double
        public let maxAbsDelta: Int
        public var ok: Bool { dimsDiffering == 0 }

        public var line: String {
            String(format: "%@ %@  %d/%d dims differ  cosine %.6f  max|d| %d",
                   ok ? "ok  " : "FAIL", id, dimsDiffering, dims, cosine, maxAbsDelta)
        }
    }

    /// Compare one expected row against one the service just returned.
    ///
    /// Split out from the request so it is testable with no service: this is a hard gate on the pipeline,
    /// and a comparison that silently passed would be indistinguishable from a healthy run.
    public static func compare(id: String, expected: [Int8], actual: [Int8]) -> CaseResult {
        guard expected.count == actual.count else {
            // A length difference is not a drift; nothing is comparable dimension by dimension. Reported
            // as "every dimension differs" so the count cannot read as a near miss.
            return CaseResult(id: id, dims: expected.count, dimsDiffering: max(expected.count, actual.count),
                              cosine: 0, maxAbsDelta: 0)
        }
        var differing = 0, maxDelta = 0
        var dot = 0.0, na = 0.0, nb = 0.0
        for (x, y) in zip(expected, actual) {
            if x != y {
                differing += 1
                maxDelta = max(maxDelta, abs(Int(x) - Int(y)))
            }
            dot += Double(x) * Double(y)
            na += Double(x) * Double(x)
            nb += Double(y) * Double(y)
        }
        let cos = (na == 0 || nb == 0) ? 0 : dot / (na.squareRoot() * nb.squareRoot())
        return CaseResult(id: id, dims: expected.count, dimsDiffering: differing, cosine: cos,
                          maxAbsDelta: maxDelta)
    }

    /// The digest over the case texts. A mismatch means the file's texts were edited without regenerating
    /// its vectors, so the answers describe texts that are no longer being sent — which would otherwise
    /// surface as an inexplicable space change.
    public static func textsSha256(_ cases: [Case]) -> String {
        var hasher = SHA256()
        for item in cases {
            hasher.update(data: Data(item.id.utf8))
            hasher.update(data: Data([0]))
            hasher.update(data: Data(item.text.utf8))
            hasher.update(data: Data([0x0a]))
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// `<canarySet>:<sha256 over case ids and their vectors>` — the published name of the space.
    ///
    /// The texts are deliberately not in the digest: they are the instrument, `canarySet` names the
    /// instrument, and folding them in would rename the space for a reworded probe that measured the same
    /// one. Recomputed here rather than trusted, so a hand-edited file is caught instead of published.
    public static func spaceId(canarySet: String, cases: [Case]) -> String {
        var hasher = SHA256()
        hasher.update(data: Data(canarySet.utf8))
        hasher.update(data: Data([0x0a]))
        for item in cases {
            hasher.update(data: Data(item.id.utf8))
            hasher.update(data: Data([0]))
            hasher.update(data: Data(base64Encoded: item.v) ?? Data())
            hasher.update(data: Data([0x0a]))
        }
        return canarySet + ":" + hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    public struct Failure: Error, CustomStringConvertible {
        public let description: String
    }

    /// What a verified run records beside its vectors, and what `finalize` reads to stamp the manifest.
    /// `scripts/v2/embed_canary.py --record` writes the same shape, so the Python and Swift embed paths
    /// hand `finalize` one file, not two.
    public struct Stamp: Codable {
        public let spaceId: String
        public let canarySet: String
        public let dims: Int
        public let url: String
        public let verifiedAt: String

        public init(spaceId: String, canarySet: String, dims: Int, url: String, verifiedAt: String) {
            self.spaceId = spaceId; self.canarySet = canarySet; self.dims = dims
            self.url = url; self.verifiedAt = verifiedAt
        }

        /// Tolerant decode for the same reason `DenEmbedClient.Identity` has one: this file is written by
        /// two producers and read on every later run, so a key one of them adds must not make the other's
        /// files stop loading. `embedder` is written by the Python side and deliberately not modelled.
        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            spaceId = try c.decode(String.self, forKey: .spaceId)
            canarySet = try c.decodeIfPresent(String.self, forKey: .canarySet) ?? ""
            dims = try c.decodeIfPresent(Int.self, forKey: .dims) ?? 0
            url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
            verifiedAt = try c.decodeIfPresent(String.self, forKey: .verifiedAt) ?? ""
        }

        enum CodingKeys: String, CodingKey { case spaceId, canarySet, dims, url, verifiedAt }
    }

    /// Embed every case and refuse unless all of them come back byte-identical. Returns the `Stamp` the
    /// caller records beside its vectors, so the manifest can name the space the corpus is in.
    ///
    /// `maxTokens` is checked first and is fatal on its own: it is where the service truncates, so at a
    /// different cap the long cases embed a different text and their bytes cannot mean anything. Failing
    /// on them without saying so reads as a mysterious partial failure rather than as a setting.
    @discardableResult
    public static func verify(path: String, client: DenEmbedClient, url: String,
                              log: (String) -> Void) async throws -> Stamp {
        let file = try File.read(path)
        log("embed canary: \(file.canarySet) — \(file.cases.count) cases from \(path)")

        guard textsSha256(file.cases) == file.textsSha256 else {
            throw Failure(description: "\(path): its texts were edited without regenerating its vectors, "
                + "so the committed answers describe texts that are no longer in it. Regenerate it with "
                + "scripts/v2/embed_canary.py --regenerate against a known-good embedder.")
        }
        guard spaceId(canarySet: file.canarySet, cases: file.cases) == file.spaceId else {
            throw Failure(description: "\(path): its spaceId does not match the vectors it carries — the "
                + "file was hand-edited. Regenerate it rather than correcting the digest.")
        }

        let identity = try await client.identity()
        // `try?` over an optional-returning call yields a double optional, and flattening it matters:
        // left nested, an ABSENT cap compares unequal to every real one and refuses every service.
        if let cap = (try? capOf(path: path)) ?? nil, cap != identity.maxTokens {
            throw Failure(description: "the service truncates at \(identity.maxTokens) tokens and these "
                + "answers were recorded at \(cap). Every case longer than the smaller cap embeds a "
                + "different text, so no comparison was attempted. Set MAX_TOKENS=\(cap), or regenerate "
                + "the canary at the new cap as part of a full re-embed.")
        }

        var failures: [CaseResult] = []
        for item in file.cases {
            guard let expected = item.vector else {
                throw Failure(description: "\(path): case \(item.id) has an unreadable base64 vector")
            }
            let actual = try await client.embedManyInt8([item.text])
            let result = compare(id: item.id, expected: expected, actual: actual.first ?? [])
            log("  " + result.line)
            if !result.ok { failures.append(result) }
        }

        guard failures.isEmpty else {
            let worst = failures.map(\.cosine).min() ?? 0
            let why = worst >= 0.9999
                ? "the same model doing slightly different arithmetic — a BLAS, a kernel, an ORT build. "
                    + "The vectors are close but not equal, and int8 dot products have no tolerance for "
                    + "that: it is still a different space."
                : "a different embedding space. Check the service's configuration first — MAX_TOKENS is "
                    + "the setting that has actually caused this — then the CPU architecture, the ONNX "
                    + "Runtime version and the model."
            throw Failure(description: "embed canary FAILED on \(failures.count) of \(file.cases.count) "
                + "cases (\(failures.map(\.id).joined(separator: ", "))) against \(identity.label). \(why) "
                + "Refusing to write vectors: a corpus half in another space loads, ranks, and is wrong.")
        }
        let now = ISO8601DateFormatter().string(from: Date())
        return Stamp(spaceId: file.spaceId, canarySet: file.canarySet, dims: file.dims,
                     url: url, verifiedAt: now)
    }

    /// The `max_tokens` the canary was recorded at, read from the `embedder` block `embed_canary.py`
    /// writes. Absent for a file from before that block existed, which is not a reason to refuse — the
    /// vectors themselves still decide.
    static func capOf(path: String) throws -> Int? {
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path)))
        return ((raw as? [String: Any])?["embedder"] as? [String: Any])?["max_tokens"] as? Int
    }
}
