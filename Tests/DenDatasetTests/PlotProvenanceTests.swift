import XCTest
@testable import DenDataset

/// How a grounded title got its plot text, kept per title (oxyc/den-dataset#16).
///
/// The enrich pass knows this at the moment it decides and used to throw it away, so the only instrument
/// left was a collision census — "two titles on one article" — which sees 729 of the 2,075 titles grounded
/// on something that is not about them. The 1,346 it misses are the ones whose article grounds nobody else:
/// Silo on the novel collides with nothing and reads as clean.
final class PlotProvenanceTests: XCTestCase {
    private func title(_ plot: String = String(repeating: "x", count: 400)) -> EnrichedTitle {
        EnrichedTitle(tmdbId: 1438, mediaType: .tv, title: "The Wire", year: 2002, overview: "",
                      genreIDs: [80, 18], genreNames: ["Crime", "Drama"], keywords: [],
                      originCountry: ["US"], originalLanguage: "en", voteCount: 2715)
    }

    func testTheWinningCandidateIsRecorded() {
        let grounded = title().groundedOnWikiPlot(
            "plot", article: "Silo (novel)", revId: 9,
            provenance: PlotProvenance(role: .sourceWork, requested: "Silo (novel)",
                                       resolved: "Silo (novel)"))
        XCTAssertEqual(grounded.plotProvenance?.role, .sourceWork)
        XCTAssertEqual(grounded.plotProvenance?.redirected, false)
        XCTAssertTrue(grounded.plotProvenance?.groundedOnAnotherWork == true,
                      "a P144 source work describes the novel, not this adaptation")
    }

    /// The class a threshold change cannot reach. Wikidata's sitelink for the sequel points at a redirect
    /// that lands in the parent film's page, so the winning candidate IS the title's own article and the
    /// text is about a different film.
    func testARedirectOffTheOwnArticleIsRecordedAsSuch() {
        let moved = PlotProvenance(role: .own, requested: "Jarhead 2: Field of Fire",
                                   resolved: "Jarhead (film)")
        XCTAssertEqual(moved.role, .own)
        XCTAssertEqual(moved.redirected, true)
        XCTAssertTrue(moved.groundedOnAnotherWork)
    }

    func testAnOwnArticleThatDidNotMoveIsNotFlagged() {
        let clean = PlotProvenance(role: .own, requested: "The Wire", resolved: "The Wire")
        XCTAssertEqual(clean.redirected, false)
        XCTAssertFalse(clean.groundedOnAnotherWork)
    }

    /// A non-English article is still THIS title's article — `articlesByLang` is built from its own
    /// sitelinks — so it is not a different work, only a different language.
    func testAnotherLanguageIsStillTheTitlesOwnWork() {
        let german = PlotProvenance(role: .ownOtherLanguage, requested: "Schachnovelle (2021)",
                                    resolved: "Schachnovelle (2021)")
        XCTAssertFalse(german.groundedOnAnotherWork)
    }

    /// The Enterprise endpoint returns sections and no page title, so it cannot say whether a redirect
    /// moved the fetch. Unknown must stay unknown: reading it as "did not redirect" would report the one
    /// path that cannot tell as clean.
    func testAFetchThatNamesNoPageLeavesTheRedirectUnknown() {
        let unknown = PlotProvenance(role: .own, requested: "The Wire", resolved: nil)
        XCTAssertNil(unknown.redirected)
        XCTAssertFalse(unknown.groundedOnAnotherWork,
                       "unknown is not a violation either — it is a question this fetch cannot answer")
    }

    /// The `plot(articleTitle:)` fast path used to echo the requested title back as the resolved one, which
    /// is indistinguishable from "asked for it and got it".
    func testTheEnterprisePathReportsNoResolvedArticle() {
        let fetch = WikipediaSource.PlotFetch(text: "plot", revId: nil, resolvedArticle: nil,
                                              sections: ["Plot"])
        XCTAssertNil(fetch.resolvedArticle)
        XCTAssertNil(PlotProvenance(role: .own, requested: "The Wire",
                                    resolved: fetch.resolvedArticle).redirected)
    }

    /// A record enriched before this was recorded carries nothing, and nothing may fill it in: the article
    /// name recovers neither fact.
    func testAnUngroundedRecordCarriesNoProvenance() {
        XCTAssertNil(title().plotProvenance)
        XCTAssertNil(title().groundedOnWikiPlot("plot", article: "The Wire").plotProvenance,
                     "grounding without a recorded decision leaves it unknown, not `own`")
    }

    /// Both copy-constructors rebuild the record field by field — which is exactly where a newly added
    /// field goes missing without anyone noticing.
    func testTheCopyConstructorsPreserveIt() {
        let grounded = title().groundedOnWikiPlot(
            "plot", article: "Wuthering Heights",
            provenance: PlotProvenance(role: .sourceWork, requested: "Wuthering Heights",
                                       resolved: "Wuthering Heights"))
        XCTAssertEqual(grounded.mergingWikidata(runtimeMinutes: 60, creators: ["David Simon"])
            .plotProvenance?.role, .sourceWork)
        XCTAssertEqual(grounded.notingNoPlot("belowFloor").plotProvenance?.role, .sourceWork)
    }

    /// The strings are the on-disk vocabulary an enriched batch is read back with, and a consumer filters
    /// on them.
    func testTheRolesSerialiseAsTheirRecordedNames() {
        XCTAssertEqual(PlotArticleRole.own.rawValue, "own")
        XCTAssertEqual(PlotArticleRole.ownOtherLanguage.rawValue, "own-other-language")
        XCTAssertEqual(PlotArticleRole.sourceWork.rawValue, "source-work")
    }
}
