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
        // Per-family cutoffs, recalibrated for the Opus (t02) regime. The old 0.70/0.55/0.60 was set against a
        // vote regime that emitted a flat 0.95 for every label; Opus emits an HONEST scale (0.85–0.9 headline,
        // 0.6 clearly-applies-but-secondary, 0.5 minor-but-present) and already self-censors below 0.5 in the
        // prompt — so the 0.70 blended gate guillotined correct secondary calls (Titanic → Disaster @0.5,
        // Imitation Game → War Drama/Historical @0.6). New cutoffs trust Opus's own floor:
        //  • blended 0.55 — keep clear blends, trim only the pure-0.5 borderline;
        //  • thematic 0.50 — themes are factual (heist/biopic/disaster: yes/no), low FP risk → keep all Opus flags;
        //  • moods 0.55 — keep clear moods, trim 0.5 guesses.
        // World-knowledge labels (Cult/Anime/Art House/Epic) are gated separately by vote count, not confidence.
        public init(subgenre: Double = 0.55, thematic: Double = 0.50, mood: Double = 0.55) {
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
    /// `confirmedWK`: the world-knowledge labels an Opus title-recognition pass (DT-G adjudication) confirmed
    /// for this title — they survive the vote-count gate even on the obscure tail (a genuine deep-cut cult /
    /// art-house film Opus actually knows). Empty for titles never adjudicated (the gate then applies as before).
    public func classify(rawVotes: [String], title: EnrichedTitle, confirmedWK: Set<String> = []) -> Classification? {
        let votes = rawVotes.compactMap(parse)
        guard let primary = fusedPrimaryGenre(votes) else { return nil }
        let subgenres = aggregate(votes.map(\.subgenres), title: title,
                                  threshold: { self.taxonomy.subgenres.contains($0) ? self.thresholds.subgenre : self.thresholds.thematic },
                                  inVocab: { self.taxonomy.subgenres.contains($0) || self.taxonomy.thematic.contains($0) })
        let moods = aggregate(votes.map(\.moods), title: title,
                              threshold: { _ in self.thresholds.mood },
                              inVocab: { self.taxonomy.moods.contains($0) })
        return Classification(tmdbId: title.tmdbId, mediaType: title.mediaType, primaryGenre: primary,
                              subgenres: Self.gateWorldKnowledge(subgenres, voteCount: title.voteCount, confirmedWK: confirmedWK),
                              moods: moods, source: .llm)
    }

    /// World-knowledge labels require recognising the FILM, not just its plot. The DT-G eval showed plot-only
    /// models hallucinate them on the obscure tail (`Stockholmsnatt`, vc 15 → "Cult") but are reliable above
    /// ~100 votes — so gate them on a min vote count (model-agnostic; cheaper than spending Opus everywhere).
    /// `confirmedWK` overrides the gate per-label: a WK label an Opus recognition pass confirmed for this title
    /// survives even below the floor (recovers genuine deep-tail cult/art-house Sonnet flagged and the blanket
    /// gate would otherwise strip).
    static let worldKnowledgeLabels: Set<String> = ["Cult", "Anime", "Art House", "Epic"]
    static let worldKnowledgeVoteFloor = 100
    static func gateWorldKnowledge(_ subgenres: [LabelConfidence], voteCount: Int,
                                   confirmedWK: Set<String> = []) -> [LabelConfidence] {
        voteCount >= worldKnowledgeVoteFloor ? subgenres
            : subgenres.filter { !worldKnowledgeLabels.contains($0.label) || confirmedWK.contains($0.label) }
    }

    // MARK: - Prompt

    private var systemPrompt: String {
        """
        You are a film/TV cataloguer. Assign labels ONLY from the provided controlled vocabulary. Never \
        invent labels. Pick the single dominant primary genre. Be specific; omit weak guesses (confidence \
        < 0.5). Output only JSON matching the schema. \
        Animation is a MEDIUM, not a primary genre — it is not in the vocabulary and is tracked separately. \
        For an animated title, choose its underlying STORY genre (Family, Adventure, Comedy, Action, Drama, \
        Fantasy, Science Fiction). Apply the "Anime" subgenre to Japanese animation you recognise. \
        Some labels depend on KNOWING the film, not just its plot — Cult (cult status isn't in the plot), \
        Anime, Art House, Epic. Assign these only when you recognise the title and are confident; if you are \
        reasoning purely from the plot text, omit them.
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
        // Genre name is the final tiebreak, for the same reason as `aggregate`: `tally` is a Dictionary,
        // and the rarity prior cannot separate two genres that SHARE a weight (Fantasy/Family both 1.15,
        // Documentary/History both 1.45, and three more such pairs) — so an even split between them
        // resolved by hash order, which changes between processes. `contenders` is filtered from `ranked`,
        // and `max(by:)` replaces only on a strict increase — so it returns the FIRST of equal elements and
        // fixing the order here settles the whole function.
        let ranked = tally.sorted { ($0.value, GenreRarity.weight(genreID(for: $0.key)), $1.key) >
                                    ($1.value, GenreRarity.weight(genreID(for: $1.key)), $0.key) }
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
        // Ties break on the LABEL, not on dictionary order. `sum` is a Dictionary, so `map` yields an
        // arbitrary order that Swift's per-process String hash seed changes between runs, and
        // `sorted(by:)` is not stable — so equal confidences produced an arbitrary order AND an
        // arbitrary subset surviving `prefix(3)`. Measured on the shipped corpus: 9.6% of records
        // have a tied subgenre confidence, 15.5% a tied mood, and 996 sit exactly on the cut, so the
        // same inputs shipped different labels run to run. Reproducibility is the point — `assemble
        // --force` on an unchanged batch must not change what ships.
        return grounded
            .filter { $0.confidence >= threshold($0.label) }
            .sorted { ($0.confidence, $1.label) > ($1.confidence, $0.label) }
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
