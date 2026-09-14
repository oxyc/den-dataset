import XCTest
@testable import DenDataset

final class SourceKindTests: XCTestCase {
    private func kind(_ label: String) -> WikidataFacts.SourceKind {
        WikidataFacts.sourceKind(forType: label)
    }

    /// The two types that carry most of the corpus: 212 of 400 sampled source works are "literary work" and
    /// another 32 are "written work".
    func testLiteraryTypesAreBooks() {
        for label in ["literary work", "written work", "novel", "novella", "memoir", "fairy tale",
                      "short story", "autobiography", "light novel"] {
            XCTAssertEqual(kind(label), .book, label)
        }
    }

    /// Wikidata keeps inventing narrower types; a suffix rule catches them without listing every one.
    func testUnlistedVariantsFallToTheRightFamily() {
        XCTAssertEqual(kind("serial novel"), .book)
        XCTAssertEqual(kind("mystery novel"), .book)
        XCTAssertEqual(kind("children's literature"), .book)
        XCTAssertEqual(kind("seinen manga"), .comic)
        XCTAssertEqual(kind("radio play"), .play)
    }

    /// "Based on Batman" is a different claim from "based on a novel" — folding them together would fill a
    /// books row with superhero films.
    func testCharactersAreNotTexts() {
        for label in ["film character", "comics character", "fictional human", "superhero team",
                      "television character"] {
            XCTAssertEqual(kind(label), .character, label)
        }
    }

    /// A remake of another film is an adaptation, but not of a book.
    func testScreenWorksAreTheirOwnKind() {
        XCTAssertEqual(kind("film"), .screen)
        XCTAssertEqual(kind("television series"), .screen)
        XCTAssertEqual(kind("anime television series"), .screen)
        XCTAssertEqual(kind("media franchise"), .franchise)
    }

    func testUnknownTypeIsOther() {
        XCTAssertEqual(kind("archaeological site"), .other)
        XCTAssertEqual(kind(""), .other)
    }

    func testMatchingIgnoresCaseAndPadding() {
        XCTAssertEqual(kind("  Literary Work  "), .book)
    }

    /// A work is usually several things at once. The strongest signal wins, so a "literary work" that is also
    /// a "written work" is a book, and anything real beats `other`.
    func testStrongestKindWinsAcrossTypes() {
        XCTAssertEqual(WikidataFacts.sourceKind(forTypes: ["written work", "literary work"]), .book)
        XCTAssertEqual(WikidataFacts.sourceKind(forTypes: ["archaeological site", "novel"]), .book)
        XCTAssertEqual(WikidataFacts.sourceKind(forTypes: ["film character", "comic book series"]), .comic,
                       "a text beats a character")
        XCTAssertEqual(WikidataFacts.sourceKind(forTypes: ["archaeological site"]), .other)
        XCTAssertNil(WikidataFacts.sourceKind(forTypes: []))
    }
}
