import Foundation

/// DT-C — the classification + embedding backfill (the one-time central job that builds the
/// `tmdbId → {primaryGenre, subgenres, moods, vector}` index). The producer here owns its OWN copies of the
/// format + producer types; the app keeps an identical set in DenKit. The struct definitions are byte-for-byte
/// the same so the shipped JSON encodes identically. The published artifact is **derived labels + quantized
/// vectors only** — never raw TMDB overviews/posters (ToS-clean).

/// A label with the classifier's calibrated confidence.
public struct LabelConfidence: Codable, Sendable, Equatable {
    public let label: String
    public let confidence: Double
    public init(label: String, confidence: Double) { self.label = label; self.confidence = confidence }
}

public enum LabelSource: String, Codable, Sendable { case llm, recipe, wikidata, cluster }

/// The DERIVED record published per title (the "backlog" row). No raw TMDB text.
public struct IndexRecord: Codable, Sendable, Equatable {
    public let tmdbId: Int
    public let mediaType: String
    public let primaryGenre: String
    public let subgenres: [LabelConfidence]
    public let moods: [LabelConfidence]
    public let source: LabelSource
    /// Animation is a FORMAT, not the story's genre (DT-C policy) — the primary genre is the real narrative
    /// genre and this deterministic flag (TMDB genre 16) marks animated titles, so discovery can filter
    /// animation in/out without conflating it with a genre.
    public let animated: Bool
    public init(tmdbId: Int, mediaType: String, primaryGenre: String,
                subgenres: [LabelConfidence], moods: [LabelConfidence], source: LabelSource,
                animated: Bool = false) {
        self.tmdbId = tmdbId; self.mediaType = mediaType; self.primaryGenre = primaryGenre
        self.subgenres = subgenres; self.moods = moods; self.source = source; self.animated = animated
    }
}

/// The published labels artifact (`labels-tNN.json`), keyed by `taxonomyVersion`.
public struct LabelsArtifact: Codable, Sendable, Equatable {
    public let taxonomyVersion: String
    public let count: Int
    public let records: [IndexRecord]
    public init(taxonomyVersion: String, records: [IndexRecord]) {
        self.taxonomyVersion = taxonomyVersion
        self.count = records.count
        self.records = records
    }
}

/// Resumable checkpoint — the set of already-processed ids so a re-run skips them (24h-run safety).
public struct Checkpoint: Codable, Sendable, Equatable {
    public var processed: Set<Int>
    public init(processed: Set<Int> = []) { self.processed = processed }
    public func contains(_ id: Int) -> Bool { processed.contains(id) }
    public mutating func mark(_ id: Int) { processed.insert(id) }
}
