import Foundation

/// The classifier's grounding signal (DT-C): `label (lowercased) → the TMDB keyword ids that back that
/// concept`. TMDB-keyword agreement only *raises* a label's confidence (a small bonus); absence is silent.
///
/// This map is BAKED from `RecipeCatalog` in the Den app — an app UI construct that must not move into the
/// producer. It is the exact output of:
///
///     var map: [String: Set<Int>] = [:]
///     for recipe in RecipeCatalog.all where !recipe.query.keywords.isEmpty {
///         map[recipe.title.lowercased(), default: []].formUnion(recipe.query.keywords)
///     }
///     map["spy/espionage"]    = map["spy & espionage"]      // taxonomy-label aliases for the two recipe
///     map["assassin/hitman"]  = map["assassin & hitman"]    // titles whose wording differs
///     map["whodunit/murder mystery"] = nil
///     return map.compactMapValues { $0 }
///
/// Only the `thematic` recipe family carries keyword ids (blended/regional/disambiguation are genre- or
/// origin-driven), so the 16 keyword-backed recipe titles + the 2 aliases give the 18 entries below. If
/// `RecipeCatalog` changes in the app, re-derive this literal.
public enum GroundingKeywords {
    public static let map: [String: Set<Int>] = [
        // thematic recipes (title.lowercased() → query.keywords)
        "police procedural": [268067, 15167, 6149],
        "heist": [10051],
        "serial killer": [10714],
        "spy & espionage": [5265, 236615],
        "assassin & hitman": [782, 177964],
        "time travel": [4379],
        "cyberpunk": [12190],
        "zombie": [12377, 186565],
        "slasher": [12339],
        "superhero": [9715],
        "post-apocalyptic": [4458, 4565],
        "coming-of-age": [10683],
        "courtroom & legal": [214780, 222517, 254459, 33519],
        "martial arts": [779, 780, 9917],
        "biopic": [9672],
        "mockumentary": [11800],
        // taxonomy-label aliases (the recipe title wording differs from the taxonomy label)
        "spy/espionage": [5265, 236615],
        "assassin/hitman": [782, 177964],
    ]
}
