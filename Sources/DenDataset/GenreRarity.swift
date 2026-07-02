import Foundation

/// IDF-style rarity weights per genre (DT-A — the "Drama is too broad" fix). Broad genres that co-occur
/// with almost everything (Drama, Comedy) are down-weighted; rare/specific ones (Crime, Western,
/// Documentary, War) up-weighted, so a Crime+Drama title leans **Crime**. Heuristic priors over TMDB genre
/// prevalence — the classifier uses these to break near-tie primary-genre votes toward the rarer genre.
/// Copied verbatim from DenKit's `LibraryInsights.GenreRarity` so the producer's tie-break is byte-identical.
public enum GenreRarity {
    private static let weights: [Int: Double] = [
        18: 0.50,   // Drama (most common)
        35: 0.65,   // Comedy
        28: 0.85,   // Action
        53: 0.90,   // Thriller
        12: 1.00,   // Adventure
        10749: 1.00, // Romance
        878: 1.10,  // Science Fiction
        14: 1.15,   // Fantasy
        10751: 1.15, // Family
        27: 1.20,   // Horror
        16: 1.20,   // Animation
        9648: 1.25, // Mystery
        80: 1.35,   // Crime
        99: 1.45,   // Documentary
        36: 1.45,   // History
        10402: 1.55, // Music
        10752: 1.55, // War
        37: 1.85,   // Western (rarest)
    ]
    /// Rarity weight for a genre id (1.0 for unknown ids — neutral).
    public static func weight(_ id: Int) -> Double { weights[id] ?? 1.0 }
}
