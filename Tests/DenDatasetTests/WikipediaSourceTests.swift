import XCTest
@testable import DenDataset

/// What is left of the Wikipedia source once the plot hop moved to `lib/plot.py`: the article name the
/// facts pass's titles hop reads off a sitelink.
final class WikipediaSourceTests: XCTestCase {
    func testArticleTitlePercentDecodesAndUnderscores() {
        XCTAssertEqual(WikipediaSource.articleTitle(fromURL: "https://en.wikipedia.org/wiki/The_Matrix"), "The Matrix")
        XCTAssertEqual(WikipediaSource.articleTitle(fromURL: "https://en.wikipedia.org/wiki/Am%C3%A9lie"), "Amélie")
        XCTAssertNil(WikipediaSource.articleTitle(fromURL: "https://example.com/no-wiki-path"))
    }
}
