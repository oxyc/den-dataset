import XCTest
@testable import DenDataset

/// The vector blob names its own rows.
///
/// The failure these are about: v1 was `[i32 count][i32 dim][rows…]`, so which title row *n* belonged to
/// lived only in the record order of a separate `labels-*.json`. Two files with the same count and
/// different orders produced an index that loaded cleanly and returned someone else's neighbours. Nothing
/// in the bytes could tell them apart, which is why every test below is about the KEYS rather than the
/// numbers — the numbers were never the part that could go wrong undetected.
final class VectorBlobTests: XCTestCase {
    private let rows: [[Int8]] = [[1, 2, -3], [4, -5, 6], [-7, 8, 9]]
    private let keys: [UInt64] = [
        VectorBlob.key(mediaType: "movie", tmdbId: 238),
        VectorBlob.key(mediaType: "tv", tmdbId: 238),
        VectorBlob.key(mediaType: "movie", tmdbId: 1),
    ]

    func testAKeyNamesBothTheMediaTypeAndTheId() {
        // The same tmdbId as a film and as a series: 1,097 real ids are both, so a bare id is not a key.
        XCTAssertNotEqual(VectorBlob.key(mediaType: "movie", tmdbId: 238),
                          VectorBlob.key(mediaType: "tv", tmdbId: 238))
        XCTAssertEqual(VectorBlob.key(mediaType: "movie", tmdbId: 238), 238)
        XCTAssertEqual(VectorBlob.key(mediaType: "tv", tmdbId: 238), (1 << 32) | 238)
        // And it is the key `build_store.py` writes into the store's `keys` section: `(media << 32) | id`.
        for key in keys {
            XCTAssertEqual(VectorBlob.key(mediaType: VectorBlob.mediaType(of: key),
                                          tmdbId: VectorBlob.tmdbId(of: key)), key)
        }
    }

    func testTheBlobCarriesItsRowKeysInRowOrder() throws {
        let decoded = try VectorBlob.decode(try VectorBlob.encode(keys: keys, vectors: rows))
        XCTAssertEqual(decoded.count, 3)
        XCTAssertEqual(decoded.dim, 3)
        XCTAssertEqual(decoded.keys, keys, "row n must be readable as belonging to key n, from the file "
            + "alone — that is the whole point of the format bump")
        XCTAssertEqual(decoded.rowsBase, 16 + 3 * 8)
    }

    func testTheRowsSurviveTheRoundTripExactly() throws {
        let data = try VectorBlob.encode(keys: keys, vectors: rows)
        let decoded = try VectorBlob.decode(data)
        for (i, row) in rows.enumerated() {
            let start = decoded.rowsBase + i * decoded.dim
            let got = data[start..<(start + decoded.dim)].map { Int8(bitPattern: $0) }
            XCTAssertEqual(got, row, "row \(i)")
        }
    }

    func testAV1BlobIsRefusedRatherThanMisread() {
        // Exactly what `vectorsBlob` used to write: [i32 count][i32 dim][rows…], no magic.
        var v1 = Data()
        for value in [Int32(3), Int32(3)] {
            var le = value.littleEndian
            withUnsafeBytes(of: &le) { v1.append(contentsOf: $0) }
        }
        for row in rows { v1.append(contentsOf: row.map { UInt8(bitPattern: $0) }) }

        XCTAssertThrowsError(try VectorBlob.decode(v1)) { error in
            XCTAssertTrue("\(error)".contains("DENVEC02"),
                          "the refusal must name the format, not just fail a length check: \(error)")
            XCTAssertTrue("\(error)".contains("migrate_vector_blob"),
                          "and say how to convert it: \(error)")
        }
    }

    func testAV2BlobCannotBeMistakenForAV1OneEither() throws {
        // The other direction. A v1 reader takes the first eight bytes as count and dim; on a v2 blob
        // those are the magic, which reads as absurd numbers no length check can accept.
        let data = try VectorBlob.encode(keys: keys, vectors: rows)
        let count = data.withUnsafeBytes { UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self)) }
        let dim = data.withUnsafeBytes {
            UInt32(littleEndian: $0.loadUnaligned(fromByteOffset: 4, as: UInt32.self))
        }
        XCTAssertNotEqual(Int(count), rows.count)
        XCTAssertNotEqual(Int(dim), rows[0].count)
        XCTAssertNotEqual(data.count, 8 + Int(count) * Int(dim))
    }

    func testATruncatedBlobIsRefused() throws {
        let data = try VectorBlob.encode(keys: keys, vectors: rows)
        XCTAssertThrowsError(try VectorBlob.decode(data.dropLast(4)))
        XCTAssertThrowsError(try VectorBlob.decode(data.prefix(8)))
    }

    func testAKeyColumnThatRepeatsATitleIsRefused() {
        let repeated = [keys[0], keys[0], keys[2]]
        XCTAssertThrowsError(try VectorBlob.encode(keys: repeated, vectors: rows)) { error in
            XCTAssertTrue("\(error)".contains("repeats"), "\(error)")
        }
    }

    func testAKeyPerRowIsRequired() {
        XCTAssertThrowsError(try VectorBlob.encode(keys: Array(keys.dropLast()), vectors: rows))
    }
}
