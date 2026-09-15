import Foundation

/// Live movie/TV enrichment from **Wikipedia** (FP-2) — the fresh, ToS-clean plot source that replaces
/// shipping TMDB overviews. NEVER a dump: every call hits the live public APIs. Two hops:
///
///  1. `wikidata(forTMDBIds:mediaType:)` — ONE Wikidata SPARQL POST maps a batch of TMDB ids to their
///     Wikidata film/series entity, the linked English Wikipedia article title, the IMDb id, and the runtime
///     + creators that ride along for free (CC0, so free of TMDB's terms).
///     Movies key on `wdt:P4947` (TMDB movie id), TV on `wdt:P4983` (TMDB series id); `wdt:P345` is the IMDb
///     id; the enwiki article is `?a schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/>`.
///  2. `plot(articleTitle:)` — the article's Plot/Synopsis section as plain prose. With a Wikimedia Enterprise
///     token (`WIKIMEDIA_ENTERPRISE_TOKEN`) it uses the pre-sectioned structured-contents endpoint; otherwise
///     it uses the public action API (`action=parse`) to find the Plot section, fetch its wikitext, and strip
///     the wiki markup to prose. Returns nil when the article has no plot section → the caller composes on
///     facts + tags only (never skips the title).
///
/// Incremental top-up (documented; not run here): discover freshly-changed films via a Wikidata
/// `schema:dateModified` filter on the entity, and re-embed a title when its article `revid` changes
/// (`action=parse&prop=revid`).
public struct WikipediaSource: Sendable {
    /// The mapping returned per TMDB id: the enwiki article title (for the plot hop), the IMDb id, and the
    /// facts that ride along on the same query for free.
    public struct Mapping: Sendable, Equatable {
        public let article: String?
        /// The article of the work this was ADAPTED FROM (P144) — a novel, memoir or manga that tells the
        /// same story. Used only when `article` yields no plot section; see the query comment for why the
        /// franchise a title merely belongs to (P179) is NOT an acceptable substitute.
        public let sourceArticle: String?
        public let imdb: String?
        /// Minutes (P2047). Measured coverage: ~93% of films, ~37% of series — and a series' value is
        /// per-episode, so treat it as a film fact and let TV fall back elsewhere.
        public let runtimeMinutes: Int?
        /// Showrunners (P170), sorted for stability. Measured coverage: ~37% of a RANDOM series sample —
        /// famous shows are near-complete, the long tail is not. So this complements TMDB's `created_by`
        /// rather than replacing it: free where present, TMDB fills the rest.
        public let creators: [String]

        public init(article: String?, imdb: String?, runtimeMinutes: Int? = nil, creators: [String] = [],
                    sourceArticle: String? = nil) {
            self.article = article
            self.sourceArticle = sourceArticle
            self.imdb = imdb
            self.runtimeMinutes = runtimeMinutes
            self.creators = creators
        }
    }

    /// A polite, identifying User-Agent is REQUIRED by the Wikimedia APIs (unidentified traffic is throttled).
    public static let userAgent = "den-dataset/1.0 (github.com/oxyc/den-dataset)"

    private let session: URLSession
    private let sparqlEndpoint: URL
    private let actionAPI: URL
    private let enterpriseToken: String?
    /// Article and mapping responses served from disk when present — see `WikiCachePolicy`. nil disables.
    private let cache: ResponseCache?

    public init(session: URLSession = .shared,
                sparqlEndpoint: URL = URL(string: "https://query.wikidata.org/sparql")!,
                actionAPI: URL = URL(string: "https://en.wikipedia.org/w/api.php")!,
                enterpriseToken: String? = ProcessInfo.processInfo.environment["WIKIMEDIA_ENTERPRISE_TOKEN"],
                cache: ResponseCache? = WikiCachePolicy.cache()) {
        self.session = session
        self.sparqlEndpoint = sparqlEndpoint
        self.actionAPI = actionAPI
        self.enterpriseToken = (enterpriseToken?.isEmpty == false) ? enterpriseToken : nil
        self.cache = cache
    }

    // MARK: - Wikidata mapping

    /// One SPARQL POST → `tmdbId → Mapping` for the whole batch. Missing ids are simply absent from the map.
    public func wikidata(forTMDBIds ids: [Int], mediaType: MediaType) async throws -> [Int: Mapping] {
        let unique = Array(Set(ids)).sorted()
        guard !unique.isEmpty else { return [:] }
        let property = mediaType == .tv ? "P4983" : "P4947"   // TMDB series id / TMDB movie id
        let values = unique.map { "\"\($0)\"" }.joined(separator: " ")
        // Runtime and creators ride along on the hop that already happens — no extra request, and Wikidata is
        // CC0, so neither fact carries TMDB's terms with it. Both are OPTIONAL: a title missing them still
        // returns its article, which is what this call exists for.
        let query = """
        SELECT ?tmdb ?article ?sourceArticle ?imdb ?runtime ?creatorLabel WHERE {
          VALUES ?tmdb { \(values) }
          ?film wdt:\(property) ?tmdb .
          OPTIONAL { ?film wdt:P345 ?imdb . }
          OPTIONAL { ?film wdt:P2047 ?runtime . }
          OPTIONAL { ?film wdt:P170 ?creator . }
          OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }
          # SOURCE-WORK FALLBACK. An adaptation's own article is often production-and-episodes with no plot:
          # "Attack on Titan (TV series)" is Series overview / Season 1-4 / Cast, while the STORY lives on the
          # article for the work it adapts. P144 (based on) names that work, and it tells the same story, so
          # its plot describes this title: 13 Reasons Why reads its plot off the novel, The Pacific off the
          # memoir, Shooter off Point of Impact.
          #
          # P179 (part of the series) was tried here too and REMOVED. A franchise sibling is not the same
          # story, so it produced confidently wrong plots — Angel grounded on Buffy, Torchwood on Doctor Who,
          # Xena on Hercules, Bates Motel on Psycho. Measured over the titles this fallback newly grounded:
          # 322 came from a P144 source work, against 66 reachable only through P179. Dropping those 66 costs
          # 0.16% of the grounded corpus and removes every such attribution.
          OPTIONAL { ?film wdt:P144 ?basedOn .
                     ?sourceArticle schema:about ?basedOn ; schema:isPartOf <https://en.wikipedia.org/> . }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
        }
        ORDER BY ?tmdb ?article
        """

        // Keyed on the QUERY TEXT, so it survives a re-run and changes the moment the query does. This is the
        // most valuable entry in the cache: WDQS is the pipeline's flakiest dependency — it was throttling to
        // one request a minute during this work — and a mapping is stable, since a title's article and IMDb
        // id rarely move. A cached mapping lets a re-run proceed through a WDQS outage entirely.
        let cacheKey = cache?.key(path: "sparql", query: ["q": query])
        if let cacheKey, let hit = cache?.read(cacheKey) {
            // Parse failures fall through to a live fetch rather than throwing: a cached body that no longer
            // decodes must not be able to fail a run.
            if let mapping = try? Self.parseWikidata(hit) { return mapping }
        }

        var components = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "format", value: "json")]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
        request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
        request.httpBody = Data(query.utf8)

        let data = try await send(request)
        let mapping = try Self.parseWikidata(data)
        // Written only after parsing succeeded — an HTML maintenance page from WDQS decodes as nothing and
        // must never be stored, or the outage outlives itself.
        if let cacheKey { cache?.write(cacheKey, data) }
        return mapping
    }

    /// Decode a SPARQL JSON result into `tmdbId → Mapping`. Pure + testable (fixture JSON → mapping).
    ///
    /// THROWS on an undecodable body rather than returning an empty map. `enrich` treats an absent id in a
    /// SUCCESSFUL result as definitive (the title has no Wikipedia article, checkpoint it and move on) and a
    /// transport failure as transient (retry the batch). Collapsing an unparseable 200 into "no bindings"
    /// merged those two: a WDQS maintenance page or an HTML error body would make every title in the batch
    /// tags-only, checkpointed, and never re-grounded — and with `--require-wiki-plot` the whole batch is
    /// then dropped from the shipped index.
    static func parseWikidata(_ data: Data) throws -> [Int: Mapping] {
        let root: SPARQLResult
        do {
            root = try JSONDecoder().decode(SPARQLResult.self, from: data)
        } catch {
            throw WikidataError.unparseableResponse(String(decoding: data.prefix(200), as: UTF8.self))
        }
        var map: [Int: Mapping] = [:]
        for binding in root.results.bindings {
            guard let tmdbRaw = binding.tmdb?.value, let tmdbId = Int(tmdbRaw) else { continue }
            let article = (binding.article?.value).flatMap { Self.articleTitle(fromURL: $0) }
            // The work this ADAPTS (P144) — its plot is this story. A franchise sibling's is not.
            let source = (binding.sourceArticle?.value).flatMap { Self.articleTitle(fromURL: $0) }
            let imdb = binding.imdb?.value
            // Wikidata stores runtime as a decimal ("96" / "96.0"); a series may carry several (a 50- and a
            // 70-minute cut). The SMALLEST is the useful one for "have I got time for this".
            let runtime = (binding.runtime?.value).flatMap { Double($0) }.map { Int($0.rounded()) }
            // A film binds once PER creator (and per IMDb id), so creators accumulate across rows rather than
            // first-wins — taking only the first would silently drop the second Duffer brother.
            let existing = map[tmdbId]
            var creators = existing?.creators ?? []
            if let creator = binding.creatorLabel?.value, !creator.isEmpty, !creators.contains(creator) {
                creators.append(creator)
            }
            map[tmdbId] = Mapping(
                article: existing?.article ?? article,
                imdb: existing?.imdb ?? imdb,
                runtimeMinutes: [existing?.runtimeMinutes, runtime].compactMap { $0 }.min(),
                creators: creators.sorted(),
                sourceArticle: existing?.sourceArticle ?? source)
        }
        return map
    }

    // MARK: - CC0 doc facts (director + genre)

    /// The two clauses of the embedding doc that still came from TMDB. Everything else in the CC0 shape is
    /// already clean: the plot is Wikipedia, the themes are our own tags, and `Created by` is P170 on the
    /// mapping hop above.
    public struct DocFacts: Sendable, Equatable {
        /// P57. Measured: films 97%, series 34-37% — and for a series a missing value is usually CORRECT,
        /// since a series has no single director. TMDB is thinner still at 25% for series, so this is a gain
        /// rather than a regression: a quarter of series get a director they do not have today.
        public let directors: [String]
        /// P136, media suffix stripped. NOT TMDB's 19-genre vocabulary — Wikidata is finer (sitcom,
        /// telenovela, romantic comedy, biographical) and mean overlap with TMDB measured 0.40.
        public let genres: [String]

        public init(directors: [String] = [], genres: [String] = []) {
            self.directors = directors
            self.genres = genres
        }
    }

    /// Director (P57) and genre (P136) for a batch, as TWO requests rather than two more OPTIONALs on the
    /// mapping query. Both properties are multi-valued, and one query with several OPTIONALs returns their
    /// CROSS PRODUCT — which times out at WDQS on a 100-id batch.
    public func docFacts(forTMDBIds ids: [Int], mediaType: MediaType) async throws -> [Int: DocFacts] {
        let unique = Array(Set(ids)).sorted()
        guard !unique.isEmpty else { return [:] }
        let property = mediaType == .tv ? "P4983" : "P4947"
        let values = unique.map { "\"\($0)\"" }.joined(separator: " ")

        // The label language is "en,mul", NOT "en". Wikidata has moved proper names to the `mul`
        // (multilingual) language code — Christopher Nolan (Q25191) has NO English rdfs:label, only `mul`
        // plus the scripts that genuinely differ (ar/he/ja/ru). Asking for "en" alone makes the label service
        // return the bare Q-id, which `parseLabelled` drops as an identifier, so the director vanishes with
        // no error at all. Measured: that silently cost Inception, The Dark Knight and The Prestige theirs.
        func fetch(_ prop: String) async throws -> [Int: [String]] {
            let query = """
            SELECT ?tmdb ?vLabel WHERE {
              VALUES ?tmdb { \(values) }
              ?film wdt:\(property) ?tmdb .
              ?film wdt:\(prop) ?v .
              SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
            }
            ORDER BY ?tmdb ?vLabel
            """
            var components = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
            components.queryItems = [URLQueryItem(name: "format", value: "json")]
            var request = URLRequest(url: components.url!)
            request.httpMethod = "POST"
            request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
            request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
            request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
            request.httpBody = Data(query.utf8)
            return try Self.parseLabelled(try await send(request))
        }

        let directors = try await fetch("P57")
        let rawGenres = try await fetch("P136")
        var out: [Int: DocFacts] = [:]
        for id in unique {
            let genres = Array(Set((rawGenres[id] ?? []).map(Self.strippedGenre).filter { !$0.isEmpty }))
            let dirs = directors[id] ?? []
            if dirs.isEmpty && genres.isEmpty { continue }   // absent, not empty — unknown is not "none"
            out[id] = DocFacts(directors: dirs, genres: genres.sorted())
        }
        return out
    }

    /// `tmdbId → [label]` for a one-property query. Values accumulate per id rather than first-wins, since a
    /// film binds once per value.
    static func parseLabelled(_ data: Data) throws -> [Int: [String]] {
        let root: SPARQLResult
        do {
            root = try JSONDecoder().decode(SPARQLResult.self, from: data)
        } catch {
            throw WikidataError.unparseableResponse(String(decoding: data.prefix(200), as: UTF8.self))
        }
        var map: [Int: [String]] = [:]
        for binding in root.results.bindings {
            guard let raw = binding.tmdb?.value, let id = Int(raw),
                  let label = binding.vLabel?.value, !label.isEmpty else { continue }
            // A label the SERVICE could not resolve comes back as the bare Q-id. That is an identifier, not a
            // name, and embedding "Q1379241" as a director teaches the model nothing — drop it.
            if label.hasPrefix("Q"), Int(label.dropFirst()) != nil { continue }
            if !(map[id] ?? []).contains(label) { map[id, default: []].append(label) }
        }
        return map
    }

    /// `"science fiction film"` → `"science fiction"`. Wikidata appends the medium to genre labels where TMDB
    /// does not, and comparing the raw strings makes a vocabulary that largely DOES line up look like it
    /// shares nothing: mean overlap with TMDB measured 0.01 before this strip and 0.40 after.
    public static func strippedGenre(_ label: String) -> String {
        let media = ["film", "movie", "television series", "tv series", "series", "anime"]
        var s = label.lowercased().trimmingCharacters(in: .whitespaces)
        var changed = true
        while changed {
            changed = false
            for word in media where s.hasSuffix(" " + word) {
                s.removeLast(word.count + 1)
                s = s.trimmingCharacters(in: .whitespaces)
                changed = true
            }
        }
        // A value that is ONLY the medium says nothing — every film is a film. The caller drops empties, so
        // returning "" here is how "film" and "television series" stop reaching the doc as genres.
        return media.contains(s) ? "" : s
    }

    // MARK: - Facts sidecar

    /// Run one `WikidataFacts.Spec` over a batch. One property per request: several multi-valued OPTIONALs in
    /// a single query return their cross product, which times out at WDQS on a 100-id batch.
    public func facts(spec: WikidataFacts.Spec, forTMDBIds ids: [Int],
                      mediaType: MediaType) async throws -> [Int: WikidataFacts.FieldValue] {
        let unique = Array(Set(ids)).sorted()
        guard !unique.isEmpty else { return [:] }
        let property = mediaType == .tv ? "P4983" : "P4947"
        let values = unique.map { "\"\($0)\"" }.joined(separator: " ")

        let select: String
        let body: String
        switch spec.kind {
        case .entity:
            select = "?tmdb ?v"
            body = "?film wdt:\(spec.prop) ?v ."
        case .iso:
            // The ISO code lives on the VALUE, not the film: country Q30 carries "US" on P297.
            select = "?tmdb ?code"
            body = "?film wdt:\(spec.prop) ?v . ?v wdt:\(spec.isoVia ?? "P297") ?code ."
        case .literal:
            select = "?tmdb ?v"
            body = "?film wdt:\(spec.prop) ?v ."
        case .dateP:
            select = "?tmdb ?v ?prec"
            body = "?film p:\(spec.prop) ?st . ?st psv:\(spec.prop) ?node . "
                 + "?node wikibase:timeValue ?v ; wikibase:timePrecision ?prec ."
        }
        let query = """
        SELECT \(select) WHERE {
          VALUES ?tmdb { \(values) }
          ?film wdt:\(property) ?tmdb .
          \(body)
        }
        """
        var components = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "format", value: "json")]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
        request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
        request.httpBody = Data(query.utf8)
        return try Self.parseFacts(try await send(request), spec: spec)
    }

    static func parseFacts(_ data: Data,
                           spec: WikidataFacts.Spec) throws -> [Int: WikidataFacts.FieldValue] {
        let root: SPARQLResult
        do {
            root = try JSONDecoder().decode(SPARQLResult.self, from: data)
        } catch {
            throw WikidataError.unparseableResponse(String(decoding: data.prefix(200), as: UTF8.self))
        }
        var lists: [Int: [String]] = [:]
        var dates: [Int: (String, String)] = [:]
        for b in root.results.bindings {
            guard let raw = b.tmdb?.value, let id = Int(raw) else { continue }
            switch spec.kind {
            case .dateP:
                guard let v = b.v?.value, let precRaw = b.prec?.value, let prec = Int(precRaw),
                      let name = WikidataFacts.precisionName(prec),
                      let trimmed = WikidataFacts.trimDate(v, precision: name) else { continue }
                // Several dates are normal (a festival premiere and a wide release). Keep the EARLIEST, and
                // prefer the finer precision when they tie, so "newest first" has something real to sort on.
                if let existing = dates[id], existing.0 <= trimmed { continue }
                dates[id] = (trimmed, name)
            case .iso:
                if let code = b.code?.value.uppercased(), !(lists[id] ?? []).contains(code) {
                    lists[id, default: []].append(code)
                }
            case .entity:
                guard let v = b.v?.value else { continue }
                let qid = String(v.split(separator: "/").last ?? "")
                guard qid.hasPrefix("Q") else { continue }
                if !(lists[id] ?? []).contains(qid) { lists[id, default: []].append(qid) }
            case .literal:
                guard let v = b.v?.value else { continue }
                if !(lists[id] ?? []).contains(v) { lists[id, default: []].append(v) }
            }
        }
        var out: [Int: WikidataFacts.FieldValue] = [:]
        for (id, v) in lists where !v.isEmpty {
            out[id] = WikidataFacts.FieldValue.collapse(v.sorted(), spec: spec)
        }
        for (id, d) in dates { out[id] = .date(d.0, precision: d.1) }
        return out
    }

    /// Q-id → `{"en": name}` for the entities a facts build actually references, resolved once rather than
    /// inlined per title: a corpus of 38.5k titles references the same actors and genres over and over.
    public func entityNames(_ qids: [String], batch: Int = 300) async throws -> [String: [String: String]] {
        var out: [String: [String: String]] = [:]
        for start in stride(from: 0, to: qids.count, by: batch) {
            let slice = Array(qids[start..<min(start + batch, qids.count)])
            let values = slice.map { "wd:\($0)" }.joined(separator: " ")
            let query = """
            SELECT ?item ?itemLabel WHERE {
              VALUES ?item { \(values) }
              SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
            }
            """
            var components = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
            components.queryItems = [URLQueryItem(name: "format", value: "json")]
            var request = URLRequest(url: components.url!)
            request.httpMethod = "POST"
            request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
            request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
            request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
            request.httpBody = Data(query.utf8)
            let root: SPARQLResult
            do {
                root = try JSONDecoder().decode(SPARQLResult.self, from: try await send(request))
            } catch {
                throw WikidataError.unparseableResponse("entityNames batch at \(start)")
            }
            for b in root.results.bindings {
                guard let uri = b.item?.value, let name = b.itemLabel?.value else { continue }
                let qid = String(uri.split(separator: "/").last ?? "")
                // An unresolved label comes back as the Q-id itself. Recording "Q1234": "Q1234" would make
                // every consumer render an identifier as a name, so leave it out and let them show nothing.
                if name == qid { continue }
                out[qid] = ["en": name]
            }
        }
        return out
    }

    /// `https://en.wikipedia.org/wiki/Inception` → `Inception`; underscores → spaces, percent-decoded.
    static func articleTitle(fromURL urlString: String) -> String? {
        guard let marker = urlString.range(of: "/wiki/") else { return nil }
        let raw = String(urlString[marker.upperBound...])
        let decoded = raw.removingPercentEncoding ?? raw
        let title = decoded.replacingOccurrences(of: "_", with: " ")
        return title.isEmpty ? nil : title
    }

    struct SPARQLResult: Decodable {
        let results: Results
        struct Results: Decodable { let bindings: [Binding] }
        struct Binding: Decodable {
            let tmdb: Cell?
            let article: Cell?
            let sourceArticle: Cell?
            let imdb: Cell?
            let runtime: Cell?
            let creatorLabel: Cell?
            let vLabel: Cell?
            let item: Cell?
            let itemLabel: Cell?
            let v: Cell?
            let code: Cell?
            let prec: Cell?
            let alias: Cell?
            let orig: Cell?
            let label: Cell?
            let pid: Cell?
            let typeLabel: Cell?
        }
        struct Cell: Decodable { let value: String }
    }

    // MARK: - Plot

    /// A plot and the article revision it was read from. The revision is what makes an incremental refresh
    /// possible: with it stored, a later pass asks Wikipedia for current revids in batches of 50 and re-fetches
    /// ONLY the articles that moved, instead of re-reading all ~40k plots to discover that ~60% are unchanged.
    public struct PlotFetch: Sendable, Equatable {
        public let text: String
        /// nil when the source could not report one (the Enterprise path) — an unknown revision must be
        /// treated as "changed", since the alternative is silently pinning a stale plot forever.
        public let revId: Int?
        /// The article the text actually came from, AFTER redirect resolution. The requested title is not
        /// good enough: we send `redirects=1`, so asking for a redirect returns the target's content and the
        /// target's revid. Storing the redirect's name beside the target's revid would make the bulk-revid
        /// refresh compare against a redirect page — which effectively never changes — and pin the stale
        /// plot forever, the exact failure the revision is recorded to prevent.
        public let resolvedArticle: String?
        /// Which headings the text came from, in the order they were taken.
        ///
        /// Recorded because the text is now a CONCATENATION — The Wire's is five `Season N` sections plus
        /// its themes — so "where did this come from?" has no single answer, and a later change to the
        /// heading rules can target the articles it actually affects instead of the whole corpus.
        public let sections: [String]

        public init(text: String, revId: Int?, resolvedArticle: String? = nil, sections: [String] = []) {
            self.text = text
            self.revId = revId
            self.resolvedArticle = resolvedArticle
            self.sections = sections
        }
    }

    /// The article's Plot/Synopsis section as plain prose, or nil if the article has no such section.
    public func plot(articleTitle: String) async throws -> PlotFetch? {
        if enterpriseToken != nil, let found = try? await enterpriseProse(articleTitle: articleTitle) {
            return PlotFetch(text: found.text, revId: nil, resolvedArticle: articleTitle,
                             sections: found.sections)
        }
        return try await actionAPIPlot(articleTitle: articleTitle)
    }

    /// Headings whose prose is the STORY but which no single-section search would ever find.
    ///
    /// A long-running series does not have a "Plot" section. The Wire's article is 66,190 characters and
    /// carries none of `plotSectionNames` — its plot is 22,892 characters spread over `Season 1 (2002)` …
    /// `Season 5 (2008)`, nested under `Episodes`. Taking the first matching section and stopping returned
    /// NOTHING for it, and for the rest of serial television: of 33 sampled no-plot TV titles with >=50
    /// votes, the old rule grounded 0 and this one grounds 15.
    ///
    /// Matched as a PREFIX on the heading's first word, so "Season 1 (2002)", "Series 2" and "Part One"
    /// all qualify while "Seasonal marketing" does not (the word boundary is required).
    static let serialSectionPrefixes = ["season", "series", "part", "volume", "arc", "chapter", "episode"]

    /// Headings that describe what the work is ABOUT rather than what happens in it. Weaker evidence than a
    /// plot, and kept separate so the two can be weighted — or the second dropped — independently.
    ///
    /// Deliberately NARROW. "Style" is not here: The Wire's `Style > Realism` is 2,618 characters about the
    /// writers' research process, which is production, not premise. A heading only qualifies when the
    /// heading itself names the work's subject.
    ///
    /// "Overview" earns its place on measurement rather than instinct: of ten sampled series that still
    /// yielded nothing after the broader extraction, FIVE led with it — Stranger Things (128,007 characters),
    /// The Flintstones, Hawaii Five-O, Batman: The Brave and the Bold, The Most Hated Man on the Internet.
    /// It is the house style for a series article that summarises rather than serialises.
    ///
    /// "Characters" is here for the sketch-show shape, where the premise IS the recurring characters — Da Ali
    /// G Show's article is `Characters > Ali G / Borat Sagdiyev / Brüno` and nothing else describes it.
    /// "Format" is the premise of an unscripted show. So You Think You Can Dance's `Show format` is 535
    /// characters of exactly that — a selection process, expert judges, then a competition phase — and the
    /// same heading carries Project Runway, MasterChef, Big Brother and Alone. Matched as a prefix so
    /// "Format and rules" qualifies.
    ///
    /// Two headings were considered and REJECTED on inspection, which is why they are listed here rather
    /// than quietly absent. "History" reads like premise and is not: The Smurfs' is 2,849 characters
    /// beginning "In 1976, Stuart R. Ross ... acquiring North American merchandising rights" — business
    /// history. And "Episodes"/"Episode list" are wikitables, which `cleanWikitext` strips, so they measure
    /// 0 characters of prose whatever the classifier says about them.
    static let themeSectionNames = ["themes", "setting", "concept", "premise and production",
                                    "characters and setting", "social commentary", "overview",
                                    "characters", "series overview", "format", "show format"]

    /// Headings about the MAKING or the RECEIVING of a work. Never prose about the work itself, and the
    /// reason a broader sweep cannot simply take everything that is not a plot heading.
    static let excludedSectionPrefixes = [
        "production", "development", "filming", "casting", "cast", "crew", "music", "soundtrack",
        "reception", "critical", "review", "awards", "accolades", "ratings", "viewership", "broadcast",
        "release", "home media", "marketing", "merchandis", "legacy", "in popular culture", "controversy",
        "distribution", "box office", "references", "external links", "see also", "further reading",
        "notes", "bibliography", "sources", "cite", "adaptations", "sequel", "prequel", "spin-off",
    ]

    /// Section titles (case-insensitive) that carry the plot, in preference order. "Premise"/"Storyline" are
    /// the headings most TV-series articles use (film articles favour "Plot"), so including them materially
    /// lifts the TV hit-rate; "Summary" is last as the loosest match.
    ///
    /// "Brief summary" is here from a film spot check (Very Happy Alexander) — unambiguously a plot heading
    /// that the `"plot "` prefix rule cannot reach.
    static let plotSectionNames = ["plot", "plot summary", "synopsis", "storyline", "premise", "story",
                                   "summary", "brief summary"]

    /// Headings an ANTHOLOGY or DOCUMENTARY uses where a narrative film says "Plot" — Fantasia 2000 and
    /// New York, I Love You head their story sections "Segments"; Jodorowsky's Dune and Baraka use "Content".
    /// Ranked below every name above, so an article carrying both still yields its real plot.
    static let plotSectionFallbackNames = ["segments", "content"]

    /// A section heading reduced to comparable text: markup removed, entities resolved, trimmed, lowercased.
    ///
    /// A `sections` response does not promise plain text — MediaWiki wraps the heading in markup whenever the
    /// page needs directionality handling, so Face/Off's plot section arrives as `<span dir="ltr">Plot</span>`
    /// and matched nothing. Comparing the raw string silently dropped those articles.
    static func strippedHeading(_ line: String) -> String {
        displayHeading(line).lowercased()
    }

    /// The same heading with its markup gone but its CASE intact — what gets recorded as provenance.
    ///
    /// `strippedHeading` lowercases because it exists to compare. A recorded section name is read by a
    /// person and matched against a later rule change, and "season 1 (2002)" is worse at both than
    /// "Season 1 (2002)". The Rifleman's heading arrives as two `<span class="anchor">` elements followed by
    /// the word, so storing the raw line is not an option either.
    static func displayHeading(_ line: String) -> String {
        var s = line.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
        for (entity, char) in [("&amp;", "&"), ("&quot;", "\""), ("&#039;", "'"), ("&apos;", "'"),
                               ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")] {
            s = s.replacingOccurrences(of: entity, with: char)
        }
        return s.trimmingCharacters(in: .whitespaces)
    }

    /// Rank of a heading in preference order, or nil when it is not a plot heading. One source of truth for
    /// the filter AND the sort: they used to normalise differently — the filter trimmed whitespace and the
    /// sort did not — so a heading with a stray space passed the filter and then sorted last, letting an
    /// article's "Summary" win over its "Plot".
    /// Ranks are spaced so the two derived classes can slot BETWEEN the exact names rather than after all of
    /// them. Appending them instead put "Plot and background" below "Summary" — reintroducing, for qualified
    /// headings, the very defect this function exists to prevent.
    static func plotRank(_ line: String) -> Int? {
        let normalized = strippedHeading(line)
        if let exact = plotSectionNames.firstIndex(of: normalized) { return exact * 10 }
        // "Plot and background", "Plot segments" — a qualified Plot heading is still the plot, and is better
        // evidence than "Synopsis" or "Summary", so it ranks just under the two exact Plot spellings.
        if normalized.hasPrefix("plot ") { return 15 }
        // Anthology/documentary headings are the weakest evidence: ranked below every real name, so an
        // article carrying both still yields its actual plot.
        if let fallback = plotSectionFallbackNames.firstIndex(of: normalized) {
            return plotSectionNames.count * 10 + fallback
        }
        return nil
    }

    /// True when a section heading is one we treat as the plot.
    static func isPlotSection(_ line: String) -> Bool {
        plotRank(line) != nil
    }

    /// What a section's prose is about.
    public enum SectionKind: Equatable {
        /// The story: a plot heading, or a serial instalment.
        case story
        /// What the work is about, rather than what happens in it.
        case theme
        /// Its making or its reception. Never included.
        case excluded
    }

    /// Classify a heading, given its enclosing heading. The leaf alone is not enough, and neither is the
    /// parent — both directions matter, and The Wire's article demonstrates each:
    ///
    ///   * `Season 1 (2002)` … `Season 5 (2008)` are nested under **`Cast and characters`**, not under
    ///     `Episodes`. 22,892 characters of plot under a parent that reads as a cast list. So an explicitly
    ///     story leaf has to OUTRANK an excluded parent.
    ///   * `Institutional dysfunction` and `Surveillance` sit under `Themes` and say nothing thematic in
    ///     their own names. So a theme parent has to be INHERITED by its children.
    ///   * `Realism` sits under `Style` and is 2,618 characters about the writers' research — production.
    ///     So an unrecognised leaf under an unrecognised parent stays excluded.
    public static func sectionKind(_ line: String, parent: String? = nil) -> SectionKind {
        let heading = strippedHeading(line)
        // 1. An explicit story heading wins outright, whatever encloses it.
        if plotRank(line) != nil { return .story }
        if isSerialHeading(heading) { return .story }
        // 2. Then an explicit exclusion on the leaf itself.
        if isExcludedHeading(heading) { return .excluded }
        // 3. Then inheritance, in both directions. A child of Themes is thematic even when its own name is
        //    just a topic ("Institutional dysfunction"), and a child of Plot is plot even when its own name
        //    is a structural label ("Act II", "Prologue") — dropping those loses the back half of any film
        //    whose plot is broken into acts.
        if let parent {
            let up = strippedHeading(parent)
            if plotRank(parent) != nil || isSerialHeading(up) { return .story }
            if isThemeHeading(up) { return .theme }
            if isExcludedHeading(up) { return .excluded }
        }
        if isThemeHeading(heading) { return .theme }
        return .excluded
    }

    /// Prefix match, so "Format and rules" counts as "format" — the same shape `isExcludedHeading` uses.
    private static func isThemeHeading(_ heading: String) -> Bool {
        themeSectionNames.contains { heading == $0 || heading.hasPrefix($0 + " ") }
    }

    private static func isExcludedHeading(_ heading: String) -> Bool {
        excludedSectionPrefixes.contains { heading == $0 || heading.hasPrefix($0) }
    }

    /// A heading naming an INSTALMENT of a serial — "Season 1 (2002)", "Series 2", "Part One", "Episodes".
    ///
    /// The serial word alone is not enough. "Episode structure" begins with one and is 1,622 characters of
    /// production prose under The Wire's `Production`; matching on the first word swept it in. So the word
    /// must either stand alone (possibly pluralised) or be followed by something that reads as an instalment
    /// number.
    static func isSerialHeading(_ heading: String) -> Bool {
        var words = heading.split(separator: " ").map(String.init)
        // A leading article is noise: The Storyteller heads its anthology "The episodes", and without this
        // its seven stories — the entire content of the article — inherit nothing and are dropped.
        if let first = words.first, ["the", "a", "an"].contains(first), words.count > 1 {
            words.removeFirst()
        }
        // Both spellings: "Seasons" de-pluralises to "season", but "Series" must NOT become "serie" —
        // stripping the s unconditionally silently dropped every British series article, 16,679 characters
        // of Misfits among them.
        guard let raw = words.first else { return false }
        let singular = raw.hasSuffix("s") ? String(raw.dropLast()) : raw
        guard serialSectionPrefixes.contains(raw) || serialSectionPrefixes.contains(singular) else {
            return false
        }
        words.removeFirst()
        guard let next = words.first else { return true }   // bare "Episodes", "Seasons"
        if next.first?.isNumber == true { return true }     // "Season 1", "Series 2 (2010)"
        let ordinals = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                        "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
                        "first", "second", "third", "fourth", "fifth"]
        return ordinals.contains(next.trimmingCharacters(in: CharacterSet(charactersIn: ":(),.")))
    }

    /// Split raw article wikitext into `(heading, level, body)`, lead first.
    ///
    /// Splitting the whole article locally replaces one request per section. The Wire needs six sections;
    /// fetching them individually is six round trips against an API that rate-limits, for an article that
    /// comes down in one.
    public static func splitSections(_ wikitext: String) -> [(heading: String, level: Int, body: String)] {
        let pattern = #"(?m)^(={2,6})\s*(.+?)\s*\1\s*$"#
        guard let re = try? NSRegularExpression(pattern: pattern) else {
            return [("", 1, wikitext)]
        }
        let whole = NSRange(wikitext.startIndex..., in: wikitext)
        let matches = re.matches(in: wikitext, range: whole)
        guard !matches.isEmpty else { return [("", 1, wikitext)] }

        var out: [(String, Int, String)] = []
        if let first = Range(matches[0].range, in: wikitext) {
            out.append(("", 1, String(wikitext[wikitext.startIndex..<first.lowerBound])))
        }
        for (n, match) in matches.enumerated() {
            guard let equalsRange = Range(match.range(at: 1), in: wikitext),
                  let titleRange = Range(match.range(at: 2), in: wikitext),
                  let full = Range(match.range, in: wikitext) else { continue }
            let end = n + 1 < matches.count
                ? Range(matches[n + 1].range, in: wikitext)?.lowerBound ?? wikitext.endIndex
                : wikitext.endIndex
            out.append((String(wikitext[titleRange]),
                        wikitext[equalsRange].count,
                        String(wikitext[full.upperBound..<end])))
        }
        return out
    }

    /// Every section of an article that describes the work, in article order, with its heading kept.
    ///
    /// Story sections first and theme sections after, so a caller that wants to trim to a budget drops the
    /// weaker evidence rather than the end of the plot.
    public static func describingProse(_ wikitext: String) -> (text: String, sections: [String]) {
        let sections = splitSections(wikitext)
        var parentByLevel: [Int: String] = [:]
        var story: [(String, String)] = []
        var theme: [(String, String)] = []
        for (heading, level, body) in sections {
            guard !heading.isEmpty else { continue }
            parentByLevel[level] = heading
            // The nearest enclosing heading ABOVE this one.
            let parent = (2..<level).reversed().compactMap { parentByLevel[$0] }.first
            let prose = cleanWikitext(body)
            guard !prose.isEmpty else { continue }
            // The CLEANED heading is what gets recorded. MediaWiki wraps a heading in markup whenever the
            // page needs anchors — The Rifleman's Overview arrives as two <span class="anchor"> elements
            // followed by the word — and storing that raw would make the provenance unreadable and
            // unmatchable against any later rule change.
            let name = displayHeading(heading)
            switch sectionKind(heading, parent: parent) {
            case .story: story.append((name, prose))
            case .theme: theme.append((name, prose))
            case .excluded: continue
            }
        }
        let kept = story + theme
        return (kept.map(\.1).joined(separator: "\n\n"), kept.map(\.0))
    }

    private func actionAPIPlot(articleTitle: String) async throws -> PlotFetch? {
        // ONE request for the whole article: wikitext, revid and resolved title together.
        //
        // This used to be two — a section list to find the Plot index, then that one section's wikitext —
        // which is why a series whose plot is spread over five `Season N` headings yielded nothing at all.
        // Fetching per section instead would be one round trip each against a rate-limited API; the whole
        // article comes down in one, and splitting it locally costs nothing.
        let data = try await get(actionAPI, [
            "action": "parse", "page": articleTitle, "prop": "wikitext|revid",
            "format": "json", "formatversion": "2", "redirects": "1",
        ])
        guard let wikitext = Self.decodeWikitext(data) else { return nil }
        let found = Self.describingProse(wikitext)
        guard !found.text.isEmpty else { return nil }
        let parsed = try? JSONDecoder().decode(WikitextResult.self, from: data)
        return PlotFetch(text: found.text, revId: parsed?.parse.revid,
                         resolvedArticle: parsed?.parse.title ?? articleTitle,
                         sections: found.sections)
    }

    /// The article revision a `prop=…|revid` response was rendered from.
    static func revId(_ data: Data) -> Int? {
        (try? JSONDecoder().decode(SectionsResult.self, from: data))?.parse.revid
    }

    /// Parse a `prop=sections` response (formatversion=2) and return the Plot section's `index` string.
    static func plotSectionIndex(_ data: Data) -> String? {
        guard let root = try? JSONDecoder().decode(SectionsResult.self, from: data) else { return nil }
        let sections = root.parse.sections
        // Preference order: an exact "Plot" beats "Synopsis"/"Story", which beat the anthology fallbacks.
        return sections
            .compactMap { section in plotRank(section.line).map { ($0, section.index) } }
            .min { $0.0 < $1.0 }?.1
    }

    /// Extract the `wikitext` string from a `prop=wikitext` response (formatversion=2).
    static func decodeWikitext(_ data: Data) -> String? {
        (try? JSONDecoder().decode(WikitextResult.self, from: data))?.parse.wikitext
    }

    private struct SectionsResult: Decodable {
        let parse: Parse
        // `title` is the page AFTER redirect resolution, which is what must be stored alongside `revid`.
        struct Parse: Decodable { let sections: [Section]; let revid: Int?; let title: String? }
        struct Section: Decodable { let line: String; let index: String }
    }
    private struct WikitextResult: Decodable {
        let parse: Parse
        /// `revid` and `title` ride on the same response as the wikitext — one request carries all three,
        /// and `title` is the page AFTER redirect resolution, which is what must be stored beside `revid`.
        struct Parse: Decodable { let wikitext: String; let revid: Int?; let title: String? }
    }

    // MARK: - Wikitext cleaning

    /// Strip wiki markup to plain prose: HTML comments, `<ref>…</ref>` (and self-closing), other tags,
    /// `{{…}}` templates (iteratively, handling nesting), `[[File:…]]`/`[[Image:…]]`, `[[a|b]]`→`b`,
    /// `[[a]]`→`a`, external links, `[1]`-style ref markers, and `'''`/`''` emphasis. Whitespace is collapsed.
    static func cleanWikitext(_ input: String) -> String {
        var s = input

        s = replace(s, #"<!--[\s\S]*?-->"#, "")                 // HTML comments
        s = replace(s, #"<ref[^>]*?/>"#, "")                    // self-closing <ref .../>
        s = replace(s, #"<ref[^>]*?>[\s\S]*?</ref>"#, "")       // <ref>…</ref> (may span lines)
        s = replace(s, #"<[^>]+>"#, "")                         // any remaining HTML tags
        s = replace(s, #"={2,}[^=\n]+={2,}"#, "")               // section headings (== Plot ==, === … ===)

        // Wiki tables {|…|} — iterate innermost-first (a match contains no nested `{|` before its `|}`), so a
        // table nested in a cell unwinds outward. Done before templates so a table's inner {{…}} go with it.
        while let range = s.range(of: #"\{\|(?:(?!\{\|)[\s\S])*?\|\}"#, options: .regularExpression) {
            s.replaceSubrange(range, with: "")
        }

        // Templates {{…}} — iterate innermost-first so nested templates fully unwind.
        while let range = s.range(of: #"\{\{[^{}]*\}\}"#, options: .regularExpression) {
            s.replaceSubrange(range, with: "")
        }

        s = replace(s, #"\[\[(?:File|Image):[^\[\]]*\]\]"#, "") // media links
        s = replace(s, #"\[\[[^\[\]|]*\|([^\[\]]*)\]\]"#, "$1") // [[a|b]] → b
        s = replace(s, #"\[\[([^\[\]]*)\]\]"#, "$1")            // [[a]]   → a
        s = replace(s, #"\[https?://[^\s\]]+\s+([^\]]*)\]"#, "$1") // [http://… text] → text
        s = replace(s, #"\[https?://[^\]]*\]"#, "")             // bare [http://…]
        s = replace(s, #"\[\d+\]"#, "")                         // [1]-style ref markers

        s = s.replacingOccurrences(of: "'''''", with: "")
        s = s.replacingOccurrences(of: "'''", with: "")
        s = s.replacingOccurrences(of: "''", with: "")

        // Decode the HTML entities wikitext commonly carries (&nbsp; especially) so they don't survive as
        // literal tokens into the composed embedding doc.
        for (entity, glyph) in htmlEntities { s = s.replacingOccurrences(of: entity, with: glyph) }

        s = replace(s, #"[ \t]+"#, " ")                         // collapse runs of spaces/tabs
        s = replace(s, #" *\n *"#, "\n")                         // trim around newlines
        s = replace(s, #"\n{2,}"#, "\n")                         // collapse blank lines
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The handful of HTML entities that actually turn up in article wikitext, decoded to their glyphs.
    /// `&amp;` is last so an already-encoded `&amp;nbsp;` doesn't get double-decoded into a stray space.
    static let htmlEntities: [(String, String)] = [
        ("&nbsp;", " "), ("&ndash;", "–"), ("&mdash;", "—"), ("&quot;", "\""),
        ("&apos;", "'"), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
    ]

    private static func replace(_ s: String, _ pattern: String, _ template: String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return s }
        let range = NSRange(s.startIndex..., in: s)
        return re.stringByReplacingMatches(in: s, range: range, withTemplate: template)
    }

    // MARK: - Wikimedia Enterprise (optional, token-gated)

    /// The pre-sectioned plot from the Wikimedia Enterprise structured-contents endpoint. Best-effort: any
    /// failure (or a missing plot section) returns nil so the caller falls back to the action API.
    private func enterpriseProse(articleTitle: String) async throws -> (text: String, sections: [String])? {
        guard let token = enterpriseToken else { return nil }
        guard let encoded = articleTitle.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "https://api.enterprise.wikimedia.com/v2/structured-contents/\(encoded)") else {
            return nil
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(#"{"filters":[{"field":"is_part_of.identifier","value":"enwiki"}],"limit":1}"#.utf8)

        return Self.enterpriseProse(try await send(request))
    }

    /// Parse an Enterprise structured-contents payload and pull EVERY section describing the work.
    ///
    /// This took the first Plot/Synopsis section and stopped, exactly as the action-API path did — and since
    /// Enterprise is tried first whenever a token is present, leaving it that way would have silently undone
    /// the broader extraction for every title. The Wire would still yield nothing on the fast path.
    ///
    /// Enterprise nests sub-sections in `has_parts`, so the enclosing heading is simply the recursion's
    /// parent — which `sectionKind` needs in both directions: The Wire's seasons hang under
    /// `Cast and characters`, and its themes are named for their topic rather than for being themes.
    static func enterpriseProse(_ data: Data) -> (text: String, sections: [String])? {
        guard let articles = try? JSONDecoder().decode([EnterpriseArticle].self, from: data) else { return nil }
        var story: [(String, String)] = []
        var theme: [(String, String)] = []

        // A section contributes only its OWN paragraphs and always recurses, so prose is attributed to the
        // heading it actually sits under. Taking a qualifying parent wholesale instead would credit The
        // Wire's themes to "Themes" rather than to "Institutional dysfunction", and would disagree with the
        // wikitext path, which classifies every heading independently.
        func walk(_ section: EnterpriseSection, parent: String?) {
            let name = section.name ?? ""
            if !name.isEmpty {
                let own = ownParagraphs(of: section)
                if !own.isEmpty {
                    let clean = displayHeading(name)
                    switch sectionKind(name, parent: parent) {
                    case .story: story.append((clean, own))
                    case .theme: theme.append((clean, own))
                    case .excluded: break
                    }
                }
            }
            for child in section.hasParts ?? [] { walk(child, parent: name.isEmpty ? parent : name) }
        }

        for article in articles {
            for section in article.sections ?? [] { walk(section, parent: nil) }
        }
        let kept = story + theme
        guard !kept.isEmpty else { return nil }
        return (kept.map(\.1).joined(separator: "\n\n"), kept.map(\.0))
    }

    /// A section's OWN prose: its `value` plus its direct paragraph parts, never a nested section's.
    ///
    /// The recursion belongs to the caller, which needs each heading's text kept separately so it can be
    /// classified and attributed. Flattening the subtree here instead credited a whole `Themes` branch to
    /// "Themes" and hid whatever was excluded inside it.
    private static func ownParagraphs(of section: EnterpriseSection) -> String {
        var parts: [String] = []
        if let value = section.value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
            parts.append(value)
        }
        for child in section.hasParts ?? [] where (child.name ?? "").isEmpty {
            if let value = child.value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
                parts.append(value)
            }
        }
        return parts.joined(separator: "\n")
    }

    /// Recursively gather prose under a section: a paragraph part carries its text in `value`; a nested
    /// section carries empty `value` + its own `has_parts`. Joining both handles flat and sub-sectioned plots.
    private static func paragraphProse(of section: EnterpriseSection) -> String {
        var parts: [String] = []
        if let value = section.value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
            parts.append(value)
        }
        for child in section.hasParts ?? [] {
            let prose = paragraphProse(of: child)
            if !prose.isEmpty { parts.append(prose) }
        }
        return parts.joined(separator: "\n")
    }

    private struct EnterpriseArticle: Decodable {
        let sections: [EnterpriseSection]?
    }
    private struct EnterpriseSection: Decodable {
        let name: String?
        let value: String?
        let hasParts: [EnterpriseSection]?
        enum CodingKeys: String, CodingKey { case name, value; case hasParts = "has_parts" }
    }

    // MARK: - Transport

    private func get(_ base: URL, _ query: [String: String]) async throws -> Data {
        // Two `action=parse` calls per title, ~80k per full pass, nearly all re-reading unchanged articles.
        // Cached on the full query, so the section list and the section's wikitext occupy separate entries.
        let cacheKey = cache?.key(path: base.path, query: query)
        if let cacheKey, let hit = cache?.read(cacheKey) { return hit }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        // `URLQueryItem` leaves "+" alone, and a receiving server reads it as a SPACE — so every article whose
        // title contains one ("Knife+Heart", "X+Y", "Survive Style 5+") resolved to a title with a space and
        // came back `missingtitle`. Percent-encoding it after the fact is the narrowest fix: the character is
        // legal in a query, it is only its form-decoding that is wrong.
        components.percentEncodedQuery = components.percentEncodedQuery?
            .replacingOccurrences(of: "+", with: "%2B")
        var request = URLRequest(url: components.url!)
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let data = try await send(request)
        // Only a real answer is worth keeping. The action API reports a missing page or a bad parameter as a
        // 200 carrying `{"error":{…}}`, so caching on status alone would pin "missingtitle" for the whole TTL
        // and make a transient outage look like a permanently plotless title.
        if let cacheKey, Self.isCacheableBody(data) { cache?.write(cacheKey, data) }
        return data
    }

    /// True when an action-API body is a successful `parse` result rather than an error envelope.
    static func isCacheableBody(_ data: Data) -> Bool {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return object["error"] == nil && object["parse"] != nil
    }

    /// One HTTP round-trip with transient-failure retry (429/5xx/timeout) + a non-2xx → `WikipediaError.http`.
    /// A definitive status (404 on a stale sitelink, 400) throws through so the caller records a plain miss.
    private func send(_ request: URLRequest) async throws -> Data {
        try await Transport.retrying {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                throw WikipediaError.http(http.statusCode)
            }
            return data
        }
    }
}

public enum WikipediaError: Error, Sendable {
    case http(Int)
}


/// A 200 that is not a SPARQL result — WDQS maintenance HTML, a proxy error page. Distinguished from "no
/// bindings" because the two mean opposite things to the enrich checkpoint.
public enum WikidataError: Error, CustomStringConvertible {
    case unparseableResponse(String)

    public var description: String {
        switch self {
        case .unparseableResponse(let head):
            return "Wikidata returned a 200 that is not a SPARQL result (starts: \(head))"
        }
    }
}

extension WikipediaSource {
    /// Display and search strings per title: the enwiki ARTICLE title, `rdfs:label`, `P1476` and every English
    /// alias. Search needs all of them — atlas's title index carries only TMDB's ORIGINAL title today, so
    /// "parasite" and "spirited away" miss entirely while "Gisaengchung" and "Sen to Chihiro" hit.
    ///
    /// Aliases are fetched separately because `skos:altLabel` is multi-valued and multiplies every other row.
    public struct TitleStrings: Sendable {
        public var article: String?
        public var label: String?
        public var original: String?
        public var aliases: [String] = []
    }

    public func titles(forTMDBIds ids: [Int], mediaType: MediaType) async throws -> [Int: TitleStrings] {
        let unique = Array(Set(ids)).sorted()
        guard !unique.isEmpty else { return [:] }
        let property = mediaType == .tv ? "P4983" : "P4947"
        let values = unique.map { "\"\($0)\"" }.joined(separator: " ")

        func run(_ select: String, _ body: String) async throws -> [SPARQLResult.Binding] {
            let query = """
            SELECT ?tmdb \(select) WHERE {
              VALUES ?tmdb { \(values) }
              ?film wdt:\(property) ?tmdb .
              \(body)
            }
            """
            var c = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
            c.queryItems = [URLQueryItem(name: "format", value: "json")]
            var request = URLRequest(url: c.url!)
            request.httpMethod = "POST"
            request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
            request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
            request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
            request.httpBody = Data(query.utf8)
            let data = try await send(request)
            guard let root = try? JSONDecoder().decode(SPARQLResult.self, from: data) else {
                throw WikidataError.unparseableResponse(String(decoding: data.prefix(200), as: UTF8.self))
            }
            return root.results.bindings
        }

        var out: [Int: TitleStrings] = [:]
        // "en,mul": Wikidata moved proper names to the `mul` language code, and a title is a proper name.
        for b in try await run("?article ?label ?orig", """
              OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }
              OPTIONAL { ?film wdt:P1476 ?orig . }
              OPTIONAL { ?film rdfs:label ?label . FILTER(LANG(?label) IN ('en','mul')) }
            """) {
            guard let raw = b.tmdb?.value, let id = Int(raw) else { continue }
            var t = out[id] ?? TitleStrings()
            if t.article == nil, let a = b.article?.value { t.article = Self.articleTitle(fromURL: a) }
            if t.label == nil { t.label = b.label?.value }
            if t.original == nil { t.original = b.orig?.value }
            out[id] = t
        }
        for b in try await run("?alias", """
              ?film skos:altLabel ?alias . FILTER(LANG(?alias) IN ('en','mul'))
            """) {
            guard let raw = b.tmdb?.value, let id = Int(raw), let a = b.alias?.value else { continue }
            var t = out[id] ?? TitleStrings()
            if !t.aliases.contains(a) { t.aliases.append(a) }
            out[id] = t
        }
        return out
    }

    /// Q-id → name, English aliases, and TMDB person id (P4985). The aliases are what let a search answer
    /// "tom hanks" from a record that stores only a Q-id, and P4985 lets a client open the person's page
    /// without a name lookup.
    public struct EntityInfo: Sendable {
        public var name: String?
        public var aliases: [String] = []
        public var tmdbPersonId: String?
    }

    public func entityDetails(_ qids: [String], batch: Int = 200) async throws -> [String: EntityInfo] {
        var out: [String: EntityInfo] = [:]
        for start in stride(from: 0, to: qids.count, by: batch) {
            let slice = Array(qids[start..<min(start + batch, qids.count)])
            let values = slice.map { "wd:\($0)" }.joined(separator: " ")
            func run(_ select: String, _ body: String) async throws -> [SPARQLResult.Binding] {
                let query = "SELECT ?item \(select) WHERE { VALUES ?item { \(values) } \(body) }"
                var c = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
                c.queryItems = [URLQueryItem(name: "format", value: "json")]
                var r = URLRequest(url: c.url!)
                r.httpMethod = "POST"
                r.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
                r.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
                r.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
                r.httpBody = Data(query.utf8)
                let data = try await send(r)
                guard let root = try? JSONDecoder().decode(SPARQLResult.self, from: data) else {
                    throw WikidataError.unparseableResponse("entityDetails at \(start)")
                }
                return root.results.bindings
            }
            for b in try await run("?itemLabel ?pid", """
                  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
                  OPTIONAL { ?item wdt:P4985 ?pid . }
                """) {
                guard let uri = b.item?.value else { continue }
                let qid = String(uri.split(separator: "/").last ?? "")
                var e = out[qid] ?? EntityInfo()
                if let n = b.itemLabel?.value, n != qid { e.name = n }
                if e.tmdbPersonId == nil { e.tmdbPersonId = b.pid?.value }
                out[qid] = e
            }
            for b in try await run("?alias", """
                  ?item skos:altLabel ?alias . FILTER(LANG(?alias) IN ('en','mul'))
                """) {
                guard let uri = b.item?.value, let a = b.alias?.value else { continue }
                let qid = String(uri.split(separator: "/").last ?? "")
                var e = out[qid] ?? EntityInfo()
                if !e.aliases.contains(a) { e.aliases.append(a) }
                out[qid] = e
            }
        }
        return out
    }

    /// `Q-id → its P31 (instance of) English labels`. What a thing *is*.
    ///
    /// Used on the targets of P144 (based on): the facts sidecar records that a film adapts Q1234, which links
    /// adaptations of one source to each other but cannot answer "show me films based on books" — nothing says
    /// whether Q1234 is a novel, a manga or a video game.
    public func instanceOf(_ qids: [String], batch: Int = 200) async throws -> [String: [String]] {
        var out: [String: [String]] = [:]
        for start in stride(from: 0, to: qids.count, by: batch) {
            let slice = Array(qids[start..<min(start + batch, qids.count)])
            let values = slice.map { "wd:\($0)" }.joined(separator: " ")
            let query = """
            SELECT ?item ?typeLabel WHERE {
              VALUES ?item { \(values) }
              ?item wdt:P31 ?type .
              SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
            }
            """
            var c = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
            c.queryItems = [URLQueryItem(name: "format", value: "json")]
            var r = URLRequest(url: c.url!)
            r.httpMethod = "POST"
            r.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
            r.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
            r.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
            r.httpBody = Data(query.utf8)
            let data = try await send(r)
            guard let root = try? JSONDecoder().decode(SPARQLResult.self, from: data) else {
                throw WikidataError.unparseableResponse("instanceOf at \(start)")
            }
            for b in root.results.bindings {
                guard let uri = b.item?.value, let label = b.typeLabel?.value else { continue }
                let qid = String(uri.split(separator: "/").last ?? "")
                // A label service miss returns the bare Q-id; that is not a type name.
                guard label != qid else { continue }
                out[qid, default: []].append(label)
            }
        }
        return out
    }
}
