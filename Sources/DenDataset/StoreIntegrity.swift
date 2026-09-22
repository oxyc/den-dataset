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

/// One composed document, for `embed-corpus --dump-docs`: the embedder's input, carried to whichever
/// machine will embed it.
///
/// Keyed `mediaType:tmdbId`, never a bare id — 1,097 ids in this corpus are both a film and a series, and
/// this file exists precisely to travel between machines, where a bare-id or positional join has nothing
/// left to correct itself against.
public struct DocRow: Codable, Sendable, Equatable {
    public let key: String
    public let doc: String

    public init(key: String, doc: String) {
        self.key = key
        self.doc = doc
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
}
