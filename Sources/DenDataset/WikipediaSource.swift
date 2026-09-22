import Foundation

/// Wikidata, for the facts sidecar — what is left here once the enrichment's mapping and plot hops moved to
/// `lib/wikidata.py` and `lib/plot.py`.
public struct WikipediaSource: Sendable {
    /// A polite, identifying User-Agent is REQUIRED by the Wikimedia APIs (unidentified traffic is throttled).
    public static let userAgent = "den-dataset/1.0 (github.com/oxyc/den-dataset)"

    private let session: URLSession
    private let sparqlEndpoint: URL
    /// Fact responses served from disk when present — see `WikiCachePolicy`. nil disables.
    private let cache: ResponseCache?

    public init(session: URLSession = .shared,
                sparqlEndpoint: URL = URL(string: "https://query.wikidata.org/sparql")!,
                cache: ResponseCache? = WikiCachePolicy.cache()) {
        self.session = session
        self.sparqlEndpoint = sparqlEndpoint
        self.cache = cache
    }

    // MARK: - Genre labels

    /// `"science fiction film"` → `"science fiction"`. Wikidata appends the medium to genre labels where TMDB
    /// does not, and comparing the raw strings makes a vocabulary that largely DOES line up look like it
    /// shares nothing: mean overlap with TMDB measured 0.01 before this strip and 0.40 after.
    public static func strippedGenre(_ label: String) -> String {
        // Longest first, so "television series" is taken whole before "series" can bite into it.
        // `television program`, `television` and `anime and manga` were missing, which left
        // `reality television`, `crime fiction` and their kin unmatched against a TMDB vocabulary that
        // does contain them: 38 genre Q-ids across 1,014 references, unmapped for want of a suffix.
        let media = ["television program", "television series", "anime and manga", "tv series",
                     "television", "series", "movie", "anime", "film"]
        var s = label.lowercased().trimmingCharacters(in: .whitespaces)
        var changed = true
        while changed {
            changed = false
            for word in media where s.hasSuffix(" " + word) {
                s.removeLast(word.count + 1)
                s = s.trimmingCharacters(in: .whitespaces)
                changed = true
            }
            // `fiction` only when something survives it AND the whole phrase is not itself a genre —
            // otherwise "science fiction" becomes "science", which is not a TMDB genre and not a thing.
            // hasSuffix, not equality: "hard science fiction" and "military science fiction" are
            // real referenced genres, and an equality guard strips them to "hard science" and
            // "military science". The stripped label is written back into the shipped entity map
            // and into the composed document the embedding reads, so a mangled one travels.
            if !changed, s.hasSuffix(" fiction"), !s.hasSuffix("science fiction"),
               s.count > " fiction".count + 1 {
                s.removeLast(" fiction".count)
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
        // Cached on the query text, exactly like the mapping query above and for the same reason: WDQS is the
        // flakiest thing the pipeline touches, and a fact scrape is re-run far more often than it succeeds
        // outright. Without this, every retry re-asked for the ids that had already answered — so a scrape
        // that failed on its last property paid for all of them again, and a batch boundary that moved
        // invalidated nothing but still refetched everything.
        //
        // The key covers the whole query, so it includes the id batch AND the property. Re-batching the same
        // ids differently therefore misses, which is correct rather than merely safe: a different VALUES set
        // is a different question, and answering it from a cache keyed on something coarser would silently
        // return facts for ids the caller did not ask about.
        let cacheKey = cache?.key(path: "sparql-facts", query: ["q": query])
        if let cacheKey, let hit = cache?.read(cacheKey) {
            // Same fall-through as the mapping query: a stored body that no longer decodes must not be able
            // to fail a run, it just costs one live fetch.
            if let parsed = try? Self.parseFacts(hit, spec: spec) { return parsed }
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
        let parsed = try Self.parseFacts(data, spec: spec)
        // Written only after parsing succeeded — a WDQS maintenance page is HTML, decodes as nothing, and
        // storing it would make the outage outlive itself.
        if let cacheKey { cache?.write(cacheKey, data) }
        return parsed
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

    // MARK: - Transport

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
