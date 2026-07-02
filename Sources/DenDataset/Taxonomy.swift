import Foundation

/// The controlled discovery vocabulary (DT-B, from DT-taxonomy.md) — four orthogonal axes the classifier
/// (DT-C) assigns from. Versioned (`version`): a bump triggers targeted reclassification and a new index
/// artifact (DT-D splits `taxonomyVersion` from `embeddingModelVersion`). Code-defined canonical source +
/// `Codable` so the pipeline + index can serialize it. **No anime** (Animation-as-primary-genre is fine).
public struct Taxonomy: Codable, Sendable, Equatable {
    public let version: String
    /// Exactly one per title (the "Drama is too broad" fix) — the 18 TMDB genres.
    public let primaryGenres: [String]
    /// Blended subgenres (≤3 per title).
    public let subgenres: [String]
    /// Thematic labels.
    public let thematic: [String]
    /// Regional labels (origin/language; mostly recipe-driven, stored for consistency).
    public let regional: [String]
    /// Mood/tone (the LLM's main value-add — TMDB can't express these).
    public let moods: [String]

    public init(version: String, primaryGenres: [String], subgenres: [String],
                thematic: [String], regional: [String], moods: [String]) {
        self.version = version
        self.primaryGenres = primaryGenres
        self.subgenres = subgenres
        self.thematic = thematic
        self.regional = regional
        self.moods = moods
    }

    /// Every non-primary label (subgenre + thematic + regional + mood) — the multi-label space.
    public var allLabels: [String] { subgenres + thematic + regional + moods }
    /// Every label including the primary genres — used to reject off-vocabulary output.
    public var everyLabel: Set<String> { Set(primaryGenres + allLabels) }

    public func isPrimaryGenre(_ label: String) -> Bool { primaryGenres.contains(label) }
    public func contains(_ label: String) -> Bool { everyLabel.contains(label) }

    /// The current taxonomy (`t01`). De-duplicated per the DT-taxonomy.md rules: "Western" is a primary
    /// genre only (dropped as a theme); "Courtroom" folds into "Legal/Courtroom Drama" (no standalone theme).
    public static let current = Taxonomy(
        version: "t01",
        primaryGenres: [
            "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family",
            "Fantasy", "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction", "Thriller",
            "War", "Western",
        ],
        subgenres: [
            "Romantic Comedy", "Action Comedy", "Horror Comedy", "Dark Comedy", "Crime Comedy",
            "Crime Thriller", "Action Thriller", "Psychological Thriller", "Legal/Courtroom Drama",
            "Medical Drama", "Romantic Drama", "War Drama", "Historical/Period Drama", "Sci-Fi Horror",
            "Sci-Fi Action", "Fantasy Adventure", "Neo-Noir", "Supernatural Horror", "Coming-of-Age",
        ],
        thematic: [
            "Police Procedural", "Heist", "Serial Killer", "Spy/Espionage", "Assassin/Hitman",
            "Time Travel", "Cyberpunk", "Dystopian/Post-Apocalyptic", "Zombie", "Slasher", "Vampire",
            "Werewolf/Monster", "Superhero", "Mockumentary", "Whodunit/Murder Mystery", "Biopic",
            "Disaster", "Sports", "Martial Arts", "Prison", "Road Movie", "Survival", "Political", "Musical",
            // Emergent (DT-C clustering of the on-device vectors found these as cohesive groups the seed
            // taxonomy missed). Content-detectable, so kept as themes (assignable by the LLM re-pass) rather
            // than recipe-only regional labels.
            "Narco", "Giallo", "Hong Kong Action", "Samurai", "Telenovela",
        ],
        regional: [
            "Nordic Noir", "K-Drama", "Korean Thriller", "British Crime", "French Cinema",
            "Italian Cinema", "Spanish-language Thriller", "Latin American", "Turkish Drama",
            "Bollywood/Hindi", "Scandinavian", "J-Horror", "German Cinema",
        ],
        moods: [
            "Slow-burn", "Feel-good", "Mind-bending", "Cozy", "Dark & Gritty", "Wholesome", "Tearjerker",
            "Tense/Edge-of-seat", "Quirky/Offbeat", "Visually-stunning", "Twist-ending", "Bingeable",
            "Comfort-watch", "Thought-provoking",
        ]
    )
}
