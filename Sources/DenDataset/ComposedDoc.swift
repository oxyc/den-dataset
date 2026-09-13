import Foundation

/// The embedding input document (FP-2). Replaces the old lexical `title + overview + keywords` bag with a
/// composed natural-language doc built from FACTS (title, year, director, cast, TMDB genres), the title's
/// classified TAGS (subgenre + mood labels), and the live WIKIPEDIA plot. bge-m3 (via den-embed) reads this
/// prose; a title with no Wikipedia plot still composes on facts + tags with an empty Plot — a title is
/// NEVER skipped for lack of a plot.
///
/// Shape:
///   "Title (Year). Directed by <d>. Created by <c>. Starring <a, b, c>. Genres: <g1, g2>. Themes: <t1, t2>. Plot: <plot>"
///
/// Each fact segment is omitted when its source is empty (no director → no "Directed by" clause), so the doc
/// stays clean instead of carrying "Directed by ." Placeholders. The Plot clause is always present (possibly
/// empty) so the tags-only doc still reads as a document rather than a fragment.
public enum ComposedDoc {
    /// The CC0 doc shape: no title, no year, no cast, and every fact clause sourced from Wikidata rather than
    /// TMDB. Takes bare arrays, not an `EnrichedTitle`, so nothing TMDB-shaped can reach it by accident.
    ///
    /// Dropping title/year/cast is a retrieval decision, not just a licensing one. Measured by ablating each
    /// clause and scoring how much closer thematically-similar pairs sit than same-actor-opposite-tone pairs:
    /// the full doc scores 0.018, this shape 0.091 — 5x the discrimination. Cast is most of that (0.061 on
    /// its own): actor names make *Good Will Hunting* and *Jumanji* neighbours, which is an identity match,
    /// not a similarity. Year alone measured flat (0.016 vs 0.018) and is dropped for the token budget and
    /// because it belongs in metadata, not because it polluted the vector.
    ///
    /// Director and genre clauses are KEPT but unsettled: the wider probe scored `plot+tags only` higher
    /// still (0.121), and the control set has no same-director pairs, so it cannot see director acting as an
    /// identity token the way cast does. This is the A/B that settles it.
    public static func buildLean(directors: [String], creators: [String], genres: [String],
                                 tags: [String], plot: String?) -> String {
        var parts: [String] = []
        if !directors.isEmpty { parts.append("Directed by \(directors.joined(separator: ", ")).") }
        if !creators.isEmpty { parts.append("Created by \(creators.joined(separator: ", ")).") }
        if !genres.isEmpty { parts.append("Genres: \(genres.joined(separator: ", ")).") }
        if !tags.isEmpty { parts.append("Themes: \(tags.joined(separator: ", ")).") }
        parts.append("Plot: \((plot ?? "").trimmingCharacters(in: .whitespacesAndNewlines))")
        return parts.joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }

    public static func build(title: EnrichedTitle, tags: [String], plot: String?) -> String {
        var parts: [String] = []

        if let year = title.year {
            parts.append("\(title.title) (\(year)).")
        } else {
            parts.append("\(title.title).")
        }
        if let director = title.director, !director.trimmingCharacters(in: .whitespaces).isEmpty {
            parts.append("Directed by \(director).")
        }
        // Series carry their showrunner here, since TMDB leaves `director` null for nearly all of them —
        // without this a same-creator connection (a Wire/Treme/Corner cluster) is absent from the doc, and
        // so from the embedding, which is why those series had no local signal to rank on.
        if !title.createdBy.isEmpty {
            parts.append("Created by \(title.createdBy.joined(separator: ", ")).")
        }
        if !title.topCast.isEmpty {
            parts.append("Starring \(title.topCast.joined(separator: ", ")).")
        }
        if !title.genreNames.isEmpty {
            parts.append("Genres: \(title.genreNames.joined(separator: ", ")).")
        }
        if !tags.isEmpty {
            parts.append("Themes: \(tags.joined(separator: ", ")).")
        }
        let plotText = (plot ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        parts.append("Plot: \(plotText)")

        return parts.joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }
}
