import XCTest
@testable import DenDataset

/// Reading a plot off another language's Wikipedia.
///
/// Two thirds of the films with no plot have no English article at all, so no heading rule reaches them.
/// Half of those have an article elsewhere, and 12 of 30 sampled carried a real plot under the local
/// heading — Chess Story's German `Handlung` is 4,812 characters of narrative. bge-m3 is multilingual, so
/// the prose embeds directly with no translation step.
final class MultiLangTests: XCTestCase {
    /// A language with no heading list must return nil rather than guess. A wrong list yields production
    /// prose that reads like a plot to anyone who cannot check it — and Welsh is deliberately excluded
    /// despite appearing more often than Italian in the sample, because that frequency is the signature of
    /// bot-generated stubs rather than real coverage.
    func testAnUnsupportedLanguageIsNotGuessedAt() async throws {
        let wiki = WikipediaSource(cache: nil)
        let none = try await wiki.plot(articleTitle: "Anything at all", language: "cy")
        XCTAssertNil(none)
        XCTAssertNil(WikipediaSource.plotHeadingsByLanguage["cy"])
    }

    /// The sitelink query is restricted to the wikis with heading lists — unrestricted, it returns a row per
    /// language and multiplies the whole result set.
    func testSupportedWikisAreTheOnesWithHeadings() {
        for lang in WikipediaSource.plotHeadingsByLanguage.keys {
            XCTAssertTrue(WikipediaSource.supportedWikis.contains("https://\(lang).wikipedia.org/"))
        }
        XCTAssertFalse(WikipediaSource.supportedWikis.contains("cy.wikipedia.org"))
        XCTAssertFalse(WikipediaSource.supportedWikis.contains("en.wikipedia.org"),
                       "English is the primary path, not a fallback")
    }

    /// Every Wikipedia serves `/w/api.php`, so a cache key built from path+query alone collides across
    /// languages: the Italian "Iago (film)" and the English one are the same key. The English body was
    /// served for the Italian request, the fallback saw no plot section and gave up, and Iago's 6,728-char
    /// `Trama` was lost. A collision can return another language's article wholesale, and nothing
    /// downstream can tell — the text it yields is perfectly well-formed.
    func testCacheKeysDifferByHost() {
        let cache = ResponseCache(namespace: "test",
                                  directory: URL(fileURLWithPath: NSTemporaryDirectory()), ttl: 60)
        let query = ["action": "parse", "page": "Iago (film)", "prop": "wikitext"]
        let en = cache.key(path: "en.wikipedia.org/w/api.php", query: query)
        let it = cache.key(path: "it.wikipedia.org/w/api.php", query: query)
        XCTAssertNotEqual(en, it, "same path and query on two wikis must not share a cache entry")
    }

    func testLanguageCodeFromSite() {
        XCTAssertEqual(WikipediaSource.languageCode(fromSite: "https://de.wikipedia.org/"), "de")
        XCTAssertNil(WikipediaSource.languageCode(fromSite: "https://en.wikipedia.org/"),
                     "English is carried separately as `article`")
        XCTAssertNil(WikipediaSource.languageCode(fromSite: "https://commons.wikimedia.org/"))
    }
}
