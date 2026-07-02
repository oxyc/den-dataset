import Foundation

// The producer's thin TMDB client + the minimal supporting types the backfill tool references. This is a
// deliberate ~120-LOC reimplementation, NOT a copy of DenKit's 417-LOC TMDBWire: the tool needs only two
// endpoints — `/discover` (worklist) and detail+keywords (enrich) — so the surface stays small.

/// A TMDB id (movie/tv/person). `rawValue` is the integer the REST paths use.
public struct TMDBID: Hashable, Codable, Sendable, RawRepresentable {
    public let rawValue: Int
    public init(rawValue: Int) { self.rawValue = rawValue }
    public init(_ value: Int) { self.rawValue = value }
    public init(from decoder: any Decoder) throws {
        rawValue = try decoder.singleValueContainer().decode(Int.self)
    }
    public func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(rawValue)
    }
}

/// A `(tmdbId, mediaType)` pair — TMDB ids are type-ambiguous, so identity is the pair.
public struct MediaIdentifier: Hashable, Codable, Sendable {
    public let id: TMDBID
    public let mediaType: MediaType
    public init(id: TMDBID, mediaType: MediaType) {
        self.id = id
        self.mediaType = mediaType
    }
    public init(_ id: Int, _ mediaType: MediaType) {
        self.init(id: TMDBID(id), mediaType: mediaType)
    }
}

/// One TMDB keyword (grounding signal).
public struct Keyword: Hashable, Codable, Sendable {
    public let id: Int
    public let name: String
    public init(id: Int, name: String) { self.id = id; self.name = name }
}

/// A discovery list row — the worklist phase reads only `tmdbID.rawValue` (+ `year` for diagnostics).
public struct MediaItem: Hashable, Sendable {
    public let identifier: MediaIdentifier
    public let year: Int?
    public var tmdbID: TMDBID { identifier.id }
    public init(identifier: MediaIdentifier, year: Int?) {
        self.identifier = identifier
        self.year = year
    }
}

/// One page of TMDB results.
public struct Page<Element: Sendable>: Sendable {
    public let items: [Element]
    public let page: Int
    public let totalPages: Int
    public init(items: [Element], page: Int, totalPages: Int) {
        self.items = items; self.page = page; self.totalPages = totalPages
    }
}

public enum TMDBError: Error, Sendable {
    case missingAPIKey
    case http(Int)
    case transport(String)
    case decoding(String)
}

/// A cooperative counting semaphore — bounds in-flight requests without blocking a cooperative thread (a
/// `DispatchSemaphore.wait()` inside `async` stalls the executor and is a hard error under Swift 6). Waiters
/// suspend on a continuation and resume FIFO as permits free up.
actor AsyncSemaphore {
    private var permits: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []
    init(_ value: Int) { permits = max(1, value) }

    func acquire() async {
        if permits > 0 { permits -= 1; return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func release() {
        if let next = waiters.first { waiters.removeFirst(); next.resume() }
        else { permits += 1 }
    }
}

/// A minimal throttled TMDB client over `URLSession`. `maxConcurrent` bounds in-flight requests via a
/// counting semaphore (the tool fans out enrich calls in a task group). Base https://api.themoviedb.org/3,
/// authenticated with the `api_key` query param.
public final class TMDBClient: Sendable {
    private let apiKey: String
    private let baseURL: URL
    private let session: URLSession
    private let gate: AsyncSemaphore

    public init(apiKey: String,
                baseURL: URL = URL(string: "https://api.themoviedb.org/3")!,
                maxConcurrent: Int = 8,
                session: URLSession = .shared) {
        self.apiKey = apiKey
        self.baseURL = baseURL
        self.session = session
        self.gate = AsyncSemaphore(maxConcurrent)
    }

    /// `/discover/{movie,tv}` from a typed `DiscoverQuery` (DT-A).
    public func discover(_ query: DiscoverQuery, page: Int = 1) async throws -> Page<MediaItem> {
        var params = query.parameters()
        params["page"] = String(page)
        let data = try await get("/discover/\(query.mediaType.pathSegment)", params)
        let paged = try Self.decoder.decode(PagedList.self, from: data)
        let items = paged.results.map { row -> MediaItem in
            let dateString = row.releaseDate ?? row.firstAirDate
            let year = dateString.flatMap { Int($0.prefix(4)) }
            return MediaItem(identifier: MediaIdentifier(row.id, query.mediaType), year: year)
        }
        return Page(items: items, page: paged.page, totalPages: paged.totalPages)
    }

    /// Single-request enrichment (DT-C) — detail + keywords + credits in ONE call via
    /// `append_to_response=keywords,credits`. Credits feed the composed embedding doc (director + top cast).
    public func classificationRecord(_ identifier: MediaIdentifier) async throws -> EnrichedTitle {
        let data = try await get("/\(identifier.mediaType.pathSegment)/\(identifier.id.rawValue)",
                                 ["append_to_response": "keywords,credits"])
        let wire = try Self.decoder.decode(ClassificationWire.self, from: data)
        return wire.toEnrichedTitle(id: identifier.id.rawValue, mediaType: identifier.mediaType)
    }

    // MARK: - Transport

    private func get(_ path: String, _ query: [String: String]) async throws -> Data {
        guard !apiKey.isEmpty else { throw TMDBError.missingAPIKey }
        guard var components = URLComponents(url: baseURL.appendingPathComponent(path),
                                             resolvingAgainstBaseURL: false) else {
            throw TMDBError.transport("invalid base URL for \(path)")
        }
        var items = [URLQueryItem(name: "api_key", value: apiKey)]
        items += query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        components.queryItems = items
        guard let url = components.url else { throw TMDBError.transport("could not build request URL for \(path)") }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        await gate.acquire()
        defer { Task { await gate.release() } }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw TMDBError.http(http.statusCode)
        }
        return data
    }

    /// snake_case JSON → the camelCase wire structs below.
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: - Wire

    private struct PagedList: Decodable {
        let page: Int
        let totalPages: Int
        let results: [ListRow]
        enum CodingKeys: String, CodingKey { case page, totalPages, results }
        init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            page = (try? c.decode(Int.self, forKey: .page)) ?? 1
            totalPages = (try? c.decode(Int.self, forKey: .totalPages)) ?? 1
            results = (try? c.decode([ListRow].self, forKey: .results)) ?? []
        }
    }

    private struct ListRow: Decodable {
        let id: Int
        let releaseDate: String?   // movie
        let firstAirDate: String?  // tv
    }

    /// Detail + `append_to_response=keywords` in one payload. Movies key the title/date one way, TV another;
    /// this decodes both and normalizes into `EnrichedTitle`.
    private struct ClassificationWire: Decodable {
        let title: String?            // movie
        let name: String?             // tv
        let overview: String?
        let releaseDate: String?      // movie
        let firstAirDate: String?     // tv
        let voteCount: Int?
        let originalLanguage: String?
        let genres: [GenreDTO]?
        let originCountry: [String]?              // tv
        let productionCountries: [Country]?       // movie
        let keywords: KeywordsBlock?
        let credits: CreditsBlock?

        struct GenreDTO: Decodable { let id: Int; let name: String }
        struct Country: Decodable { let iso31661: String }
        struct KeywordDTO: Decodable { let id: Int; let name: String }
        struct KeywordsBlock: Decodable { let keywords: [KeywordDTO]?; let results: [KeywordDTO]? }
        // Credits — `cast` is billing-ordered; `crew` carries job titles (we want job == "Director").
        struct CreditsBlock: Decodable {
            let cast: [Person]?
            let crew: [CrewMember]?
            struct Person: Decodable { let name: String; let order: Int? }
            struct CrewMember: Decodable { let name: String; let job: String? }
        }

        func toEnrichedTitle(id: Int, mediaType: MediaType) -> EnrichedTitle {
            let dateString = releaseDate ?? firstAirDate
            let year = dateString.flatMap { Int($0.prefix(4)) }
            let kw = (keywords?.keywords ?? keywords?.results ?? []).map { Keyword(id: $0.id, name: $0.name) }
            let countries = originCountry ?? productionCountries?.map(\.iso31661) ?? []
            // Director = first crew member with job "Director"; top cast = the first ~4 billed names.
            let director = credits?.crew?.first { $0.job == "Director" }?.name
            let billed = (credits?.cast ?? []).sorted { ($0.order ?? .max) < ($1.order ?? .max) }
            let topCast = Array(billed.prefix(4).map(\.name))
            return EnrichedTitle(
                tmdbId: id, mediaType: mediaType, title: title ?? name ?? "", year: year,
                overview: overview ?? "", genreIDs: genres?.map(\.id) ?? [],
                genreNames: genres?.map(\.name) ?? [], keywords: kw, originCountry: countries,
                originalLanguage: originalLanguage, voteCount: voteCount ?? 0,
                director: director, topCast: topCast)
        }
    }
}
