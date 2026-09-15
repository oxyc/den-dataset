import XCTest
@testable import DenDataset

/// Why a title has no plot, kept per title.
///
/// `hasWikiPlot: false` on its own is what makes every finder improvement cost a full re-scrape. It has four
/// causes wanting four different fixes — no English article at all, no section the heading rules recognise,
/// prose under the floor, and a definitive fetch failure — and collapsed into one boolean the only safe
/// answer to "who should I re-run?" is "all 19,542". That is how this corpus came to be re-scraped again and
/// again.
final class NoPlotReasonTests: XCTestCase {
    private func title(plot: String = "", hasPlot: Bool = false) -> EnrichedTitle {
        EnrichedTitle(tmdbId: 1438, mediaType: .tv, title: "The Wire", year: 2002, overview: plot,
                      genreIDs: [80, 18], genreNames: ["Crime", "Drama"], keywords: [],
                      originCountry: ["US"], originalLanguage: "en", voteCount: 2715,
                      hasWikiPlot: hasPlot)
    }

    func testAReasonIsCarriedOnTheRecord() {
        let noted = title().notingNoPlot("noSection")
        XCTAssertEqual(noted.noPlotReason, "noSection")
        XCTAssertFalse(noted.hasWikiPlot)
        XCTAssertEqual(noted.tmdbId, 1438, "the rest of the record survives")
        XCTAssertEqual(noted.voteCount, 2715)
    }

    /// Finding a plot has to CLEAR the reason, or a title that recovers keeps explaining a failure that no
    /// longer happened — and the subset a later pass re-runs would include titles that already worked.
    func testGroundingClearsAnEarlierReason() {
        let recovered = title().notingNoPlot("noSection")
            .groundedOnWikiPlot(String(repeating: "x", count: 400), article: "The Wire", revId: 42)
        XCTAssertNil(recovered.noPlotReason)
        XCTAssertTrue(recovered.hasWikiPlot)
        XCTAssertEqual(recovered.plotArticle, "The Wire")
        XCTAssertEqual(recovered.plotRevId, 42)
    }

    /// The Wikidata merge runs after the plot hop and rebuilds the record field by field, which is exactly
    /// where a newly added field goes missing without anyone noticing.
    func testTheWikidataMergePreservesTheReason() {
        let merged = title().notingNoPlot("noArticle")
            .mergingWikidata(runtimeMinutes: 60, creators: ["David Simon"])
        XCTAssertEqual(merged.noPlotReason, "noArticle")
        XCTAssertEqual(merged.createdBy, ["David Simon"])
    }
}
