import XCTest
@testable import DenDataset

/// The vectors blob is `DENVEC02`, a little-endian count and dim, a u64 key per row, then the int8 rows. The
/// labels artifact's bytes are `pipeline/finalize.py`'s now, and `pipeline/finalize_test.py` holds them.
final class ConformanceTests: XCTestCase {
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
