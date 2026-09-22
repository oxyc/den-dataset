import XCTest
@testable import DenDataset

/// The order enriched batches are read in. Where a new batch's number comes from is
/// `pipeline/enrich_test.py`'s question now.
final class EnrichedBatchesTests: XCTestCase {
    /// A key may appear in more than one batch — 1,855 of 59,218 do, and 505 disagree about `hasWikiPlot` —
    /// so the order batches are read in decides whether those titles get their plot or an empty string.
    /// `sorted()` is lexicographic and put `batch-99` after `batch-177`, which made the winner depend on how
    /// many digits an id happened to have. Nothing pinned the order before this test.
    func testBatchesOrderNumericallyNotLexically() {
        let names = ["batch-99.json", "batch-177.json", "batch-18.json", "batch-2.json", "batch-102.json"]
        XCTAssertEqual(EnrichedBatches.orderedNames(inFileNames: names),
                       ["batch-2.json", "batch-18.json", "batch-99.json",
                        "batch-102.json", "batch-177.json"])
        XCTAssertEqual(names.sorted().first, "batch-102.json",
                       "the lexical order this replaces — batch-102 first, batch-99 last")
    }

    /// Files that are not batches must not appear in the reading order.
    func testOrderingIgnoresFilesThatAreNotBatches() {
        let names = ["enrich-checkpoint.json", "batch-abc.json", "batch-9.json.bak", "batch-3.json"]
        XCTAssertEqual(EnrichedBatches.orderedNames(inFileNames: names), ["batch-3.json"])
    }
}
