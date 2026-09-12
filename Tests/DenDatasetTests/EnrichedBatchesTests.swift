import XCTest
@testable import DenDataset

/// The batch id a delta run writes to. This exists because the rule "no checkpoint means first run, start at
/// 1" destroyed data: `out-t02` held 153 enriched batches and no enrich checkpoint, so a delta restarted at 1
/// and overwrote batch-1 and batch-2 — 640 records replaced by 235 — while `votes/batch-1-pass1.json` still
/// held the previous titles' votes, ready to label the new titles with the old ones' labels.
final class EnrichedBatchesTests: XCTestCase {
    func testHighestIDIsTheFloorForTheNextBatch() {
        let names = ["batch-1.json", "batch-2.json", "batch-153.json", "batch-17.json"]
        XCTAssertEqual(EnrichedBatches.highestID(inFileNames: names), 153,
                       "numeric max, not lexical — 'batch-17' must not beat 'batch-153'")
    }

    func testEmptyDirectoryIsGenuinelyAFirstRun() {
        XCTAssertEqual(EnrichedBatches.highestID(inFileNames: []), 0)
    }

    /// Anything that isn't an enriched batch must not raise the floor, or one stray file silently skips ids.
    func testIgnoresFilesThatAreNotBatches() {
        let names = ["enrich-checkpoint.json", "batch-.json", "batch-abc.json",
                     "batch-9.json.bak", "labels-t02.json", "batch-4.json"]
        XCTAssertEqual(EnrichedBatches.highestID(inFileNames: names), 4)
    }

    /// The real directory path, since that is what `enrich` calls.
    func testReadsARealDirectory() throws {
        let dir = NSTemporaryDirectory() + "eb-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: dir) }
        for name in ["batch-1.json", "batch-42.json", "notes.txt"] {
            XCTAssertTrue(FileManager.default.createFile(atPath: dir + "/" + name, contents: Data("[]".utf8)))
        }
        XCTAssertEqual(EnrichedBatches.highestID(inDirectory: dir), 42)
        XCTAssertEqual(EnrichedBatches.highestID(inDirectory: dir + "/does-not-exist"), 0,
                       "a missing directory is zero, not a crash — enrich may run before it exists")
    }
}
