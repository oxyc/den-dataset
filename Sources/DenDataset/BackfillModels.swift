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
    /// The prose the classifier is grounded on — a **Wikipedia** plot (CC0) or empty. TMDB's overview is
    /// never carried here; see `overviewChars`.
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
    /// TV showrunners. Sourced from Wikidata (P170) where it has them and TMDB `created_by` otherwise —
    /// kept separate from `director`, which TMDB leaves null for nearly all series, so this is the only
    /// credit that links a series to its creator's other work.
    public let createdBy: [String]
    /// Runtime in minutes, from Wikidata (P2047) — ~93% of films carry it, and the enriched record has no
    /// other source for it. Series values are per-episode and much sparser (~37%), so this is a film fact.
    public let runtimeMinutes: Int?
    /// True once `overview` holds a live Wikipedia plot. The composed doc uses the plot only when this is
    /// set; a no-plot title composes on facts + tags with an empty Plot.
    public let hasWikiPlot: Bool
    /// How long TMDB's own overview was — the LENGTH, never the text.
    ///
    /// TMDB's terms (§1.C) speak directly to using their content with a machine-learning application, so the
    /// overview may not be embedded, sent to a classifier, or kept in a dataset. The only thing the pipeline
    /// ever needed from it is whether a title is a stub (a one-line placeholder cannot be classified), and a
    /// character count answers that without retaining a word. `overview` therefore holds a Wikipedia plot or
    /// nothing at all, which makes the rule structural rather than a flag someone has to remember.
    public let overviewChars: Int
    /// The enwiki article the plot was read from, and the revision it was read at. Together they are what
    /// makes a refresh incremental: a later pass asks for current revids 50 articles at a time and re-fetches
    /// only what moved. Without them every refresh must re-read all ~40k plots to learn that most are
    /// unchanged — a ~7h pass to find the ~4% that actually shifted.
    ///
    /// `plotArticle` may be the article of the SOURCE work rather than this title's own (see the P144/P179
    /// fallback in the enrich pass), which is exactly why it is recorded rather than re-derived.
    public let plotArticle: String?
    public let plotRevId: Int?
    /// WHY there is no plot, when there is none: `noArticle`, `noSection`, `belowFloor`, `fetchFailed`.
    ///
    /// `hasWikiPlot: false` alone is what makes every improvement cost a full re-scrape. The four causes
    /// want four different fixes — a non-English sitelink, a heading rule or lead fallback, a threshold
    /// decision, a retry — and collapsed into one boolean the only safe answer to "who should I re-run?" is
    /// "all of them". 19,542 titles currently carry that boolean and nothing else.
    public let noPlotReason: String?

    public init(tmdbId: Int, mediaType: MediaType, title: String, year: Int?, overview: String,
                genreIDs: [Int], genreNames: [String], keywords: [Keyword], originCountry: [String],
                originalLanguage: String?, voteCount: Int,
                director: String? = nil, topCast: [String] = [], createdBy: [String] = [],
                runtimeMinutes: Int? = nil, hasWikiPlot: Bool = false,
                plotArticle: String? = nil, plotRevId: Int? = nil, overviewChars: Int = 0,
                noPlotReason: String? = nil) {
        self.tmdbId = tmdbId; self.mediaType = mediaType; self.title = title; self.year = year
        self.overview = overview; self.genreIDs = genreIDs; self.genreNames = genreNames
        self.keywords = keywords; self.originCountry = originCountry
        self.originalLanguage = originalLanguage; self.voteCount = voteCount
        self.director = director; self.topCast = topCast; self.createdBy = createdBy
        self.runtimeMinutes = runtimeMinutes
        self.hasWikiPlot = hasWikiPlot
        self.plotArticle = plotArticle; self.plotRevId = plotRevId
        self.overviewChars = overviewChars
        self.noPlotReason = noPlotReason
    }

    /// Return a copy with the Wikipedia plot grounded in (`overview` ← plot, `hasWikiPlot` = true).
    public func groundedOnWikiPlot(_ plot: String, article: String? = nil, revId: Int? = nil) -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType, title: title, year: year, overview: plot,
                      genreIDs: genreIDs, genreNames: genreNames, keywords: keywords,
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast, createdBy: createdBy,
                      runtimeMinutes: runtimeMinutes, hasWikiPlot: true,
                      plotArticle: article, plotRevId: revId, overviewChars: overviewChars,
                      noPlotReason: nil)
    }

    /// Return a copy recording WHY no plot was found, so a later pass can re-run only the subset a given
    /// fix reaches rather than the whole corpus.
    public func notingNoPlot(_ reason: String) -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType, title: title, year: year, overview: overview,
                      genreIDs: genreIDs, genreNames: genreNames, keywords: keywords,
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast, createdBy: createdBy,
                      runtimeMinutes: runtimeMinutes, hasWikiPlot: false,
                      plotArticle: plotArticle, plotRevId: plotRevId, overviewChars: overviewChars,
                      noPlotReason: reason)
    }

    /// Fold in the facts that ride along on the Wikidata hop.
    ///
    /// Wikidata's creators WIN over TMDB's `created_by` where it has them. That is the same rule the plot
    /// already follows and for the same reason: this text is fed to an embedder, and TMDB's terms (§1.C)
    /// speak directly to using their content with a machine-learning application, while Wikidata is CC0.
    /// TMDB stays as the fallback for the ~63% of series Wikidata has no creator for.
    public func mergingWikidata(runtimeMinutes wikiRuntime: Int?, creators: [String]) -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType, title: title, year: year, overview: overview,
                      genreIDs: genreIDs, genreNames: genreNames, keywords: keywords,
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast,
                      createdBy: creators.isEmpty ? createdBy : creators,
                      runtimeMinutes: wikiRuntime ?? runtimeMinutes, hasWikiPlot: hasWikiPlot,
                      plotArticle: plotArticle, plotRevId: plotRevId, overviewChars: overviewChars,
                      noPlotReason: noPlotReason)
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

/// One row of the on-device METADATA SIDECAR (`metadata-<datasetVersion>.json`) — the minimal fields a poster
/// card needs, so a semantic/ANN neighbour (which the index returns as a bare tmdbId) renders WITHOUT a
/// per-result TMDB detail call. Ships as a ≤6-month synced cache (never bundled): poster_path + title are
/// factual/artwork references, distinct from the expressive overviews the labels pipeline strips.
public struct PosterMeta: Codable, Sendable, Equatable {
    public let tmdbId: Int
    public let mediaType: String     // "movie"/"tv" (pathSegment), consistent with IndexRecord
    public let title: String
    public let posterPath: String?
    public let year: Int?
    public init(tmdbId: Int, mediaType: String, title: String, posterPath: String?, year: Int?) {
        self.tmdbId = tmdbId; self.mediaType = mediaType; self.title = title
        self.posterPath = posterPath; self.year = year
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
