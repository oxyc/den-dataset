import XCTest
@testable import DenDataset

/// The artifact FORMAT is the only coupling to the app, so lock it: the labels JSON encodes with sorted keys
/// and round-trips, and the vectors blob is `[int32 count][int32 dim]` little-endian followed by the int8
/// rows. (The smoke test additionally reads the REAL binary the tool wrote — this asserts the contract shape.)
final class ConformanceTests: XCTestCase {
    func testLabelsArtifactEncodesSortedAndRoundTrips() throws {
        let records = [
            IndexRecord(tmdbId: 603, mediaType: "movie", primaryGenre: "Science Fiction",
                        subgenres: [LabelConfidence(label: "Sci-Fi Action", confidence: 0.9)],
                        moods: [LabelConfidence(label: "Mind-bending", confidence: 0.8)],
                        source: .llm, animated: false),
            IndexRecord(tmdbId: 155, mediaType: "movie", primaryGenre: "Action",
                        subgenres: [LabelConfidence(label: "Crime Thriller", confidence: 0.85)],
                        moods: [LabelConfidence(label: "Dark & Gritty", confidence: 0.7)],
                        source: .llm, animated: false),
        ]
        let artifact = LabelsArtifact(taxonomyVersion: "t01", records: records)

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let blob = try encoder.encode(artifact)

        // Sorted keys → deterministic byte output (top-level keys appear in alphabetical order).
        let json = String(data: blob, encoding: .utf8)!
        let countIdx = json.range(of: "\"count\"")!.lowerBound
        let recordsIdx = json.range(of: "\"records\"")!.lowerBound
        let taxIdx = json.range(of: "\"taxonomyVersion\"")!.lowerBound
        XCTAssert(countIdx < recordsIdx && recordsIdx < taxIdx, "sortedKeys orders count < records < taxonomyVersion")

        let decoded = try JSONDecoder().decode(LabelsArtifact.self, from: blob)
        XCTAssertEqual(decoded, artifact)
        XCTAssertEqual(decoded.count, 2)
    }

    func testVectorBlobHeaderIsLittleEndianAndRoundTrips() throws {
        let vectors: [[Int8]] = [
            [127, -127, 0, 42],
            [-1, 2, -3, 4],
            [0, 0, 0, 0],
        ]
        let blob = Self.vectorsBlob(vectors)

        // Header: [int32 count][int32 dim] little-endian.
        XCTAssertEqual(blob.count, 8 + vectors.count * 4)
        let count = blob.subdata(in: 0..<4).withUnsafeBytes { Int32(littleEndian: $0.load(as: Int32.self)) }
        let dim = blob.subdata(in: 4..<8).withUnsafeBytes { Int32(littleEndian: $0.load(as: Int32.self)) }
        XCTAssertEqual(count, 3)
        XCTAssertEqual(dim, 4)

        // Payload round-trips: each row is `dim` signed bytes.
        var offset = 8
        for expected in vectors {
            let row = blob.subdata(in: offset..<offset + 4).map { Int8(bitPattern: $0) }
            XCTAssertEqual(row, expected)
            offset += 4
        }
    }

    /// Mirrors the tool's `vectorsBlob` byte-for-byte (the tool's copy lives in the executable target). The
    /// smoke test cross-checks that the real binary the CLI produces has this exact header.
    static func vectorsBlob(_ vectors: [[Int8]]) -> Data {
        var data = Data()
        let dim = vectors.first?.count ?? 0
        var count = Int32(vectors.count).littleEndian
        var dimension = Int32(dim).littleEndian
        withUnsafeBytes(of: &count) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &dimension) { data.append(contentsOf: $0) }
        for row in vectors { data.append(contentsOf: row.map { UInt8(bitPattern: $0) }) }
        return data
    }
}
