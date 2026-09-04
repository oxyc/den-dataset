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
    // Metadata sidecar (optional; set by the `metadata` command AFTER finalize). Absent ⇒ no sidecar → the
    // server serves labels+vectors only and the app hydrates poster cards via TMDB as before (no regression).
    public let metadataFile: String?
    public let metadataSha256: String?
    public let metadataBytes: Int?
    // What actually embedded this corpus, as den-embed's /health reported it during the build (see
    // DenEmbedClient.Identity). `embeddingModel` and `dims` say "bge-m3" and 1024 for every generation of
    // the service, so they cannot express the couplings that DO move vectors — the ORT 1.22 → 1.28 bump and
    // the Rust rewrite's token cap. Absent means a corpus built before this was recorded.
    public let embedderRuntime: String?
    public let embedderMaxTokens: Int?

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
        case embedderRuntime, embedderMaxTokens
    }

    public init(datasetVersion: String, taxonomyVersion: String, embeddingModel: String, dims: Int,
                count: Int, quantization: String, labelsFile: String, vectorsFile: String,
                labelsGzFile: String, labelsSha256: String, labelsBytes: Int, vectorsSha256: String,
                vectorsBytes: Int, builtAt: String, lastModifiedHttp: String,
                metadataFile: String? = nil, metadataSha256: String? = nil, metadataBytes: Int? = nil,
                embedderRuntime: String? = nil, embedderMaxTokens: Int? = nil) {
        self.datasetVersion = datasetVersion; self.taxonomyVersion = taxonomyVersion
        self.embeddingModel = embeddingModel; self.dims = dims; self.count = count
        self.quantization = quantization; self.labelsFile = labelsFile; self.vectorsFile = vectorsFile
        self.labelsGzFile = labelsGzFile; self.labelsSha256 = labelsSha256; self.labelsBytes = labelsBytes
        self.vectorsSha256 = vectorsSha256; self.vectorsBytes = vectorsBytes
        self.builtAt = builtAt; self.lastModifiedHttp = lastModifiedHttp
        self.metadataFile = metadataFile; self.metadataSha256 = metadataSha256
        self.metadataBytes = metadataBytes
        self.embedderRuntime = embedderRuntime; self.embedderMaxTokens = embedderMaxTokens
    }

    /// A copy naming a metadata sidecar. `metadata` patches an existing manifest rather than rebuilding it.
    public func namingSidecar(file: String, sha256: String, bytes: Int) -> DatasetMeta {
        DatasetMeta(datasetVersion: datasetVersion, taxonomyVersion: taxonomyVersion,
                    embeddingModel: embeddingModel, dims: dims, count: count, quantization: quantization,
                    labelsFile: labelsFile, vectorsFile: vectorsFile, labelsGzFile: labelsGzFile,
                    labelsSha256: labelsSha256, labelsBytes: labelsBytes, vectorsSha256: vectorsSha256,
                    vectorsBytes: vectorsBytes, builtAt: builtAt, lastModifiedHttp: lastModifiedHttp,
                    metadataFile: file, metadataSha256: sha256, metadataBytes: bytes,
                    embedderRuntime: embedderRuntime, embedderMaxTokens: embedderMaxTokens)
    }

    /// The keys this struct is authoritative for — including when it omits one. Everything else in the file
    /// belongs to a producer that is not in this repo and must survive a rewrite untouched.
    public static let ownedKeys = Set(CodingKeys.allCases.map(\.rawValue))
}

public struct ClassifyCheckpoint: Codable {
    /// Keyed `"movie:123"` / `"tv:123"`, because TMDB's movie and TV id namespaces OVERLAP — the same
    /// reason `EnrichCheckpoint` is keyed that way, which this one never was.
    ///
    /// As a bare `Set<Int>` it made `assemble` skip any TV title whose id had already been classified as a
    /// movie. Measured on the shipped corpus: of 7,832 enriched TV titles, 940 share an id with a shipped
    /// movie, and **all 940 are missing** from the index — against a 43.5% baseline drop rate for the
    /// rest. Buffy, Doctor Who, Star Trek, Avatar: The Last Airbender, Cheers. Every one was enriched, with
    /// the TMDB, Wikidata and Wikipedia calls paid for, and then silently never classified. `finalize`'s
    /// de-dup key was already media-qualified, so this was the only place at fault.
    public var done: Set<String> = []
    /// Set when the file on disk was the legacy bare-Int form. Those ints cannot be re-qualified from the
    /// checkpoint alone — nothing recorded which media each belonged to — so `assemble` rebuilds the set
    /// from the labels store, which does, and rewrites the checkpoint in the keyed form.
    public var needsMigration = false
    /// How many ids the legacy checkpoint held, for the rebuild to sanity-check itself against. Load-time
    /// only — like `needsMigration`, it never reaches disk.
    public var legacyCount = 0
    public var totals = Totals()

    public static func key(_ media: String, _ id: Int) -> String { "\(media):\(id)" }

    public init() {}

    /// Tolerant decode, mirroring `EnrichCheckpoint`: a legacy `[Int]` still loads rather than resetting
    /// progress to empty and re-classifying (and re-embedding) the whole out-dir.
    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let keyed = try? c.decode(Set<String>.self, forKey: .done) {
            done = keyed
        } else {
            // The ids themselves are unusable as keys, but the COUNT is not: `assemble` rebuilds `done`
            // from the labels store, and without something to compare against, a rebuild that produced
            // nothing (an absent or emptied store) would silently re-classify and re-embed the whole
            // out-dir. That is precisely the reset the loud-checkpoint guard refuses to do by design.
            legacyCount = (try c.decode(Set<Int>.self, forKey: .done)).count
            needsMigration = true
        }
        totals = try c.decodeIfPresent(Totals.self, forKey: .totals) ?? Totals()
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(done, forKey: .done)
        try c.encode(totals, forKey: .totals)
    }

    enum CodingKeys: String, CodingKey { case done, totals }

    public struct Totals: Codable {
        public var noPrimary = 0, missingVotes = 0

        public init() {}

        /// Tolerant decode, matching `EnrichCheckpoint.Totals`: a checkpoint written before a field existed
        /// must still load. Synthesized `Decodable` requires every key, so adding a third counter would make
        /// `assemble` refuse every checkpoint on disk — a hard stop for a field nobody needs to have.
        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            noPrimary = try c.decodeIfPresent(Int.self, forKey: .noPrimary) ?? 0
            missingVotes = try c.decodeIfPresent(Int.self, forKey: .missingVotes) ?? 0
        }

        enum CodingKeys: String, CodingKey { case noPrimary, missingVotes }

        public mutating func merge(noPrimary: Int, missingVotes: Int) {
            self.noPrimary += noPrimary; self.missingVotes += missingVotes
        }
    }
}



/// The sidecar's row order feeds `metadataSha256`, and the app folds that into its sync key — so an
/// unstable order costs every device a ~4.6 MB re-download of a file that did not change.
///
/// A free function, in the library, because it is the ONE line of the sidecar write that can be wrong: as
/// a closure inside the CLI it was unreachable from the tests, and mutation proved it — reverting it to
/// `$0.tmdbId < $1.tmdbId` left the whole suite green while a test claiming to cover it restated the
/// comparator locally instead of calling it.
public enum SidecarOrder {
    /// A TOTAL order. `tmdbId` alone is not one here: 940 ids in the corpus are both a movie and a series,
    /// and Swift's `sort` is unstable, so ties landed in TaskGroup completion order.
    public static func before(_ a: PosterMeta, _ b: PosterMeta) -> Bool {
        (a.tmdbId, a.mediaType) < (b.tmdbId, b.mediaType)
    }

    public static func sorted(_ rows: [PosterMeta]) -> [PosterMeta] { rows.sorted(by: before) }
}

/// Whether a run may append to an existing store, given what built it and what is about to.
///
/// Free function for the same reason: the decision lived inline in `recordEmbedder`, and disabling it
/// wholesale left every test green.
public enum EmbedderGate {
    public enum Decision: Equatable {
        /// The store was built by this same embedder — append.
        case matches
        /// A different embedder built it. Appending would mix two generations into one corpus.
        case mismatch(previous: String, now: String)
        /// Rows exist but nothing recorded what built them, so it cannot be known.
        case unknownProvenance
        /// Nothing there yet — record this identity and proceed.
        case firstUse
    }

    public static func decide(previous: DenEmbedClient.Identity?, now: DenEmbedClient.Identity,
                              storeHasRows: Bool) -> Decision {
        if let previous {
            return previous == now ? .matches : .mismatch(previous: previous.label, now: now.label)
        }
        // Adopting the CURRENT service as an existing store's identity would write a guess down as a fact,
        // and the guard would then pass forever on the one corpus that actually has the problem.
        return storeHasRows ? .unknownProvenance : .firstUse
    }
}
