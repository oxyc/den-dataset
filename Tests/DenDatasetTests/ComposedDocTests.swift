import XCTest
@testable import DenDataset

/// FP-2 — the composed embedding document. Pins the exact prose the bge-m3 embedder receives: facts + the
/// classified tags + the Wikipedia plot, with each fact clause omitted when empty and an always-present Plot
/// clause (empty for a tags-only title, which must never be skipped).
final class ComposedDocTests: XCTestCase {
    private func title(director: String? = "Christopher Nolan",
                       topCast: [String] = ["Leonardo DiCaprio", "Joseph Gordon-Levitt", "Elliot Page"],
                       genres: [String] = ["Action", "Science Fiction"],
                       overview: String = "") -> EnrichedTitle {
        EnrichedTitle(tmdbId: 27205, mediaType: .movie, title: "Inception", year: 2010, overview: overview,
                      genreIDs: [28, 878], genreNames: genres, keywords: [], originCountry: ["US"],
                      originalLanguage: "en", voteCount: 34000, director: director, topCast: topCast)
    }

    func testFullDocExactString() {
        let doc = ComposedDoc.build(
            title: title(),
            tags: ["Heist", "Mind-bending"],
            plot: "A thief who steals corporate secrets through dream-sharing pulls one last heist.")
        XCTAssertEqual(doc, "Inception (2010). Directed by Christopher Nolan. "
            + "Starring Leonardo DiCaprio, Joseph Gordon-Levitt, Elliot Page. "
            + "Genres: Action, Science Fiction. Themes: Heist, Mind-bending. "
            + "Plot: A thief who steals corporate secrets through dream-sharing pulls one last heist.")
    }

    func testNoPlotComposesFactsAndTagsWithEmptyPlot() {
        let doc = ComposedDoc.build(title: title(), tags: ["Heist"], plot: nil)
        XCTAssertEqual(doc, "Inception (2010). Directed by Christopher Nolan. "
            + "Starring Leonardo DiCaprio, Joseph Gordon-Levitt, Elliot Page. "
            + "Genres: Action, Science Fiction. Themes: Heist. Plot:")
        XCTAssertTrue(doc.hasSuffix("Plot:"), "no-plot title ends on an empty Plot clause, never skipped")
    }

    func testFactClausesOmittedWhenEmpty() {
        let bare = EnrichedTitle(tmdbId: 1, mediaType: .movie, title: "Untitled", year: nil, overview: "",
                                 genreIDs: [], genreNames: [], keywords: [], originCountry: [],
                                 originalLanguage: nil, voteCount: 0)
        let doc = ComposedDoc.build(title: bare, tags: [], plot: nil)
        XCTAssertEqual(doc, "Untitled. Plot:", "no year/director/cast/genres/tags → title + empty Plot only")
    }
}
