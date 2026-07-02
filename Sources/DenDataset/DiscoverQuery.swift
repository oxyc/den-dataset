import Foundation

/// A typed TMDB `/discover` query (DT-A). Serializes into the `[String: String]` param map the thin client
/// passes to `get(_:_:)`. Copied from DenKit's `DiscoverQuery` (the producer's worklist phase is the only
/// caller here, so the app-facing `retargeted(to:)` helper is dropped).
///
/// TMDB join rules (DT-recipe-catalog.md): within ONE param, comma = AND, pipe `|` = OR — they can't be
/// mixed in a single param, so the join is per-field. `without_genres` is always AND (TMDB exclusion).
public struct DiscoverQuery: Sendable, Equatable {
    public enum Join: String, Sendable { case and = ",", or = "|" }

    public var mediaType: MediaType
    public var genres: [Int]
    public var genreJoin: Join
    public var keywords: [Int]
    public var keywordJoin: Join
    public var withoutGenres: [Int]
    public var originalLanguage: String?
    public var originCountry: [String]
    public var originCountryJoin: Join
    public var voteCountGte: Int?
    /// Release-date window (`YYYY-MM-DD`). Maps to `primary_release_date.{gte,lte}` for movies and
    /// `first_air_date.{gte,lte}` for TV — lets a caller partition the universe by year (the backfill
    /// worklist uses this to page past TMDB's 10k-result-per-query ceiling).
    public var releaseDateGte: String?
    public var releaseDateLte: String?
    public var sortBy: String
    public var includeAdult: Bool

    public init(
        mediaType: MediaType = .movie,
        genres: [Int] = [],
        genreJoin: Join = .and,
        keywords: [Int] = [],
        keywordJoin: Join = .or,
        withoutGenres: [Int] = [],
        originalLanguage: String? = nil,
        originCountry: [String] = [],
        originCountryJoin: Join = .or,
        voteCountGte: Int? = nil,
        releaseDateGte: String? = nil,
        releaseDateLte: String? = nil,
        sortBy: String = "popularity.desc",
        includeAdult: Bool = false
    ) {
        self.mediaType = mediaType
        self.genres = genres
        self.genreJoin = genreJoin
        self.keywords = keywords
        self.keywordJoin = keywordJoin
        self.withoutGenres = withoutGenres
        self.originalLanguage = originalLanguage
        self.originCountry = originCountry
        self.originCountryJoin = originCountryJoin
        self.voteCountGte = voteCountGte
        self.releaseDateGte = releaseDateGte
        self.releaseDateLte = releaseDateLte
        self.sortBy = sortBy
        self.includeAdult = includeAdult
    }

    /// The TMDB `/discover/{movie,tv}` params — excludes `api_key` + `page` (the client adds those).
    public func parameters() -> [String: String] {
        var params: [String: String] = [
            "sort_by": sortBy,
            "include_adult": includeAdult ? "true" : "false",
        ]
        if !genres.isEmpty {
            params["with_genres"] = genres.map(String.init).joined(separator: genreJoin.rawValue)
        }
        if !keywords.isEmpty {
            params["with_keywords"] = keywords.map(String.init).joined(separator: keywordJoin.rawValue)
        }
        if !withoutGenres.isEmpty {
            params["without_genres"] = withoutGenres.map(String.init).joined(separator: ",")
        }
        if let originalLanguage, !originalLanguage.isEmpty {
            params["with_original_language"] = originalLanguage
        }
        if !originCountry.isEmpty {
            params["with_origin_country"] = originCountry.joined(separator: originCountryJoin.rawValue)
        }
        if let voteCountGte {
            params["vote_count.gte"] = String(voteCountGte)
        }
        let dateKey = mediaType == .tv ? "first_air_date" : "primary_release_date"
        if let releaseDateGte { params["\(dateKey).gte"] = releaseDateGte }
        if let releaseDateLte { params["\(dateKey).lte"] = releaseDateLte }
        return params
    }
}
