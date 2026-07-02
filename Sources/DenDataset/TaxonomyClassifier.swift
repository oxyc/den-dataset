import Foundation

/// One title's classification (DT-C) — the labels that go into the index after aggregation + calibration.
public struct Classification: Sendable, Equatable {
    public let tmdbId: Int
    public let mediaType: MediaType
    public let primaryGenre: String
    public let subgenres: [LabelConfidence]
    public let moods: [LabelConfidence]
    public let source: LabelSource

    public func indexRecord(animated: Bool = false) -> IndexRecord {
        IndexRecord(tmdbId: tmdbId, mediaType: mediaType.pathSegment, primaryGenre: primaryGenre,
                    subgenres: subgenres, moods: moods, source: source, animated: animated)
    }
}

/// Classifies one enriched title into the taxonomy via a cheap LLM (DT-classification-prompt.md), with
/// **self-consistency (n=3 majority vote)**, a **fused primary genre** (LLM votes + IDF rarity tie-break),
/// **per-family calibrated thresholds**, **grounding as a bonus** (TMDB-keyword agreement only *raises*
/// confidence — absence never lowers it), and strict **off-vocabulary rejection**.
public struct TaxonomyClassifier: Sendable {
    public struct Thresholds: Sendable {
        public var subgenre: Double      // blended (genres/blended ≥ 0.90)
        public var thematic: Double
        public var mood: Double          // moods ≥ 0.75
        // Per-family cutoffs locked from the golden precision/recall sweep (DT-C calibration), not guessed:
        //  • blended 0.70 — the FP-knee: drops the worst false positives (precision flat ~0.84 below it,
        //    only reaching the 0.90 target at 0.83 where recall collapses);
        //  • thematic 0.55 — precision is ~0.95 at every cutoff, so maximize recall;
        //  • moods 0.60 — strictly dominates the old 0.75 (higher precision AND recall on the golden set).
        // The earlier 0.90/0.80/0.75 cutoffs dropped moderate-confidence-but-correct labels (e.g. Time Travel
        // on Back to the Future), leaving titles absent from their signature discovery row.
        public init(subgenre: Double = 0.70, thematic: Double = 0.55, mood: Double = 0.60) {
            self.subgenre = subgenre; self.thematic = thematic; self.mood = mood
        }
    }

    let taxonomy: Taxonomy
    let llm: any LLMClient
    let samples: Int
    let thresholds: Thresholds
    let groundingBonus: Double
    /// Two primary-genre vote fractions within this margin are a "tie" → IDF rarity breaks toward the
    /// rarer/specific genre (0.55 Drama / 0.45 Crime → Crime).
    let tieMargin: Double

    public init(taxonomy: Taxonomy = .current, llm: any LLMClient, samples: Int = 3,
                thresholds: Thresholds = Thresholds(), groundingBonus: Double = 0.1, tieMargin: Double = 0.25) {
        self.taxonomy = taxonomy; self.llm = llm; self.samples = max(1, samples)
        self.thresholds = thresholds; self.groundingBonus = groundingBonus; self.tieMargin = tieMargin
    }

    /// Run `samples` LLM passes, aggregate, calibrate, ground → the calibrated classification (nil only if
    /// no pass yielded an in-vocabulary primary genre).
    public func classify(_ title: EnrichedTitle) async throws -> Classification? {
        var raws: [String] = []
        for _ in 0..<samples { raws.append(try await llm.complete(request(for: title))) }
        return classify(rawVotes: raws, title: title)
    }

    /// Aggregate pre-collected raw classification JSON (one string per self-consistency pass) into the
    /// calibrated classification — the seam used by the **Haiku-subagent backend** (DT-C, chosen): Opus
    /// orchestrates + collects the votes, the calibrated judgment (majority vote, fused primary genre with
    /// the IDF rarity tie-break, per-family thresholds, grounding bonus, off-vocab rejection) stays here so
    /// it is identical to the in-process path and stays unit-tested. Off-vocabulary / unparseable passes are
    /// dropped. nil only when no pass yielded an in-vocabulary primary genre.
    public func classify(rawVotes: [String], title: EnrichedTitle) -> Classification? {
        let votes = rawVotes.compactMap(parse)
        guard let primary = fusedPrimaryGenre(votes) else { return nil }
        let subgenres = aggregate(votes.map(\.subgenres), title: title,
                                  threshold: { self.taxonomy.subgenres.contains($0) ? self.thresholds.subgenre : self.thresholds.thematic },
                                  inVocab: { self.taxonomy.subgenres.contains($0) || self.taxonomy.thematic.contains($0) })
        let moods = aggregate(votes.map(\.moods), title: title,
                              threshold: { _ in self.thresholds.mood },
                              inVocab: { self.taxonomy.moods.contains($0) })
        return Classification(tmdbId: title.tmdbId, mediaType: title.mediaType, primaryGenre: primary,
                              subgenres: subgenres, moods: moods, source: .llm)
    }

    // MARK: - Prompt

    private var systemPrompt: String {
        """
        You are a film/TV cataloguer. Assign labels ONLY from the provided controlled vocabulary. Never \
        invent labels. Pick the single dominant primary genre. Be specific; omit weak guesses (confidence \
        < 0.5). No anime labels. Output only JSON matching the schema.
        """
    }

    func request(for title: EnrichedTitle) -> LLMRequest {
        let user = """
        VOCABULARY:
          primary_genre: [\(taxonomy.primaryGenres.joined(separator: ", "))]
          subgenres:     [\((taxonomy.subgenres + taxonomy.thematic).joined(separator: ", "))]
          moods:         [\(taxonomy.moods.joined(separator: ", "))]

        TITLE: \(title.title) (\(title.year.map(String.init) ?? "?")) — \(title.mediaType.pathSegment)
        TMDB_GENRES: \(title.genreNames.joined(separator: ", "))
        KEYWORDS: \(title.keywords.map(\.name).joined(separator: ", "))
        ORIGIN: \(title.originCountry.joined(separator: "/")) / \(title.originalLanguage ?? "?")
        OVERVIEW: \(title.overview)

        Return JSON:
        { "primary_genre": "<one>",
          "subgenres": [{"label":"<from list>","confidence":0-1}, ...≤3],
          "moods":     [{"label":"<from list>","confidence":0-1}, ...≤3] }
        """
        return LLMRequest(system: systemPrompt, user: user, maxTokens: 512, temperature: 0)
    }

    // MARK: - Parse

    struct Vote: Sendable {
        let primaryGenre: String?
        let subgenres: [LabelConfidence]
        let moods: [LabelConfidence]
    }

    func parse(_ raw: String) -> Vote? {
        // Tolerate ```json fences / surrounding prose — extract the first {...} block.
        guard let start = raw.firstIndex(of: "{"), let end = raw.lastIndex(of: "}"),
              start < end, let data = String(raw[start...end]).data(using: .utf8),
              let dto = try? JSONDecoder().decode(RawClassification.self, from: data) else { return nil }
        let primary = dto.primaryGenre.flatMap { taxonomy.isPrimaryGenre($0) ? $0 : nil }
        return Vote(primaryGenre: primary,
                    subgenres: dto.subgenres?.map { LabelConfidence(label: $0.label, confidence: $0.confidence) } ?? [],
                    moods: dto.moods?.map { LabelConfidence(label: $0.label, confidence: $0.confidence) } ?? [])
    }

    private struct RawClassification: Decodable {
        let primaryGenre: String?
        let subgenres: [RawLabel]?
        let moods: [RawLabel]?
        struct RawLabel: Decodable { let label: String; let confidence: Double }
        enum CodingKeys: String, CodingKey { case primaryGenre = "primary_genre", subgenres, moods }
    }

    // MARK: - Aggregation

    /// Fused primary genre: majority vote, with the IDF rarity prior breaking near-ties toward the rarer
    /// genre. nil if no vote produced an in-vocabulary primary.
    func fusedPrimaryGenre(_ votes: [Vote]) -> String? {
        let primaries = votes.compactMap(\.primaryGenre)
        guard !primaries.isEmpty else { return nil }
        var tally: [String: Int] = [:]
        for genre in primaries { tally[genre, default: 0] += 1 }
        let total = Double(primaries.count)
        let ranked = tally.sorted { ($0.value, GenreRarity.weight(genreID(for: $0.key))) >
                                    ($1.value, GenreRarity.weight(genreID(for: $1.key))) }
        guard let top = ranked.first else { return nil }
        // Near-tie with the runner-up → pick the rarer of the close contenders (the Drama→Crime fix).
        let contenders = ranked.filter { Double(top.value - $0.value) / total <= tieMargin }
        return contenders.max { GenreRarity.weight(genreID(for: $0.key)) < GenreRarity.weight(genreID(for: $1.key)) }?.key
            ?? top.key
    }

    /// Average each label's confidence across passes (absent in a pass = 0 for that pass), add the grounding
    /// bonus when TMDB keywords agree, reject off-vocab, threshold per family, keep the top 3.
    private func aggregate(_ perVote: [[LabelConfidence]], title: EnrichedTitle,
                           threshold: (String) -> Double, inVocab: (String) -> Bool) -> [LabelConfidence] {
        var sum: [String: Double] = [:]
        for vote in perVote {
            for item in vote where inVocab(item.label) { sum[item.label, default: 0] += item.confidence }
        }
        let denom = Double(max(1, perVote.count))
        let grounded = sum.map { label, total -> LabelConfidence in
            var confidence = total / denom
            if groundingAgrees(label: label, keywords: title.keywords) {
                confidence = min(1.0, confidence + groundingBonus)   // bonus only — never lowers
            }
            return LabelConfidence(label: label, confidence: confidence)
        }
        return grounded
            .filter { $0.confidence >= threshold($0.label) }
            .sorted { $0.confidence > $1.confidence }
            .prefix(3)
            .map { $0 }
    }

    // MARK: - Grounding (bonus only)

    /// True when a recipe-grounded label's keyword set overlaps the title's TMDB keywords. Sparse by design
    /// (only recipe-backed concepts) — absence is silent, presence is a small bonus.
    func groundingAgrees(label: String, keywords: [Keyword]) -> Bool {
        guard let recipeKeywords = Self.groundingKeywords[label.lowercased()], !recipeKeywords.isEmpty else { return false }
        return !Set(keywords.map(\.id)).isDisjoint(with: recipeKeywords)
    }

    /// label (lowercased) → grounding keyword ids. Baked from `RecipeCatalog` (an app UI construct that stays
    /// in the app) into `GroundingKeywords.map` so the producer needs no app types.
    static let groundingKeywords: [String: Set<Int>] = GroundingKeywords.map

    private func genreID(for name: String) -> Int {
        Self.genreNameToID[name] ?? -1
    }
    private static let genreNameToID: [String: Int] = {
        var map: [String: Int] = [:]
        for (id, name) in GenreCatalog.movie { map[name] = id }
        return map
    }()
}
