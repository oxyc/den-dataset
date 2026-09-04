import XCTest
@testable import DenDataset

/// The classifier's aggregation ran over Swift `Dictionary`s and finished with `sorted(by:)` — an unstable
/// sort over an order that Swift's per-process String hash seed randomises. Equal confidences therefore came
/// out in an arbitrary order AND, because only the top three survive, an arbitrary SUBSET of them shipped.
/// Same inputs, different labels run to run: `assemble --force` over an unchanged batch could change what
/// was published, and re-deriving the corpus was not reproducible.
///
/// These pin the tiebreak to the label, which is the only ordering available that does not move.
final class ClassifierDeterminismTests: XCTestCase {

    private let classifier = TaxonomyClassifier(llm: NoLLMStub(), samples: 1)

    private func title(voteCount: Int = 500, keywords: [String] = []) -> EnrichedTitle {
        EnrichedTitle(tmdbId: 1, mediaType: .movie, title: "T", year: 2020, overview: "",
                      genreIDs: [], genreNames: [], keywords: keywords.map { Keyword(id: 0, name: $0) },
                      originCountry: [], originalLanguage: "en", voteCount: voteCount)
    }

    private func vote(primary: String, moods: [(String, Double)] = [],
                      subgenres: [(String, Double)] = []) -> String {
        func labels(_ pairs: [(String, Double)]) -> String {
            pairs.map { "{\"label\":\"\($0.0)\",\"confidence\":\($0.1)}" }.joined(separator: ",")
        }
        return """
        {"primary_genre":"\(primary)","subgenres":[\(labels(subgenres))],"moods":[\(labels(moods))]}
        """
    }

    /// Six moods at one confidence, all above the 0.55 cutoff, and only three seats. Which three ship was
    /// decided by hash order; now it is the three alphabetically first, in order.
    func testTiedLabelsBeyondTheTopThreeResolveByLabel() throws {
        let tied = ["Wholesome", "Campy", "Tearjerker", "Bingeable", "Cozy", "Slow-burn"]
        let raw = vote(primary: "Drama", moods: tied.map { ($0, 0.8) })
        let result = try XCTUnwrap(classifier.classify(rawVotes: [raw], title: title()))

        XCTAssertEqual(result.moods.map(\.label), ["Bingeable", "Campy", "Cozy"])
    }

    /// The ordering rule in isolation: confidence still dominates, and the label only settles ties.
    func testConfidenceOutranksTheLabelTiebreak() throws {
        let raw = vote(primary: "Drama",
                       moods: [("Wholesome", 0.9), ("Bingeable", 0.6), ("Campy", 0.6)])
        let result = try XCTUnwrap(classifier.classify(rawVotes: [raw], title: title()))

        XCTAssertEqual(result.moods.map(\.label), ["Wholesome", "Bingeable", "Campy"])
    }

    /// Same inputs presented in a different ORDER must produce the same output. Order-independence is what
    /// makes a re-run reproducible, and it is the property the unstable sort quietly gave up.
    func testTheInputOrderOfTiedLabelsDoesNotChangeTheOutput() throws {
        let tied = ["Wholesome", "Campy", "Tearjerker", "Bingeable", "Cozy", "Slow-burn"]
        let forward = vote(primary: "Drama", moods: tied.map { ($0, 0.8) })
        let reversed = vote(primary: "Drama", moods: tied.reversed().map { ($0, 0.8) })

        let a = try XCTUnwrap(classifier.classify(rawVotes: [forward], title: title()))
        let b = try XCTUnwrap(classifier.classify(rawVotes: [reversed], title: title()))

        XCTAssertEqual(a.moods.map(\.label), b.moods.map(\.label))
    }

    /// The primary genre had the same defect one level down: `tally` is a Dictionary, and two genres with
    /// equal votes AND equal rarity fell all the way through to hash order — the rarity prior, which exists
    /// to break ties, cannot break a tie between two genres that share a weight.
    ///
    /// The taxonomy has five such pairs and four are usable; all four are checked, because a single pair is a coin flip against
    /// the old code — the hash seed is fixed WITHIN a process, so one pair would have passed on the broken
    /// version about half the time. Asserting the specific winner (not merely "stable") across all five
    /// leaves the old behaviour about one chance in sixteen of slipping through.
    func testFullyTiedPrimaryGenresResolveByName() throws {
        // first = the expected winner, i.e. the alphabetically first of the pair.
        let pairs = [("Family", "Fantasy"), ("Documentary", "History"),
                     ("Music", "War"), ("Adventure", "Romance")]
        let classifier = TaxonomyClassifier(llm: NoLLMStub(), samples: 4)

        for (winner, other) in pairs {
            let votes = [vote(primary: winner), vote(primary: other),
                         vote(primary: other), vote(primary: winner)]
            XCTAssertEqual(classifier.classify(rawVotes: votes, title: title())?.primaryGenre, winner,
                           "\(winner)/\(other)")
            XCTAssertEqual(classifier.classify(rawVotes: votes.reversed(), title: title())?.primaryGenre,
                           winner, "\(winner)/\(other) reversed")
        }
    }

    /// Ties are not contrived — averaging across passes manufactures them. Two labels that appear in
    /// different subsets of three passes land on the same mean, and that is the common case on real votes.
    func testTiesFromVoteAveragingResolveByLabel() throws {
        // Each of these appears in exactly two of the three passes, so all four average to 0.6.
        let passes = [
            vote(primary: "Drama", moods: [("Wholesome", 0.9), ("Campy", 0.9)]),
            vote(primary: "Drama", moods: [("Wholesome", 0.9), ("Cozy", 0.9)]),
            vote(primary: "Drama", moods: [("Campy", 0.9), ("Cozy", 0.9), ("Bingeable", 0.9)]),
        ]
        let result = try XCTUnwrap(
            TaxonomyClassifier(llm: NoLLMStub(), samples: 3).classify(rawVotes: passes, title: title()))

        XCTAssertEqual(result.moods.map(\.label), ["Campy", "Cozy", "Wholesome"])
        XCTAssertEqual(Set(result.moods.map(\.confidence)).count, 1, "all three must be tied for this to test the tiebreak")
    }
}

/// `classify(rawVotes:)` never calls the LLM — the votes are already collected — so the client only has to
/// exist. Failing loudly rather than returning "" keeps an accidental live path from passing silently.
private struct NoLLMStub: LLMClient {
    func complete(_ request: LLMRequest) async throws -> String {
        XCTFail("classify(rawVotes:) must not call the LLM")
        return ""
    }
}
