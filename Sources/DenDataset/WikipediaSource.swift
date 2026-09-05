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
        public let imdb: String?
        /// Minutes (P2047). Measured coverage: ~93% of films, ~37% of series — and a series' value is
        /// per-episode, so treat it as a film fact and let TV fall back elsewhere.
        public let runtimeMinutes: Int?
        /// Showrunners (P170), sorted for stability. Measured coverage: ~37% of a RANDOM series sample —
        /// famous shows are near-complete, the long tail is not. So this complements TMDB's `created_by`
        /// rather than replacing it: free where present, TMDB fills the rest.
        public let creators: [String]

        public init(article: String?, imdb: String?, runtimeMinutes: Int? = nil, creators: [String] = []) {
            self.article = article
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

    public init(session: URLSession = .shared,
                sparqlEndpoint: URL = URL(string: "https://query.wikidata.org/sparql")!,
                actionAPI: URL = URL(string: "https://en.wikipedia.org/w/api.php")!,
                enterpriseToken: String? = ProcessInfo.processInfo.environment["WIKIMEDIA_ENTERPRISE_TOKEN"]) {
        self.session = session
        self.sparqlEndpoint = sparqlEndpoint
        self.actionAPI = actionAPI
        self.enterpriseToken = (enterpriseToken?.isEmpty == false) ? enterpriseToken : nil
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
        SELECT ?tmdb ?article ?imdb ?runtime ?creatorLabel WHERE {
          VALUES ?tmdb { \(values) }
          ?film wdt:\(property) ?tmdb .
          OPTIONAL { ?film wdt:P345 ?imdb . }
          OPTIONAL { ?film wdt:P2047 ?runtime . }
          OPTIONAL { ?film wdt:P170 ?creator . }
          OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
        }
        ORDER BY ?tmdb ?article
        """

        var components = URLComponents(url: sparqlEndpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "format", value: "json")]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/sparql-query", forHTTPHeaderField: "Content-Type")
        request.setValue("application/sparql-results+json", forHTTPHeaderField: "Accept")
        request.httpBody = Data(query.utf8)

        return try Self.parseWikidata(try await send(request))
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
                creators: creators.sorted())
        }
        return map
    }

    /// `https://en.wikipedia.org/wiki/Inception` → `Inception`; underscores → spaces, percent-decoded.
    static func articleTitle(fromURL urlString: String) -> String? {
        guard let marker = urlString.range(of: "/wiki/") else { return nil }
        let raw = String(urlString[marker.upperBound...])
        let decoded = raw.removingPercentEncoding ?? raw
        let title = decoded.replacingOccurrences(of: "_", with: " ")
        return title.isEmpty ? nil : title
    }

    private struct SPARQLResult: Decodable {
        let results: Results
        struct Results: Decodable { let bindings: [Binding] }
        struct Binding: Decodable {
            let tmdb: Cell?
            let article: Cell?
            let imdb: Cell?
            let runtime: Cell?
            let creatorLabel: Cell?
        }
        struct Cell: Decodable { let value: String }
    }

    // MARK: - Plot

    /// The article's Plot/Synopsis section as plain prose, or nil if the article has no such section.
    public func plot(articleTitle: String) async throws -> String? {
        if enterpriseToken != nil, let plot = try? await enterprisePlot(articleTitle: articleTitle) {
            return plot
        }
        return try await actionAPIPlot(articleTitle: articleTitle)
    }

    /// Section titles (case-insensitive) that carry the plot, in preference order. "Premise"/"Storyline" are
    /// the headings most TV-series articles use (film articles favour "Plot"), so including them materially
    /// lifts the TV hit-rate; "Summary" is last as the loosest match.
    static let plotSectionNames = ["plot", "plot summary", "synopsis", "storyline", "premise", "story", "summary"]

    /// True when a section heading is one we treat as the plot.
    static func isPlotSection(_ line: String) -> Bool {
        let normalized = line.trimmingCharacters(in: .whitespaces).lowercased()
        return plotSectionNames.contains(normalized)
    }

    private func actionAPIPlot(articleTitle: String) async throws -> String? {
        // 1. Section list → find the Plot section's index.
        let sectionsData = try await get(actionAPI, [
            "action": "parse", "page": articleTitle, "prop": "sections",
            "format": "json", "formatversion": "2", "redirects": "1",
        ])
        guard let index = Self.plotSectionIndex(sectionsData) else { return nil }

        // 2. That section's wikitext → strip to prose.
        let wikitextData = try await get(actionAPI, [
            "action": "parse", "page": articleTitle, "section": index, "prop": "wikitext",
            "format": "json", "formatversion": "2", "redirects": "1",
        ])
        guard let wikitext = Self.decodeWikitext(wikitextData) else { return nil }
        let prose = Self.cleanWikitext(wikitext)
        return prose.isEmpty ? nil : prose
    }

    /// Parse a `prop=sections` response (formatversion=2) and return the Plot section's `index` string.
    static func plotSectionIndex(_ data: Data) -> String? {
        guard let root = try? JSONDecoder().decode(SectionsResult.self, from: data) else { return nil }
        let sections = root.parse.sections
        // Preference order: an exact "Plot" beats "Synopsis"/"Story"; ranks by position in plotSectionNames.
        let ranked = sections
            .filter { isPlotSection($0.line) }
            .sorted { a, b in
                let ra = plotSectionNames.firstIndex(of: a.line.lowercased()) ?? .max
                let rb = plotSectionNames.firstIndex(of: b.line.lowercased()) ?? .max
                return ra < rb
            }
        return ranked.first?.index
    }

    /// Extract the `wikitext` string from a `prop=wikitext` response (formatversion=2).
    static func decodeWikitext(_ data: Data) -> String? {
        (try? JSONDecoder().decode(WikitextResult.self, from: data))?.parse.wikitext
    }

    private struct SectionsResult: Decodable {
        let parse: Parse
        struct Parse: Decodable { let sections: [Section] }
        struct Section: Decodable { let line: String; let index: String }
    }
    private struct WikitextResult: Decodable {
        let parse: Parse
        struct Parse: Decodable { let wikitext: String }
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
    private func enterprisePlot(articleTitle: String) async throws -> String? {
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

        return Self.enterprisePlot(try await send(request))
    }

    /// Parse an Enterprise structured-contents payload (an array of articles, each with `sections`) and pull
    /// the first Plot/Synopsis section's prose. The plot text lives in the section's `has_parts` paragraphs
    /// (each `{type:"paragraph", value:"…"}`), not the section's own `value`; sub-sections nest further, so we
    /// flatten recursively. Pure + testable.
    static func enterprisePlot(_ data: Data) -> String? {
        guard let articles = try? JSONDecoder().decode([EnterpriseArticle].self, from: data) else { return nil }
        for article in articles {
            for section in article.sections ?? [] where isPlotSection(section.name ?? "") {
                let text = paragraphProse(of: section)
                if !text.isEmpty { return text }
            }
        }
        return nil
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
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return try await send(request)
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
