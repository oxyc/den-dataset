import XCTest
@testable import DenDataset

/// The artifact FORMAT is the only coupling to the app, so lock it: the labels JSON encodes with sorted keys
/// and round-trips, and the vectors blob is `DENVEC02`, a little-endian count and dim, a u64 key per row,
/// then the int8 rows. (The smoke test additionally reads the REAL binary the tool wrote — this asserts the
/// contract shape.)
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

    func testVectorBlobLayoutIsMagicCountDimKeysRows() throws {
        // The REAL writer, not a copy of it. This file used to hold its own transcription of the tool's
        // `vectorsBlob`, and asserted the format against that — so it went on passing after the writer
        // moved, having verified only that the copy still agreed with itself.
        let keys: [UInt64] = [
            VectorBlob.key(mediaType: "movie", tmdbId: 603),
            VectorBlob.key(mediaType: "movie", tmdbId: 155),
            VectorBlob.key(mediaType: "tv", tmdbId: 155),
        ]
        let vectors: [[Int8]] = [[127, -127, 0, 42], [-1, 2, -3, 4], [0, 0, 0, 0]]
        let blob = try VectorBlob.encode(keys: keys, vectors: vectors)

        XCTAssertEqual(blob.prefix(8), Data("DENVEC02".utf8), "the magic leads, so a v1 blob cannot pass")
        XCTAssertEqual(blob.count, 16 + keys.count * 8 + vectors.count * 4)
        let decoded = try VectorBlob.decode(blob)
        XCTAssertEqual(decoded.count, 3)
        XCTAssertEqual(decoded.dim, 4)
        XCTAssertEqual(decoded.keys, keys, "each row is named by its title, in row order")

        // Payload round-trips: each row is `dim` signed bytes, after the key column.
        var offset = decoded.rowsBase
        for expected in vectors {
            XCTAssertEqual(blob.subdata(in: offset..<offset + 4).map { Int8(bitPattern: $0) }, expected)
            offset += 4
        }
    }
}
