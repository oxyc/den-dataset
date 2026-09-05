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

    func testCleanWikitextStripsSectionHeadings() {
        let wikitext = """
        == Plot ==
        A hero begins the quest.
        === Act two ===
        The hero prevails.
        """
        XCTAssertEqual(WikipediaSource.cleanWikitext(wikitext), "A hero begins the quest.\nThe hero prevails.")
    }

    func testCleanWikitextStripsTables() {
        let wikitext = """
        The team assembles.
        {| class="wikitable"
        |-
        ! Role !! Actor
        |-
        | Lead || {{nowrap|A. Star}}
        |}
        Then they escape.
        """
        XCTAssertEqual(WikipediaSource.cleanWikitext(wikitext), "The team assembles.\nThen they escape.")
    }

    func testCleanWikitextDecodesHTMLEntities() {
        let wikitext = "Tom&nbsp;&amp;&nbsp;Jerry fight&mdash;then &quot;make up&quot;."
        XCTAssertEqual(WikipediaSource.cleanWikitext(wikitext), "Tom & Jerry fight—then \"make up\".")
    }

    // MARK: - SPARQL decode

    func testParseWikidataMapsIdsToArticleAndImdb() throws {
        let json = """
        {"head":{"vars":["tmdb","article","imdb"]},"results":{"bindings":[
          {"tmdb":{"type":"literal","value":"27205"},
           "article":{"type":"uri","value":"https://en.wikipedia.org/wiki/Inception"},
           "imdb":{"type":"literal","value":"tt1375666"}},
          {"tmdb":{"type":"literal","value":"603"},
           "article":{"type":"uri","value":"https://en.wikipedia.org/wiki/The_Matrix"}}
        ]}}
        """
        let map = try WikipediaSource.parseWikidata(Data(json.utf8))
        XCTAssertEqual(map[27205]?.article, "Inception")
        XCTAssertEqual(map[27205]?.imdb, "tt1375666")
        XCTAssertEqual(map[603]?.article, "The Matrix")
        XCTAssertNil(map[603]?.imdb, "no P345 binding → nil imdb")
        XCTAssertNil(map[999])
    }

    /// Runtime and creators ride along on the article hop. A title binds once PER creator, so they must
    /// ACCUMULATE across rows — first-wins would silently drop the second Duffer brother.
    func testParseWikidataAccumulatesCreatorsAndTakesShortestRuntime() throws {
        let json = Data("""
        {"results":{"bindings":[
          {"tmdb":{"value":"66732"},"article":{"value":"https://en.wikipedia.org/wiki/Stranger_Things"},
           "runtime":{"value":"70"},"creatorLabel":{"value":"Ross Duffer"}},
          {"tmdb":{"value":"66732"},"article":{"value":"https://en.wikipedia.org/wiki/Stranger_Things"},
           "runtime":{"value":"50.0"},"creatorLabel":{"value":"Matt Duffer"}}
        ]}}
        """.utf8)
        let map = try WikipediaSource.parseWikidata(json)
        let entry = try XCTUnwrap(map[66732])
        XCTAssertEqual(entry.creators, ["Matt Duffer", "Ross Duffer"], "both creators, sorted for stability")
        XCTAssertEqual(entry.runtimeMinutes, 50, "the shortest cut answers 'have I got time for this'")
        XCTAssertEqual(entry.article, "Stranger Things")
    }

    /// The facts are OPTIONAL in the query; a title with neither still returns its article, which is what
    /// the call exists for.
    func testParseWikidataToleratesMissingRuntimeAndCreators() throws {
        let json = Data("""
        {"results":{"bindings":[
          {"tmdb":{"value":"1"},"article":{"value":"https://en.wikipedia.org/wiki/Nothing"}}
        ]}}
        """.utf8)
        let entry = try XCTUnwrap(try WikipediaSource.parseWikidata(json)[1])
        XCTAssertNil(entry.runtimeMinutes)
        XCTAssertTrue(entry.creators.isEmpty)
        XCTAssertEqual(entry.article, "Nothing")
    }

    /// Wikidata wins over TMDB's `created_by` where it has creators — the same rule the plot follows, since
    /// this text reaches an embedder and Wikidata is CC0 — and TMDB fills the gap where it does not.
    func testMergingWikidataPrefersWikidataCreatorsAndFallsBackToTMDB() {
        let base = EnrichedTitle(tmdbId: 1, mediaType: .tv, title: "S", year: 2020, overview: "",
                                 genreIDs: [], genreNames: [], keywords: [], originCountry: [],
                                 originalLanguage: "en", voteCount: 1, createdBy: ["TMDB Person"])
        let wiki = base.mergingWikidata(runtimeMinutes: 42, creators: ["Wikidata Person"])
        XCTAssertEqual(wiki.createdBy, ["Wikidata Person"])
        XCTAssertEqual(wiki.runtimeMinutes, 42)

        let noWiki = base.mergingWikidata(runtimeMinutes: nil, creators: [])
        XCTAssertEqual(noWiki.createdBy, ["TMDB Person"], "TMDB covers what Wikidata misses")
        XCTAssertNil(noWiki.runtimeMinutes)
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

    /// The real Enterprise schema nests the plot prose in the section's `has_parts` paragraphs (the section's
    /// own `value` is empty); a flat `value` read misses it entirely. Joins the paragraphs in order.
    func testEnterprisePlotJoinsHasPartsParagraphs() {
        let json = """
        [{"sections":[
          {"name":"Abstract","has_parts":[{"type":"paragraph","value":"An intro."}]},
          {"name":"Plot","has_parts":[
            {"type":"paragraph","value":"First paragraph."},
            {"type":"paragraph","value":"Second paragraph."}]}]}]
        """
        XCTAssertEqual(WikipediaSource.enterprisePlot(Data(json.utf8)), "First paragraph.\nSecond paragraph.")
    }

    /// A plot split into sub-sections (each a nested section with its own paragraphs) flattens recursively.
    func testEnterprisePlotFlattensNestedSubsections() {
        let json = """
        [{"sections":[{"name":"Plot","has_parts":[
          {"type":"paragraph","value":"Setup."},
          {"type":"section","name":"Act II","has_parts":[{"type":"paragraph","value":"Rising action."}]}]}]}]
        """
        XCTAssertEqual(WikipediaSource.enterprisePlot(Data(json.utf8)), "Setup.\nRising action.")
    }

    /// An empty result set and an undecodable body mean opposite things to the enrich checkpoint: the first
    /// is "these titles have no Wikipedia article" (definitive, checkpoint them), the second is "ask again"
    /// (transient, retry the batch). Returning [:] for both made a WDQS maintenance page permanently strip
    /// the whole batch's plots — and with --require-wiki-plot, drop it from the shipped index entirely.
    func testZeroBindingsIsAnAnswerButAnUndecodableBodyIsNot() throws {
        let empty = try WikipediaSource.parseWikidata(Data(#"{"results":{"bindings":[]}}"#.utf8))
        XCTAssertTrue(empty.isEmpty)

        XCTAssertThrowsError(try WikipediaSource.parseWikidata(Data("<html>service unavailable</html>".utf8)))
        XCTAssertThrowsError(try WikipediaSource.parseWikidata(Data()))
    }
}
