import Foundation

/// What a TMDB id refers to. TMDB ids are **type-ambiguous** (movie 550 ≠ tv 550),
/// so Den keys identity on `(tmdbId, mediaType)` everywhere (SPEC "Correctness
/// landmines"). `person` appears in multi-search + credits.
public enum MediaType: String, Codable, Sendable, CaseIterable {
    case movie
    case tv
    case person

    /// TMDB's REST path segment for detail/credits endpoints.
    public var pathSegment: String { rawValue }

    /// User-facing noun.
    public var displayName: String {
        switch self {
        case .movie: return "Movie"
        case .tv: return "Series"
        case .person: return "Person"
        }
    }
}
