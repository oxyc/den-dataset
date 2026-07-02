import Foundation

/// The embedding input document (FP-2). Replaces the old lexical `title + overview + keywords` bag with a
/// composed natural-language doc built from FACTS (title, year, director, cast, TMDB genres), the title's
/// classified TAGS (subgenre + mood labels), and the live WIKIPEDIA plot. bge-m3 (via den-embed) reads this
/// prose; a title with no Wikipedia plot still composes on facts + tags with an empty Plot — a title is
/// NEVER skipped for lack of a plot.
///
/// Shape:
///   "Title (Year). Directed by <d>. Starring <a, b, c>. Genres: <g1, g2>. Themes: <t1, t2>. Plot: <plot>"
///
/// Each fact segment is omitted when its source is empty (no director → no "Directed by" clause), so the doc
/// stays clean instead of carrying "Directed by ." Placeholders. The Plot clause is always present (possibly
/// empty) so the tags-only doc still reads as a document rather than a fragment.
public enum ComposedDoc {
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
