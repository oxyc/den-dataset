import Foundation

/// One row of the append-only vector store: a title's id and its int8 vector as ints.
///
/// Lives beside `IndexRecord` rather than in the CLI because the two are written in lockstep, line for
/// line, and the rule that keeps them in lockstep (below) is what the shipped corpus's correctness rests
/// on — not a detail of how the CLI happens to spell a file.
public struct VectorRow: Codable, Sendable, Equatable {
    public let tmdbId: Int
    public let v: [Int]

    public init(tmdbId: Int, v: [Int]) {
        self.tmdbId = tmdbId
        self.v = v
    }
}

/// The labels store and the vector store are two append-only files, zipped positionally from the moment
/// they are read: line *i* of one is assumed to describe line *i* of the other, and nothing downstream
/// carries the vector's own id to check it against.
///
/// That assumption held only by luck. `flush` writes a label line then its vector line, so a kill between
/// the two leaves the files one line apart — and repairing that by truncating to the shorter file is what
/// the code did. But a kill *mid-write* leaves a TRUNCATED final line, which still counts as a line: the
/// two files then look equal-length while the last vector belongs to no title, and every title after the
/// tear ships carrying its neighbour's vector. Nothing detected it — the vectors were all 1024-dim, the
/// counts matched, the manifest hashed cleanly, and the corpus simply retrieved the wrong titles.
public enum StoreIntegrity {
    /// The length of the longest prefix in which both lines parse AND name the same title.
    ///
    /// Deliberately positional and deliberately strict: it stops at the FIRST disagreement rather than
    /// trying to resynchronise. A store that has diverged in the middle is not repairable by dropping
    /// lines — everything after the divergence is suspect — so the honest repair is to keep the part that
    /// is provably intact and re-embed the rest, which is resumable and cheap.
    public static func alignedPrefix(labels: [String], vectors: [String]) -> Int {
        let decoder = JSONDecoder()
        var n = 0
        while n < min(labels.count, vectors.count) {
            guard let labelData = labels[n].data(using: .utf8),
                  let vectorData = vectors[n].data(using: .utf8),
                  let record = try? decoder.decode(IndexRecord.self, from: labelData),
                  let row = try? decoder.decode(VectorRow.self, from: vectorData),
                  record.tmdbId == row.tmdbId
            else { break }
            n += 1
        }
        return n
    }

    /// What to do about a divergence — and specifically, when NOT to act on one.
    ///
    /// Checking alignment instead of line counts means a disagreement can be found anywhere in the file,
    /// and truncating to it would delete everything after. A store whose 50th line is unparseable would
    /// lose 37,000 rows; one written before a field was added to `IndexRecord` (whose synthesized
    /// `Decodable` requires every key) would decode nothing and be erased outright. Both are hours of
    /// den-embed time, destroyed by a repair that runs before any work starts, with an atomic replace
    /// leaving nothing to recover. Repairing on line count alone could never do that, so the alignment
    /// check needs a bound the count check did not.
    public enum Repair: Equatable {
        case nothingToDo
        /// An interrupted write: drop the unpaired tail, keeping this many rows.
        case truncate(keeping: Int)
        /// Too large to be a tear. This is a corrupt store, and truncating it would be data loss.
        case refuse(dropping: Int, divergesAtLine: Int)
    }

    /// A tear loses at most the in-flight chunk, so anything beyond `maxDrop` is not one.
    public static func repair(labelCount: Int, vectorCount: Int, aligned: Int, maxDrop: Int) -> Repair {
        let dropping = max(labelCount, vectorCount) - aligned
        if dropping <= 0 { return .nothingToDo }
        if dropping > maxDrop { return .refuse(dropping: dropping, divergesAtLine: aligned + 1) }
        return .truncate(keeping: aligned)
    }

    /// The first index at which two already-parsed stores disagree about which title they describe, or
    /// nil when they agree throughout. Used by `finalize` as the last gate before shipping.
    public static func firstMisalignment(records: [IndexRecord], rows: [VectorRow]) -> Int? {
        (0..<min(records.count, rows.count)).first { records[$0].tmdbId != rows[$0].tmdbId }
    }
}

/// Writing `dataset.meta.json` without discarding the keys the `DatasetMeta` struct does not model.
///
/// The manifest is a closed `Codable` struct, so decode-then-re-encode drops every key it does not
/// declare — and the shipped manifest carries twelve such keys (the premise index's six, the facet blob's
/// three, the metadata sidecar's three), written by a tool that is no longer in this repo. Running
/// `metadata`, or re-running `finalize`, silently erased them: `publish-dataset.sh` still uploaded the
/// blobs because they match its glob, and its pre-flight only checks that the files the manifest *names*
/// exist — so a manifest naming none of them passed, and den-atlas lost premise search and facets with no
/// error on either side.
public enum ManifestMerge {
    /// Merge freshly-encoded manifest JSON over whatever is already on disk. A key the struct OWNS always
    /// wins — including by being absent, which is how it says "there is no sidecar". A key the struct does
    /// not own is carried through untouched.
    ///
    /// `owned` has to be passed in, because "absent from the new JSON" cannot distinguish the two cases on
    /// its own: `JSONEncoder` omits nil Optionals, so a `metadataFile` the struct deliberately left nil
    /// looked exactly like one of the unmodelled keys and inherited the PREVIOUS run's value. That is worse
    /// than the loss this function was written to stop — the manifest then names a sidecar built for an
    /// older datasetVersion and swears to its old sha256. Both consumers hard-verify that sha
    /// (den/deploy/atlas-dataset-sync.sh, and the app's index store), so the refresh does not degrade, it
    /// stops: den-atlas keeps serving the old dataset and the 4-hourly timer fails silently forever.
    ///
    /// Merging the rest rather than enumerating the twelve keys that go missing, because enumerating them
    /// fixes today's loss and leaves the next key someone adds to repeat it.
    public static func merge(new: Data, existing: Data?, owned: Set<String>) throws -> Data {
        guard var merged = try JSONSerialization.jsonObject(with: new) as? [String: Any] else { return new }
        if let existing,
           let old = try? JSONSerialization.jsonObject(with: existing) as? [String: Any] {
            for (key, value) in old where merged[key] == nil && !owned.contains(key) {
                merged[key] = value
            }
        }
        return try JSONSerialization.data(withJSONObject: merged, options: [.prettyPrinted, .sortedKeys])
    }
}
