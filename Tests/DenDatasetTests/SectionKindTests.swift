import XCTest
@testable import DenDataset

/// Which of an article's sections describe the WORK.
///
/// The finder used to take the first section named Plot/Synopsis/Premise and stop. The Wire's article is
/// 66,190 characters and has none of those headings, so it yielded nothing at all — while 22,892 characters
/// of plot sat under `Season 1 (2002)` … `Season 5 (2008)`. Across 33 sampled no-plot TV titles with >=50
/// votes the old rule grounded 0; taking every describing section grounds 21.
///
/// Every case below is one this classifier got wrong while it was being written.
final class SectionKindTests: XCTestCase {
    /// The Wire nests its seasons under `Cast and characters` — a parent that reads as a cast list. Excluding
    /// a leaf because of its parent threw away the whole plot.
    func testAStoryLeafOutranksAnExcludedParent() {
        XCTAssertEqual(WikipediaSource.sectionKind("Season 1 (2002)", parent: "Cast and characters"), .story)
        XCTAssertEqual(WikipediaSource.sectionKind("Season 5 (2008)", parent: "Cast and characters"), .story)
        XCTAssertEqual(WikipediaSource.sectionKind("Plot", parent: "Production"), .story)
    }

    /// `Institutional dysfunction` and `Surveillance` say nothing thematic in their own names; only their
    /// parent does. Without inheritance The Wire loses its themes as well as its plot.
    func testAThemeParentIsInheritedByChildren() {
        XCTAssertEqual(WikipediaSource.sectionKind("Institutional dysfunction", parent: "Themes"), .theme)
        XCTAssertEqual(WikipediaSource.sectionKind("Surveillance", parent: "Themes"), .theme)
    }

    /// `Realism` under `Style` is 2,618 characters about the writers' research — production, not premise.
    /// An unrecognised leaf under an unrecognised parent must stay out.
    func testAnUnrecognisedLeafStaysExcluded() {
        XCTAssertEqual(WikipediaSource.sectionKind("Realism", parent: "Style"), .excluded)
        XCTAssertEqual(WikipediaSource.sectionKind("Visual novel", parent: "Style"), .excluded)
        XCTAssertEqual(WikipediaSource.sectionKind("Critical response", parent: "Reception"), .excluded)
    }

    /// "Episode structure" begins with a serial word and is production prose. Matching on the first word
    /// alone swept it in; the word must stand alone or name an instalment.
    func testASerialWordAloneIsNotAnInstalment() {
        XCTAssertFalse(WikipediaSource.isSerialHeading("episode structure"))
        XCTAssertFalse(WikipediaSource.isSerialHeading("seasonal marketing"))
        XCTAssertTrue(WikipediaSource.isSerialHeading("season 1 (2002)"))
        XCTAssertTrue(WikipediaSource.isSerialHeading("part one"))
        XCTAssertTrue(WikipediaSource.isSerialHeading("episodes"))
    }

    /// "Series" already ends in s. De-pluralising it unconditionally produced "serie", which matched
    /// nothing — and silently dropped every British series article, 16,679 characters of Misfits included.
    func testSeriesIsNotDePluralisedIntoNothing() {
        XCTAssertTrue(WikipediaSource.isSerialHeading("series 1 (2009)"))
        XCTAssertTrue(WikipediaSource.isSerialHeading("series 5 (2013)"))
        XCTAssertTrue(WikipediaSource.isSerialHeading("seasons"), "the plural still de-pluralises")
    }

    /// Splitting the article locally is what replaced one request per section.
    func testSectionsSplitFromWikitext() {
        let wikitext = """
        Lead prose.

        == Production ==
        Made in Baltimore.

        == Cast and characters ==
        === Season 1 (2002) ===
        The investigation is triggered when a witness changes her story.
        """
        let parts = WikipediaSource.splitSections(wikitext)
        XCTAssertEqual(parts.map(\.heading), ["", "Production", "Cast and characters", "Season 1 (2002)"])
        XCTAssertEqual(parts.last?.level, 3)
        XCTAssertTrue(parts.last?.body.contains("witness changes her story") ?? false)
    }

    /// End to end on the shape that motivated all of this: the plot is kept, the production is not, and the
    /// heading each piece came from is recorded.
    func testDescribingProseKeepsTheStoryAndDropsTheMaking() {
        let wikitext = """
        == Production ==
        \(String(repeating: "Casting details. ", count: 20))

        == Cast and characters ==
        === Season 1 (2002) ===
        \(String(repeating: "McNulty meets the judge. ", count: 20))

        == Themes ==
        === Institutional dysfunction ===
        \(String(repeating: "The institutions fail the characters. ", count: 10))

        == Reception ==
        \(String(repeating: "Widely acclaimed. ", count: 20))
        """
        let found = WikipediaSource.describingProse(wikitext)
        XCTAssertEqual(found.sections, ["Season 1 (2002)", "Institutional dysfunction"])
        XCTAssertTrue(found.text.contains("McNulty"))
        XCTAssertTrue(found.text.contains("institutions fail"))
        XCTAssertFalse(found.text.contains("Casting"), "production is not the work")
        XCTAssertFalse(found.text.contains("acclaimed"), "reception is not the work")
    }
}
