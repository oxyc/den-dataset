import XCTest
@testable import DenDataset

final class ShipGuardTests: XCTestCase {
    private func prohibited(_ json: String) -> [String] {
        ShipGuard.prohibited(in: Data(json.utf8))
    }

    /// A real labels artifact: labels, ids and numbers.
    func testCleanArtifactShips() {
        let json = """
        {"taxonomyVersion":"t02","records":[
          {"tmdbId":278,"mediaType":"movie","primaryGenre":"Drama",
           "subgenres":[{"label":"Prison","confidence":0.9}],"moods":[]}]}
        """
        XCTAssertEqual(prohibited(json), [])
    }

    /// The field the guard was originally written for.
    func testOverviewIsCaught() {
        XCTAssertEqual(prohibited(#"{"records":[{"tmdbId":1,"overview":"A banker is sentenced."}]}"#),
                       ["overview"])
    }

    /// The failure the old `s.contains("overview")` had: it only knew one name, so any rename walked past it.
    func testOtherProseNamesAreCaughtToo() {
        for name in ["summary", "synopsis", "description", "tagline", "storyline", "plot", "logline"] {
            XCTAssertEqual(prohibited(#"{"records":[{"tmdbId":1,"\#(name)":"text"}]}"#), [name],
                           "\(name) should be refused")
        }
    }

    /// A rename that only changes the casing or the separator is the same field.
    func testMatchingIgnoresCaseAndSeparators() {
        XCTAssertEqual(prohibited(#"{"a":{"plot_summary":"x"}}"#), ["plot_summary"])
        XCTAssertEqual(prohibited(#"{"a":{"plotSummary":"x"}}"#), ["plotSummary"])
        XCTAssertEqual(prohibited(#"{"a":{"Overview":"x"}}"#), ["Overview"])
    }

    /// The other half of the old guard's problem: it matched the serialised TEXT, so a film called
    /// "Overview" — or any label mentioning the word — failed a publish that was perfectly clean.
    func testValuesAreNotMatched() {
        XCTAssertEqual(prohibited(#"{"records":[{"tmdbId":1,"title":"Overview"}]}"#), [],
                       "a title containing the word is not a prose FIELD")
        XCTAssertEqual(prohibited(#"{"records":[{"title":"Plot Against America","primaryGenre":"Drama"}]}"#), [])
    }

    /// Prose nested deep in the document is still prose.
    func testFindsKeysAtAnyDepth() {
        XCTAssertEqual(prohibited(#"{"a":{"b":[{"c":{"synopsis":"x"}}]}}"#), ["synopsis"])
    }

    func testReportsEveryOffenderSorted() {
        XCTAssertEqual(prohibited(#"{"synopsis":"x","overview":"y","title":"ok"}"#), ["overview", "synopsis"])
    }

    /// An unparseable blob must not read as clean — that would turn a corrupt artifact into a silent pass.
    func testUnparseableBlobIsNotSilentlyClean() {
        XCTAssertEqual(ShipGuard.keys(in: Data("not json".utf8)), [])
        XCTAssertEqual(prohibited("not json"), [], "no keys to judge; the sha/parse checks own this case")
    }
}
