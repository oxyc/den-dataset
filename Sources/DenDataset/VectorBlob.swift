import Foundation

/// The int8 vector blob — `vectors-bge-m3.bin`, `vectors-premise.bin`, and every experiment beside them.
///
///     0    8         magic "DENVEC02"
///     8    4         u32 count   (little-endian)
///     12   4         u32 dim     (little-endian)
///     16   8*count   u64 keys    (little-endian), ONE PER ROW, in row order
///     …    count*dim int8 rows, quantized int8-symmetric-x127 from L2-normalized floats
///
/// ## Why the keys are in the file
///
/// v1 was `[i32 count][i32 dim][rows…]` — the matrix and nothing else. Which title row *n* described was
/// recorded only in the order a separate `labels-*.json` happened to list its records. Nothing in the
/// `.bin` could detect a mismatch: regenerate the labels file with a different record order and every
/// vector silently moves onto the wrong title, in a file that loads cleanly and returns real numbers for
/// everything. That is why `labels-t02.json` and `labels-premise.json` had to keep being BUILT after they
/// stopped being PUBLISHED — not for their labels, which the corpus carries, but purely as an order
/// oracle. An oracle is not a check. It cannot fail, so it cannot catch anything.
///
/// The key is the one the store uses: `(media << 32) | tmdbId`, media 0 = movie, 1 = tv. Never a bare
/// tmdbId — 1,097 ids in this corpus are both a film and a series. Eight bytes a row is 380 KB on a 48 MB
/// file, and it turns a positional assumption into a join that can be asserted.
///
/// ## Why the magic, and why it is `DENVEC02`
///
/// Eight ASCII bytes with a trailing version number, matching `DENSTOR1` — the convention the store
/// already uses — so `head -c8` names the file and a future bump is one byte.
///
/// The discrimination against v1 is total in both directions. A v1 header is two small little-endian
/// ints, so its bytes 4..8 are the dim: `00 04 00 00` (1024) or `80 01 00 00` (384), never `45 43 30 32`.
/// Read the other way, a v2 file's first eight bytes are count = 1,448,232,772 and dim = 842,018,117,
/// which overflows every length check in this pipeline. So neither format can be misread AS the other —
/// but only a reader that looks for the magic refuses BY NAME, instead of dying on a length assert.
public enum VectorBlob {
    public static let magic = Data("DENVEC02".utf8)
    public static let headerBytes = 16
    public static let keyBytes = 8

    public struct Failure: Error, CustomStringConvertible {
        public let message: String
        public var description: String { message }
    }

    /// `(media << 32) | tmdbId` — the key the store, the blob and `build_store.py` all agree on.
    public static func key(mediaType: String, tmdbId: Int) -> UInt64 {
        (UInt64(mediaType == "movie" ? 0 : 1) << 32) | UInt64(UInt32(truncatingIfNeeded: tmdbId))
    }

    public static func mediaType(of key: UInt64) -> String { key >> 32 == 0 ? "movie" : "tv" }
    public static func tmdbId(of key: UInt64) -> Int { Int(key & 0xFFFF_FFFF) }

    /// The blob for `vectors`, each row named by the key at the same index.
    public static func encode(keys: [UInt64], vectors: [[Int8]]) throws -> Data {
        guard keys.count == vectors.count else {
            throw Failure(message: "\(keys.count) keys for \(vectors.count) vectors — every row must "
                + "name its title, and there is nothing to fall back on if one does not")
        }
        guard Set(keys).count == keys.count else {
            throw Failure(message: "the key column repeats a title — two rows claiming one title is not "
                + "a join any reader can resolve")
        }
        let dim = vectors.first?.count ?? 0
        var data = Data(capacity: headerBytes + keys.count * (keyBytes + dim))
        data.append(magic)
        for value in [UInt32(keys.count), UInt32(dim)] {
            var le = value.littleEndian
            withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
        }
        for key in keys {
            var le = key.littleEndian
            withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
        }
        for row in vectors { data.append(contentsOf: row.map { UInt8(bitPattern: $0) }) }
        return data
    }

    /// What a blob says about itself. `rowsBase` is the offset of row 0, so a caller reads rows straight
    /// out of the same `Data` rather than copying 48 MB.
    public struct Decoded {
        public let count: Int
        public let dim: Int
        public let keys: [UInt64]
        public let rowsBase: Int
    }

    public static func decode(_ data: Data) throws -> Decoded {
        guard data.count >= headerBytes, data.prefix(magic.count) == magic else {
            throw Failure(message: "not a DENVEC02 vector blob. A v1 blob carries no keys, so reading it "
                + "would rest on a labels file's record order — the exact failure this format removes. "
                + "Convert it with scripts/v2/migrate_vector_blob.py.")
        }
        let count = Int(data.withUnsafeBytes {
            UInt32(littleEndian: $0.loadUnaligned(fromByteOffset: magic.count, as: UInt32.self))
        })
        let dim = Int(data.withUnsafeBytes {
            UInt32(littleEndian: $0.loadUnaligned(fromByteOffset: magic.count + 4, as: UInt32.self))
        })
        let base = headerBytes + count * keyBytes
        guard dim > 0, data.count == base + count * dim else {
            throw Failure(message: "\(data.count) bytes for \(count)×\(dim) plus \(count) keys, "
                + "expected \(base + count * dim)")
        }
        let keys: [UInt64] = data.withUnsafeBytes { raw in
            (0..<count).map {
                UInt64(littleEndian: raw.loadUnaligned(fromByteOffset: headerBytes + $0 * keyBytes,
                                                       as: UInt64.self))
            }
        }
        guard Set(keys).count == count else {
            throw Failure(message: "the key column repeats a title — two rows claiming one title is not "
                + "a join any reader can resolve")
        }
        return Decoded(count: count, dim: dim, keys: keys, rowsBase: base)
    }
}
