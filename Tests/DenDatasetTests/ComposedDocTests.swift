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

    /// A series carries its showrunner. TMDB leaves `director` null for nearly all series, so without this
    /// clause a same-creator connection never reaches the embedding — measured as the reason same-creator
    /// series (The Wire / Treme / The Corner) had no local signal and lost to TMDB's own recommendations.
    func testSeriesComposesCreatedByAfterDirector() {
        let series = EnrichedTitle(
            tmdbId: 1438, mediaType: .tv, title: "The Wire", year: 2002, overview: "",
            genreIDs: [80, 18], genreNames: ["Crime", "Drama"], keywords: [], originCountry: ["US"],
            originalLanguage: "en", voteCount: 2715, director: nil, topCast: ["Dominic West"],
            createdBy: ["David Simon"])
        let doc = ComposedDoc.build(title: series, tags: ["Police Procedural"], plot: nil)
        XCTAssertEqual(doc, "The Wire (2002). Created by David Simon. Starring Dominic West. "
            + "Genres: Crime, Drama. Themes: Police Procedural. Plot:")
    }

    /// Several showrunners join like the cast does, and a title with none gains no clause at all — so the
    /// millions of films with no `created_by` compose exactly as they did before.
    func testCreatedByJoinsAndIsOmittedWhenEmpty() {
        let many = EnrichedTitle(
            tmdbId: 66732, mediaType: .tv, title: "Stranger Things", year: 2016, overview: "",
            genreIDs: [], genreNames: [], keywords: [], originCountry: ["US"],
            originalLanguage: "en", voteCount: 1, director: nil, topCast: [],
            createdBy: ["Matt Duffer", "Ross Duffer"])
        XCTAssertTrue(ComposedDoc.build(title: many, tags: [], plot: nil)
            .contains("Created by Matt Duffer, Ross Duffer."))
        XCTAssertFalse(ComposedDoc.build(title: title(), tags: [], plot: nil).contains("Created by"))
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

    // MARK: - The CC0 shape

    func testLeanDocCarriesNoTitleYearOrCast() {
        let doc = ComposedDoc.buildLean(directors: ["Christopher Nolan"], creators: [],
                                        genres: ["heist", "science fiction"], tags: ["Heist"],
                                        plot: "A thief steals secrets from dreams.")
        XCTAssertEqual(doc, "Directed by Christopher Nolan. Genres: heist, science fiction. "
            + "Themes: Heist. Plot: A thief steals secrets from dreams.")
        // The point of the shape: an actor name cannot reach the vector, so two films sharing only a cast
        // member cannot be pulled together by it.
        XCTAssertFalse(doc.contains("Starring"))
        XCTAssertFalse(doc.contains("Inception"), "no title clause")
        XCTAssertFalse(doc.contains("2010"), "no year clause")
    }

    func testLeanDocKeepsCreatorsForSeries() {
        let doc = ComposedDoc.buildLean(directors: [], creators: ["Matt Duffer", "Ross Duffer"],
                                        genres: [], tags: [], plot: nil)
        // A series usually has no single director, and that absence is CORRECT rather than missing data —
        // the creator clause is what carries authorship there.
        XCTAssertEqual(doc, "Created by Matt Duffer, Ross Duffer. Plot:")
    }

    func testLeanDocAlwaysEndsOnAPlotClause() {
        XCTAssertEqual(ComposedDoc.buildLean(directors: [], creators: [], genres: [], tags: [], plot: nil),
                       "Plot:", "a title with nothing stated still composes as a document, never empty")
    }

    func testWikidataGenreSuffixIsStripped() {
        // Wikidata labels genres with the medium appended; TMDB does not. Comparing raw strings scored 0.01
        // mean overlap against TMDB, and 0.40 once stripped.
        XCTAssertEqual(WikipediaSource.strippedGenre("science fiction film"), "science fiction")
        XCTAssertEqual(WikipediaSource.strippedGenre("drama television series"), "drama")
        XCTAssertEqual(WikipediaSource.strippedGenre("Thriller Film"), "thriller")
        XCTAssertEqual(WikipediaSource.strippedGenre("cyberpunk"), "cyberpunk", "no suffix → unchanged")
        XCTAssertEqual(WikipediaSource.strippedGenre("film"), "", "a bare medium is not a genre")

        // The suffixes added after the first version, each with a genre Q-id the corpus really references.
        XCTAssertEqual(WikipediaSource.strippedGenre("reality television"), "reality")
        XCTAssertEqual(WikipediaSource.strippedGenre("drama television program"), "drama")
        XCTAssertEqual(WikipediaSource.strippedGenre("science fiction anime and manga"), "science fiction")
        XCTAssertEqual(WikipediaSource.strippedGenre("crime fiction"), "crime")

        // `fiction` must not eat the head of a genre that ENDS in "science fiction". An equality guard
        // let these through as "hard science" and "military science", and the stripped label is written
        // into the shipped entity map and the composed document the embedding reads — so it travels.
        XCTAssertEqual(WikipediaSource.strippedGenre("hard science fiction"), "hard science fiction")
        XCTAssertEqual(WikipediaSource.strippedGenre("military science fiction"), "military science fiction")
        XCTAssertEqual(WikipediaSource.strippedGenre("science fiction"), "science fiction")
    }

    func testUnresolvedQIdLabelsAreDropped() throws {
        // When the label service cannot resolve a value it returns the bare Q-id. Embedding "Q1379241" as a
        // director teaches the model nothing, so it must not reach the doc.
        let json = """
        {"results":{"bindings":[
          {"tmdb":{"value":"603"},"vLabel":{"value":"Lana Wachowski"}},
          {"tmdb":{"value":"603"},"vLabel":{"value":"Q1379241"}}
        ]}}
        """
        let parsed = try WikipediaSource.parseLabelled(Data(json.utf8))
        XCTAssertEqual(parsed[603], ["Lana Wachowski"])
    }
}
