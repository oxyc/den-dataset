import XCTest
@testable import DenDataset

/// The two failures these cover both shipped a corpus that looked perfectly healthy: right vector count,
/// right dimensions, clean hashes, and every title holding someone else's vector — or a manifest that
/// silently stopped naming two thirds of the files it had just published.
final class StoreIntegrityTests: XCTestCase {

    private func labelLine(_ id: Int) -> String {
        let record = IndexRecord(tmdbId: id, mediaType: "movie", primaryGenre: "Drama",
                                 subgenres: [], moods: [], source: .llm)
        return String(data: try! JSONEncoder().encode(record), encoding: .utf8)!
    }

    private func vectorLine(_ id: Int) -> String {
        String(data: try! JSONEncoder().encode(VectorRow(tmdbId: id, v: [1, 2, 3])), encoding: .utf8)!
    }

    // MARK: - alignedPrefix

    func testAlignedStoresKeepEveryLine() {
        let labels = [1, 2, 3].map(labelLine)
        let vectors = [1, 2, 3].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 3)
    }

    func testALabelWrittenWithoutItsVectorIsDropped() {
        // The plain crash: `flush` wrote the label line and died before the vector line.
        let labels = [1, 2, 3].map(labelLine)
        let vectors = [1, 2].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    /// The failure that counting alone could not see: a kill mid-`write` leaves a truncated final line,
    /// so both files hold the same NUMBER of lines while the last vector belongs to no title. Truncating
    /// to `min(count, count)` kept that pair and let it ship.
    func testEqualLineCountsWithATornFinalLineAreNotAligned() {
        let labels = [1, 2, 3].map(labelLine)
        var vectors = [1, 2].map(vectorLine)
        vectors.append(String(vectorLine(3).prefix(12)))   // torn mid-write: still one line, not JSON
        XCTAssertEqual(labels.count, vectors.count, "the two files look equal-length — that is the trap")
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    /// The consequence that makes this a corpus-wide bug rather than a one-line one: once the stores are
    /// off by one, EVERY later title carries its neighbour's vector, and the counts still match.
    func testAShiftedStoreStopsAtTheShift() {
        let labels = [10, 20, 30, 40].map(labelLine)
        let vectors = [10, 20, 40, 50].map(vectorLine)   // 30's vector never landed
        XCTAssertEqual(labels.count, vectors.count)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    func testAnUnparseableLabelLineStopsThePrefix() {
        let labels = [labelLine(1), "{not json", labelLine(3)]
        let vectors = [1, 2, 3].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 1)
    }

    func testEmptyStoresAreVacuouslyAligned() {
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: [], vectors: []), 0)
    }

    // MARK: - firstMisalignment (finalize's last gate)

    func testFirstMisalignmentIsNilWhenEveryLinePairsUp() {
        let records = [1, 2, 3].map {
            IndexRecord(tmdbId: $0, mediaType: "movie", primaryGenre: "Drama",
                        subgenres: [], moods: [], source: .llm)
        }
        let rows = [1, 2, 3].map { VectorRow(tmdbId: $0, v: [0]) }
        XCTAssertNil(StoreIntegrity.firstMisalignment(records: records, rows: rows))
    }

    func testFirstMisalignmentFindsTheShift() {
        let records = [1, 2, 3].map {
            IndexRecord(tmdbId: $0, mediaType: "movie", primaryGenre: "Drama",
                        subgenres: [], moods: [], source: .llm)
        }
        let rows = [1, 99, 3].map { VectorRow(tmdbId: $0, v: [0]) }
        XCTAssertEqual(StoreIntegrity.firstMisalignment(records: records, rows: rows), 1)
    }

    // MARK: - ManifestMerge

    /// The twelve keys the shipped manifest actually carries, and which a `finalize` or `metadata` re-run
    /// used to delete — taking premise search and facets down with them, silently, on both sides.
    private static let unmodelledKeys = [
        "premiseEmbeddingModel", "premiseDims", "premiseCount",
        "premiseLabelsFile", "premiseLabelsSha256", "premiseLabelsBytes",
        "premiseVectorsFile", "premiseVectorsSha256", "premiseVectorsBytes",
        "facetsFile", "facetsSha256", "facetsBytes",
    ]

    func testKeysTheStructDoesNotModelSurviveARewrite() throws {
        var old: [String: Any] = ["datasetVersion": "old", "count": 1]
        for (i, key) in Self.unmodelledKeys.enumerated() { old[key] = "value-\(i)" }
        let existing = try JSONSerialization.data(withJSONObject: old)
        let fresh = try JSONSerialization.data(withJSONObject: ["datasetVersion": "new", "count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        for (i, key) in Self.unmodelledKeys.enumerated() {
            XCTAssertEqual(result[key] as? String, "value-\(i)", "\(key) was dropped")
        }
    }

    func testTheStructWinsOnKeysItOwns() throws {
        let existing = try JSONSerialization.data(withJSONObject: ["count": 1, "facetsFile": "facets.bin"])
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertEqual(result["count"] as? Int, 2, "a stale value must not survive the merge")
        XCTAssertEqual(result["facetsFile"] as? String, "facets.bin")
    }

    func testAFirstEverWriteHasNothingToPreserve() throws {
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])
        let merged = try ManifestMerge.merge(new: fresh, existing: nil)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])
        XCTAssertEqual(result.keys.sorted(), ["count"])
    }

    func testAnUnreadableExistingManifestDoesNotBlockTheWrite() throws {
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])
        let merged = try ManifestMerge.merge(new: fresh, existing: Data("{corrupt".utf8))
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])
        XCTAssertEqual(result["count"] as? Int, 2)
    }

    /// Preserved values keep their JSON TYPE. `premiseDims` and the `*Bytes` keys are numbers, and
    /// stringifying them would let a manifest through that den-atlas cannot decode.
    func testPreservedValuesKeepTheirType() throws {
        let existing = try JSONSerialization.data(
            withJSONObject: ["premiseDims": 1024, "premiseCount": 37533, "facetsBytes": 900_123])
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertEqual(result["premiseDims"] as? Int, 1024)
        XCTAssertEqual(result["premiseCount"] as? Int, 37533)
        XCTAssertEqual(result["facetsBytes"] as? Int, 900_123)
    }
}
