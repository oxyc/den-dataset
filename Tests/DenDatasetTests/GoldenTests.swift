import XCTest
@testable import DenDataset

/// Golden determinism for the embedder + quantizer — the byte-parity guarantee with the app. The FNV-1a
/// signed-hashing embedder must be reproducible so a re-run of the producer yields the same shipped vectors.
final class GoldenTests: XCTestCase {
    func testEmbedderIsDeterministic() async throws {
        let embedder = HashingEmbedder()
        let text = "A crew pulls off an elaborate diamond heist in Monte Carlo"
        let a = try await embedder.embed(text)
        let b = try await embedder.embed(text)
        XCTAssertEqual(a, b, "same input must embed to the same vector")
        XCTAssertEqual(a.count, 384, "default dimension")
    }

    func testEmbedderIsL2Normalized() async throws {
        let v = try await HashingEmbedder().embed("crime thriller neo-noir detective")
        let norm = sqrt(v.reduce(Float(0)) { $0 + $1 * $1 })
        XCTAssertEqual(norm, 1.0, accuracy: 1e-5, "non-empty text is unit-normalized")
    }

    /// A single-token input is a one-hot ±1 at `fnv1a(token) % dim` — a golden tied directly to the algorithm
    /// (recomputed here from the same FNV-1a constants the embedder uses).
    func testSingleTokenIsGoldenOneHot() async throws {
        let dim = 384
        let embedder = HashingEmbedder(dimension: dim)
        let token = "zebra"
        let v = try await embedder.embed(token)

        let hash = HashingEmbedder.fnv1a(token)
        let index = Int(hash % UInt64(dim))
        let sign: Float = (hash >> 33) & 1 == 0 ? 1 : -1
        XCTAssertEqual(v[index], sign, "the one nonzero component is the signed hash bucket")
        XCTAssertEqual(v.filter { $0 != 0 }.count, 1, "single token → exactly one nonzero component")
    }

    func testQuantizerIsStableAndBounded() async throws {
        let v = try await HashingEmbedder().embed("space opera time travel dystopia")
        let q1 = Quantizer.int8(v)
        let q2 = Quantizer.int8(v)
        XCTAssertEqual(q1, q2, "quantization is deterministic")
        for value in q1 { XCTAssert(value >= -127 && value <= 127, "int8 stays in [-127, 127]") }
    }

    /// The one-hot ±1 component quantizes to ±127 (scale = 127), and dequantizing recovers ±1.
    func testOneHotQuantizesToScaleBound() async throws {
        let v = try await HashingEmbedder().embed("zebra")
        let q = Quantizer.int8(v)
        XCTAssertEqual(q.map(Int.init).filter { abs($0) == 127 }.count, 1)
        XCTAssertEqual(q.filter { $0 != 0 }.count, 1)
        let back = Quantizer.dequantize(q)
        XCTAssertEqual(back.map { abs($0) }.max()!, 1.0, accuracy: 1e-6)
    }
}
