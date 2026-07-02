import Foundation

/// The fixed TMDB genre id→name maps (TMDB's `/genre/{movie,tv}/list`, which almost never changes). The
/// producer needs only the id↔name tables the classifier reads (movie name→id for the IDF rarity tie-break).
/// The app keeps its own richer copy in DenKit — this is a self-contained subset.
public enum GenreCatalog {
    public static let movie: [Int: String] = [
        28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
        99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
        27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
        878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    ]
    public static let tv: [Int: String] = [
        10759: "Action & Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
        99: "Documentary", 18: "Drama", 10751: "Family", 10762: "Kids", 9648: "Mystery",
        10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap",
        10767: "Talk", 10768: "War & Politics", 37: "Western",
    ]

    /// Genre name for an id within a media type (nil for `.person`/unknown).
    public static func name(_ id: Int, _ mediaType: MediaType) -> String? {
        switch mediaType {
        case .movie: return movie[id]
        case .tv: return tv[id]
        case .person: return nil
        }
    }
}
