import Foundation

/// The last check before an artifact is published: does it carry expressive prose it must not?
///
/// TMDB's terms (§1.C) bar shipping their content, and the published index is derived labels and quantized
/// vectors only. The pipeline now drops TMDB's overview at the client boundary, so nothing downstream should
/// be able to carry it — this is the check that says so out loud at the moment of publishing, when a mistake
/// stops being recoverable.
///
/// Two things it deliberately does NOT do, both of which the previous one-line version got wrong:
///
///  - It does not substring-match the serialised blob. `s.contains("overview")` is true of a film *titled*
///    "Overview" and of any plot mentioning the word, so the guard could fail on innocent data — and a guard
///    that cries wolf is one someone eventually deletes.
///  - It does not check a single name. `overview` was the field that existed when the check was written;
///    `summary`, `synopsis`, `description` or `tagline` would have sailed straight past it, which is exactly
///    how this class of rule rots.
public enum ShipGuard {
    /// Field names that carry expressive prose. A published artifact holds labels, ids and numbers; if any of
    /// these appears as a KEY, something upstream started shipping text.
    ///
    /// `plot` is on the list even though the pipeline's own plots are CC0 Wikipedia text: that text belongs in
    /// the committed corpus and the embedding, not in an artifact served to devices, and a `plot` key showing
    /// up in one means a composed doc leaked into the output.
    public static let prohibitedKeys: Set<String> = [
        "overview", "summary", "synopsis", "description", "plot", "plotsummary",
        "tagline", "storyline", "premise", "abstract", "blurb", "logline", "review",
    ]

    /// Every key appearing anywhere in a JSON document, at any depth.
    public static func keys(in data: Data) -> Set<String> {
        var found: Set<String> = []
        func walk(_ value: Any) {
            if let object = value as? [String: Any] {
                for (key, child) in object {
                    found.insert(key)
                    walk(child)
                }
            } else if let array = value as? [Any] {
                for child in array { walk(child) }
            }
        }
        guard let root = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) else {
            return found
        }
        walk(root)
        return found
    }

    /// The prohibited field names this document actually contains, sorted. Empty means it is safe to ship.
    ///
    /// Matched case- and separator-insensitively, so `plot_summary`, `plotSummary` and `PlotSummary` are one
    /// name. A guard that a rename defeats is not a guard.
    public static func prohibited(in data: Data) -> [String] {
        let normalize = { (key: String) in
            key.lowercased().filter { $0.isLetter || $0.isNumber }
        }
        return keys(in: data)
            .filter { prohibitedKeys.contains(normalize($0)) }
            .sorted()
    }
}
