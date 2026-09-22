import Foundation

/// DT-C — the classification + embedding backfill (the one-time central job that builds the
/// `tmdbId → {primaryGenre, subgenres, moods, vector}` index). The producer here owns its OWN copies of the
/// format + producer types; the app keeps an identical set in DenKit. The struct definitions are byte-for-byte
/// the same so the shipped JSON encodes identically. The published artifact is **derived labels + quantized
/// vectors only** — never raw TMDB overviews/posters (ToS-clean).

/// Which of the enrich pass's plot candidates won — the thing that says whether a title's plot text is about
/// that title at all.
///
/// The candidates are not interchangeable. `own` is this title's own article; `ownOtherLanguage` is the same
/// article on another Wikipedia, which is a different language but still this work; `sourceWork` is the
/// Wikidata P144 work it was adapted from — a novel, memoir or manga that tells a related story about a
/// DIFFERENT work. 1,975 of the 47,529 grounded titles are on a source work, and a Jev validity census scored
/// 1% of those `correct-screen-work` against 98.7% for own-article groundings (oxyc/den-dataset#16).
public enum PlotArticleRole: String, Codable, Sendable {
    case own
    case ownOtherLanguage = "own-other-language"
    case sourceWork = "source-work"
}

/// How a grounded title got its plot text, recorded at the moment the enrich pass decides it.
///
/// The pass knows both facts and used to discard them, leaving every later census to re-derive them from the
/// Wikidata cache. The collision census that stands in for them today sees only titles whose article grounds
/// a SECOND title — 729 of the 2,075 mis-grounded, 35% — because a title grounded on a novel that happens to
/// ground nothing else collides with nobody and is invisible by construction.
public struct PlotProvenance: Sendable, Equatable, Codable {
    public let role: PlotArticleRole
    /// Whether the article fetched was not the one asked for. Wikidata's sitelink can point at a redirect
    /// that lands in a different work's page — `Jarhead 2: Field of Fire` → `Jarhead (film)`,
    /// `Beck – Den svaga länken` → `Beck (Swedish TV series)` — so the role reads `own` and the text is about
    /// another film. 100 titles arrive this way, and no change to the source-work fall-through touches them.
    ///
    /// nil when the fetch named no page: the Enterprise structured-contents endpoint returns sections and no
    /// title, so it cannot say. Unknown, never assumed false.
    public let redirected: Bool?

    public init(role: PlotArticleRole, redirected: Bool?) {
        self.role = role
        self.redirected = redirected
    }
}

/// One TMDB keyword (grounding signal).
public struct Keyword: Hashable, Codable, Sendable {
    public let id: Int
    public let name: String
    public init(id: Int, name: String) { self.id = id; self.name = name }
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
    /// Which headings the plot was taken from, in order.
    ///
    /// The text is now a CONCATENATION — The Wire's is five `Season N` sections plus its themes — so
    /// "where did this come from?" has no single answer, and a later change to the heading rules can target
    /// the articles it actually affects instead of re-scraping the corpus to find out.
    public let plotSections: [String]
    /// Which Wikipedia the plot came from — "en" unless the title had no English article. A non-English
    /// plot is a different claim about the title, and a reviewer needs to know which language to read.
    public let plotLanguage: String?
    /// WHICH candidate won and whether a redirect moved it — the two facts that say whether `overview`
    /// describes this title or another work.
    ///
    /// nil means the record was enriched before this was recorded, which is UNKNOWN and not `own`. Nothing
    /// may infer the role from `plotArticle`: an adaptation's own article and the novel's are both just
    /// names, and a redirect leaves no trace in the name at all.
    public let plotProvenance: PlotProvenance?
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
                noPlotReason: String? = nil, plotSections: [String] = [],
                plotLanguage: String? = nil, plotProvenance: PlotProvenance? = nil) {
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
        self.plotSections = plotSections
        self.plotLanguage = plotLanguage
        self.plotProvenance = plotProvenance
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
