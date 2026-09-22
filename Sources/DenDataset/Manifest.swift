import Foundation

/// The manifest den-atlas and the app read: what the dataset is, which files carry it, and what they hash
/// to. Lives in the library rather than the CLI because `ManifestMerge` needs `ownedKeys` and because a
/// closed struct silently dropping keys has already taken two shipped features down.
public struct DatasetMeta: Codable {
    public let datasetVersion: String
    public let taxonomyVersion: String
    public let embeddingModel: String
    public let dims: Int
    public let count: Int
    public let quantization: String
    public let labelsFile: String
    public let vectorsFile: String
    public let labelsGzFile: String
    public let labelsSha256: String
    public let labelsBytes: Int
    public let vectorsSha256: String
    public let vectorsBytes: Int
    public let builtAt: String
    public let lastModifiedHttp: String
    // The retired poster sidecar. Nothing sets these any more, and they stay declared anyway: an OWNED key
    // the new manifest leaves nil is dropped by `ManifestMerge`, while an unowned one is carried forward —
    // so un-declaring them would let a rewrite inherit a previous run's `metadataFile` and vouch for its
    // sha, which both consumers hard-verify.
    public let metadataFile: String?
    public let metadataSha256: String?
    public let metadataBytes: Int?
    // What actually embedded this corpus, as den-embed's /health reported it during the build (see
    // DenEmbedClient.Identity). `embeddingModel` and `dims` say "bge-m3" and 1024 for every generation of
    // the service, so they cannot express the couplings that DO move vectors — the ORT 1.22 → 1.28 bump and
    // the Rust rewrite's token cap. Absent means a corpus built before this was recorded.
    public let embedderRuntime: String?
    public let embedderMaxTokens: Int?
    // The embedding SPACE, as `<canarySet>:<digest>` — see `EmbedSpaceCanary`. The three fields above
    // describe the service that was asked; this one describes what it answered. They are not the same
    // claim, and only this one can be compared by something that never saw the build: a consumer embeds
    // the same committed texts through its own den-embed and gets the same string, or it does not. Absent
    // means the corpus was built before the canary existed, or by an embed path that does not run it.
    public let embeddingSpace: String?

    /// Declared explicitly so `ownedKeys` cannot fall behind the struct.
    ///
    /// The compile-time guarantee only exists because every stored property above is a `let` with NO
    /// DEFAULT. Swift refuses to synthesize `Codable` for such a property when `CodingKeys` omits it — but
    /// it happily skips a `var x: T? = nil`, encoding nothing and reporting nothing. Every optional here
    /// used to have that shape, so the "adding a field is a compile error" claim was simply false, and the
    /// next field added would have gone missing from both the JSON and `ownedKeys` — which is exactly the
    /// combination that makes `ManifestMerge` inherit a stale value.
    enum CodingKeys: String, CodingKey, CaseIterable {
        case datasetVersion, taxonomyVersion, embeddingModel, dims, count, quantization
        case labelsFile, vectorsFile, labelsGzFile, labelsSha256, labelsBytes, vectorsSha256, vectorsBytes
        case builtAt, lastModifiedHttp
        case metadataFile, metadataSha256, metadataBytes
        case embedderRuntime, embedderMaxTokens, embeddingSpace
    }

    public init(datasetVersion: String, taxonomyVersion: String, embeddingModel: String, dims: Int,
                count: Int, quantization: String, labelsFile: String, vectorsFile: String,
                labelsGzFile: String, labelsSha256: String, labelsBytes: Int, vectorsSha256: String,
                vectorsBytes: Int, builtAt: String, lastModifiedHttp: String,
                metadataFile: String? = nil, metadataSha256: String? = nil, metadataBytes: Int? = nil,
                embedderRuntime: String? = nil, embedderMaxTokens: Int? = nil,
                embeddingSpace: String? = nil) {
        self.datasetVersion = datasetVersion; self.taxonomyVersion = taxonomyVersion
        self.embeddingModel = embeddingModel; self.dims = dims; self.count = count
        self.quantization = quantization; self.labelsFile = labelsFile; self.vectorsFile = vectorsFile
        self.labelsGzFile = labelsGzFile; self.labelsSha256 = labelsSha256; self.labelsBytes = labelsBytes
        self.vectorsSha256 = vectorsSha256; self.vectorsBytes = vectorsBytes
        self.builtAt = builtAt; self.lastModifiedHttp = lastModifiedHttp
        self.metadataFile = metadataFile; self.metadataSha256 = metadataSha256
        self.metadataBytes = metadataBytes
        self.embedderRuntime = embedderRuntime; self.embedderMaxTokens = embedderMaxTokens
        self.embeddingSpace = embeddingSpace
    }

    /// The keys this struct is authoritative for — including when it omits one. Everything else in the file
    /// belongs to a producer that is not in this repo and must survive a rewrite untouched.
    public static let ownedKeys = Set(CodingKeys.allCases.map(\.rawValue))
}
