import Foundation

/// Live movie/TV enrichment from **Wikipedia** (FP-2) — the fresh, ToS-clean plot source that replaces
/// shipping TMDB overviews. NEVER a dump: every call hits the live public APIs. Two hops:
///
///  1. `wikidata(forTMDBIds:mediaType:)` — ONE Wikidata SPARQL POST maps a batch of TMDB ids to their
///     Wikidata film/series entity, the linked English Wikipedia article title, and (optionally) the IMDb id.
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
    /// The mapping returned per TMDB id: the enwiki article title (for the plot hop) and the IMDb id.
    public struct Mapping: Sendable, Equatable {
        public let article: String?
        public let imdb: String?
        public init(article: String?, imdb: String?) { self.article = article; self.imdb = imdb }
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
        let query = """
        SELECT ?tmdb ?article ?imdb WHERE {
          VALUES ?tmdb { \(values) }
          ?film wdt:\(property) ?tmdb .
          OPTIONAL { ?film wdt:P345 ?imdb . }
          OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }
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

        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw WikipediaError.http(http.statusCode)
        }
        return Self.parseWikidata(data)
    }

    /// Decode a SPARQL JSON result into `tmdbId → Mapping`. Pure + testable (fixture JSON → mapping).
    static func parseWikidata(_ data: Data) -> [Int: Mapping] {
        guard let root = try? JSONDecoder().decode(SPARQLResult.self, from: data) else { return [:] }
        var map: [Int: Mapping] = [:]
        for binding in root.results.bindings {
            guard let tmdbRaw = binding.tmdb?.value, let tmdbId = Int(tmdbRaw) else { continue }
            let article = (binding.article?.value).flatMap { Self.articleTitle(fromURL: $0) }
            let imdb = binding.imdb?.value
            // A film can bind more than once (e.g. two IMDb ids); keep the first article/imdb seen but fill
            // in any field a later row supplies.
            let existing = map[tmdbId]
            map[tmdbId] = Mapping(article: existing?.article ?? article, imdb: existing?.imdb ?? imdb)
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

    /// Section titles (case-insensitive) that carry the plot, in preference order.
    static let plotSectionNames = ["plot", "plot summary", "synopsis", "story"]

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

        s = replace(s, #"[ \t]+"#, " ")                         // collapse runs of spaces/tabs
        s = replace(s, #" *\n *"#, "\n")                         // trim around newlines
        s = replace(s, #"\n{2,}"#, "\n")                         // collapse blank lines
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

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

        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw WikipediaError.http(http.statusCode)
        }
        return Self.enterprisePlot(data)
    }

    /// Parse an Enterprise structured-contents payload (an array of articles, each with `sections`) and pull
    /// the first Plot/Synopsis section's text. Pure + testable.
    static func enterprisePlot(_ data: Data) -> String? {
        guard let articles = try? JSONDecoder().decode([EnterpriseArticle].self, from: data) else { return nil }
        for article in articles {
            for section in article.sections ?? [] where isPlotSection(section.name ?? "") {
                let text = (section.value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty { return text }
            }
        }
        return nil
    }

    private struct EnterpriseArticle: Decodable {
        let sections: [EnterpriseSection]?
        struct EnterpriseSection: Decodable { let name: String?; let value: String? }
    }

    // MARK: - Transport

    private func get(_ base: URL, _ query: [String: String]) async throws -> Data {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw WikipediaError.http(http.statusCode)
        }
        return data
    }
}

public enum WikipediaError: Error, Sendable {
    case http(Int)
}
