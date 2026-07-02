import XCTest
@testable import DenDataset

/// FP-2 — the live Wikipedia enrichment source. Network methods are exercised by the end-to-end validation;
/// these lock the PURE seams: wikitext cleaning, the SPARQL mapping decode, section selection, and the
/// article-title extraction — the parts a fixture can pin exactly.
final class WikipediaSourceTests: XCTestCase {
    // MARK: - wikitext cleaning

    func testCleanWikitextStripsMarkupToProse() {
        let wikitext = """
        '''Inception''' is a 2010 film.<ref>{{cite|x}}</ref> A thief [[Dom Cobb|Cobb]] steals \
        secrets.<ref name="a"/> He plans a [[heist]].[1]
        """
        let prose = WikipediaSource.cleanWikitext(wikitext)
        XCTAssertEqual(prose, "Inception is a 2010 film. A thief Cobb steals secrets. He plans a heist.")
    }

    func testCleanWikitextDropsTemplatesFilesAndComments() {
        let wikitext = """
        <!-- hidden -->{{Infobox film|name=X}}[[File:Poster.jpg|thumb|A poster]]The ''crew'' escapes.
        """
        XCTAssertEqual(WikipediaSource.cleanWikitext(wikitext), "The crew escapes.")
    }

    // MARK: - SPARQL decode

    func testParseWikidataMapsIdsToArticleAndImdb() {
        let json = """
        {"head":{"vars":["tmdb","article","imdb"]},"results":{"bindings":[
          {"tmdb":{"type":"literal","value":"27205"},
           "article":{"type":"uri","value":"https://en.wikipedia.org/wiki/Inception"},
           "imdb":{"type":"literal","value":"tt1375666"}},
          {"tmdb":{"type":"literal","value":"603"},
           "article":{"type":"uri","value":"https://en.wikipedia.org/wiki/The_Matrix"}}
        ]}}
        """
        let map = WikipediaSource.parseWikidata(Data(json.utf8))
        XCTAssertEqual(map[27205]?.article, "Inception")
        XCTAssertEqual(map[27205]?.imdb, "tt1375666")
        XCTAssertEqual(map[603]?.article, "The Matrix")
        XCTAssertNil(map[603]?.imdb, "no P345 binding → nil imdb")
        XCTAssertNil(map[999])
    }

    func testArticleTitlePercentDecodesAndUnderscores() {
        XCTAssertEqual(WikipediaSource.articleTitle(fromURL: "https://en.wikipedia.org/wiki/The_Matrix"), "The Matrix")
        XCTAssertEqual(WikipediaSource.articleTitle(fromURL: "https://en.wikipedia.org/wiki/Am%C3%A9lie"), "Amélie")
        XCTAssertNil(WikipediaSource.articleTitle(fromURL: "https://example.com/no-wiki-path"))
    }

    // MARK: - section selection

    func testPlotSectionIndexPrefersPlotOverSynopsis() {
        let json = """
        {"parse":{"sections":[
          {"line":"Synopsis","index":"2"},
          {"line":"Plot","index":"1"},
          {"line":"Cast","index":"3"}]}}
        """
        XCTAssertEqual(WikipediaSource.plotSectionIndex(Data(json.utf8)), "1", "exact Plot beats Synopsis")
    }

    func testPlotSectionIndexNilWhenAbsent() {
        let json = #"{"parse":{"sections":[{"line":"Cast","index":"1"},{"line":"Reception","index":"2"}]}}"#
        XCTAssertNil(WikipediaSource.plotSectionIndex(Data(json.utf8)))
    }

    func testDecodeWikitextPullsTheString() {
        let json = #"{"parse":{"wikitext":"'''Foo''' bar."}}"#
        XCTAssertEqual(WikipediaSource.decodeWikitext(Data(json.utf8)), "'''Foo''' bar.")
    }

    // MARK: - Enterprise structured-contents decode

    func testEnterprisePlotPullsPlotSection() {
        let json = """
        [{"sections":[
          {"name":"Abstract","value":"An intro."},
          {"name":"Plot","value":"A hero saves the day."}]}]
        """
        XCTAssertEqual(WikipediaSource.enterprisePlot(Data(json.utf8)), "A hero saves the day.")
    }
}
