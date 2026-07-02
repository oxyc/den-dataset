import Foundation

/// DT-C — the classification + embedding backfill (the one-time central job that builds the
/// `tmdbId → {primaryGenre, subgenres, moods, vector}` index). The producer here owns its OWN copies of the
/// format + producer types; the app keeps an identical set in DenKit. The struct definitions are byte-for-byte
/// the same so the shipped JSON encodes identically. The published artifact is **derived labels + quantized
/// vectors only** — never raw TMDB overviews/posters (ToS-clean).

/// One item of work — a TMDB id to classify.
public struct WorklistEntry: Sendable, Equatable, Hashable {
    public let tmdbId: Int
    public let mediaType: MediaType
    public init(tmdbId: Int, mediaType: MediaType) {
        self.tmdbId = tmdbId
        self.mediaType = mediaType
    }
}

/// The single-request enrichment (`append_to_response=keywords`) feeding the classifier. Held only in
/// memory during the run; **not** part of the published index.
public struct EnrichedTitle: Sendable, Equatable {
    public let tmdbId: Int
    public let mediaType: MediaType
    public let title: String
    public let year: Int?
    /// The prose the classifier is grounded on. FP-2 re-grounds this to the WIKIPEDIA plot where one exists
    /// (ToS-clean); it falls back to the TMDB overview only where no Wikipedia plot was found.
    public let overview: String
    public let genreIDs: [Int]
    public let genreNames: [String]
    public let keywords: [Keyword]
    public let originCountry: [String]
    public let originalLanguage: String?
    public let voteCount: Int
    /// Credits (FP-2, `append_to_response=credits`) — feed the composed embedding doc, not the classifier.
    public let director: String?
    public let topCast: [String]
    /// True once `overview` holds a live Wikipedia plot (vs the TMDB overview fallback). The composed doc uses
    /// the plot only when this is set; a no-plot title composes on facts + tags with an empty Plot.
    public let hasWikiPlot: Bool

    public init(tmdbId: Int, mediaType: MediaType, title: String, year: Int?, overview: String,
                genreIDs: [Int], genreNames: [String], keywords: [Keyword], originCountry: [String],
                originalLanguage: String?, voteCount: Int,
                director: String? = nil, topCast: [String] = [], hasWikiPlot: Bool = false) {
        self.tmdbId = tmdbId; self.mediaType = mediaType; self.title = title; self.year = year
        self.overview = overview; self.genreIDs = genreIDs; self.genreNames = genreNames
        self.keywords = keywords; self.originCountry = originCountry
        self.originalLanguage = originalLanguage; self.voteCount = voteCount
        self.director = director; self.topCast = topCast; self.hasWikiPlot = hasWikiPlot
    }

    /// Return a copy with the Wikipedia plot grounded in (`overview` ← plot, `hasWikiPlot` = true).
    public func groundedOnWikiPlot(_ plot: String) -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType, title: title, year: year, overview: plot,
                      genreIDs: genreIDs, genreNames: genreNames, keywords: keywords,
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast, hasWikiPlot: true)
    }
}

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

/// Run report (coverage, primary-genre distribution, confidence histogram, cost). Emitted beside the index.
public struct RunReport: Codable, Sendable, Equatable {
    public var processed: Int = 0
    public var skippedBelowVoteFloor: Int = 0
    public var fetchFailures: Int = 0
    public var byPrimaryGenre: [String: Int] = [:]
    public var confidenceHistogram: [String: Int] = [:]   // bucket "0.5-0.6" → count
    public var llmCalls: Int = 0
    public init() {}
}

/// Resumable checkpoint — the set of already-processed ids so a re-run skips them (24h-run safety).
public struct Checkpoint: Codable, Sendable, Equatable {
    public var processed: Set<Int>
    public init(processed: Set<Int> = []) { self.processed = processed }
    public func contains(_ id: Int) -> Bool { processed.contains(id) }
    public mutating func mark(_ id: Int) { processed.insert(id) }
}

/// Parser for TMDB's **daily ID export** (`movie_ids_MM_DD_YYYY.json.gz` → JSONL of `{id, original_title,
/// popularity, …}`). We take the worklist from this static export rather than crawling `/discover` to
/// discover what exists (be light on TMDB). `vote_count` isn't in the export → it's filtered during enrich.
public enum Worklist {
    public static func parse(jsonLines: String, mediaType: MediaType) -> [WorklistEntry] {
        let decoder = JSONDecoder()
        return jsonLines.split(whereSeparator: \.isNewline).compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8),
                  let row = try? decoder.decode(ExportRow.self, from: data) else { return nil }
            return WorklistEntry(tmdbId: row.id, mediaType: mediaType)
        }
    }
    private struct ExportRow: Decodable { let id: Int }
}
