import Foundation

/// Text → dense vector seam (DT-C). The real tool can plug an embedding API behind this; the default
/// `HashingEmbedder` is offline + deterministic so the pipeline runs (and tests) with no external model.
public protocol Embedder: Sendable {
    var dimension: Int { get }
    func embed(_ text: String) async throws -> [Float]
}

/// A deterministic signed feature-hashing embedder (FNV-1a → fixed dimension, L2-normalized). A genuine
/// bag-of-words embedding — coarse but real; swap an API embedder behind `Embedder` for production quality.
public struct HashingEmbedder: Embedder {
    public let dimension: Int
    public init(dimension: Int = 384) { self.dimension = max(1, dimension) }

    public func embed(_ text: String) async throws -> [Float] {
        var vector = [Float](repeating: 0, count: dimension)
        for token in text.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }) {
            let hash = Self.fnv1a(String(token))
            let index = Int(hash % UInt64(dimension))
            let sign: Float = (hash >> 33) & 1 == 0 ? 1 : -1   // signed hashing cancels collisions
            vector[index] += sign
        }
        let norm = sqrt(vector.reduce(Float(0)) { $0 + $1 * $1 })
        if norm > 0 { for i in vector.indices { vector[i] /= norm } }
        return vector
    }

    static func fnv1a(_ string: String) -> UInt64 {
        var hash: UInt64 = 14695981039346656037
        for byte in string.utf8 { hash = (hash ^ UInt64(byte)) &* 1099511628211 }
        return hash
    }
}

/// int8 quantization for the shipped on-device ANN (DT-D). Symmetric scale by 127 — vectors are
/// L2-normalized so components are in [-1, 1]; the dequant scale is constant, nothing else is stored.
public enum Quantizer {
    public static let scale: Float = 127

    public static func int8(_ vector: [Float]) -> [Int8] {
        vector.map { Int8(max(-127, min(127, ($0 * scale).rounded()))) }
    }
    public static func dequantize(_ quantized: [Int8]) -> [Float] {
        quantized.map { Float($0) / scale }
    }
}
