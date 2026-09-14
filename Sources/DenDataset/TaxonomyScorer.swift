import Foundation

/// One hand-labeled golden title (DT-B fixture row). Ground truth for measuring classifier quality.
public struct GoldenTitle: Codable, Sendable, Equatable {
    public let tmdbId: Int
    public let mediaType: String
    public let title: String
    public let year: Int?
    public let primaryGenre: String
    public let subgenres: [String]
    public let themes: [String]
    public let moods: [String]

    /// The multi-label set (subgenres + themes + moods) scored by F1; primary genre is scored separately.
    public var labels: Set<String> { Set(subgenres + themes + moods) }

    /// `"movie:278"` — the only safe identity for a title. TMDB's movie and series id spaces overlap, so a
    /// bare id conflates movie 95 (Armageddon) with series 95 (Buffy).
    public var key: String { "\(mediaType):\(tmdbId)" }
}

/// The versioned golden set (`Tests/.../Fixtures/taxonomy/golden.json`).
public struct GoldenSet: Codable, Sendable {
    public let taxonomyVersion: String
    public let titles: [GoldenTitle]

    /// Keyed `"<mediaType>:<tmdbId>"`, not by a bare id. Keying by id alone both conflated a movie with the
    /// series sharing its number AND trapped at runtime, because `Dictionary(uniqueKeysWithValues:)` has a
    /// precondition on duplicates — so one colliding golden row would crash the scorer rather than mis-score.
    public var labelsByKey: [String: Set<String>] {
        Dictionary(titles.map { ($0.key, $0.labels) }, uniquingKeysWith: { first, _ in first })
    }

    public var primaryByKey: [String: String] {
        Dictionary(titles.map { ($0.key, $0.primaryGenre) }, uniquingKeysWith: { first, _ in first })
    }
}

/// Per-label confusion counts → precision / recall / F1.
public struct LabelScore: Sendable, Equatable {
    public let label: String
    public let truePositives: Int
    public let falsePositives: Int
    public let falseNegatives: Int
    public var precision: Double {
        let denom = truePositives + falsePositives
        return denom == 0 ? 0 : Double(truePositives) / Double(denom)
    }
    public var recall: Double {
        let denom = truePositives + falseNegatives
        return denom == 0 ? 0 : Double(truePositives) / Double(denom)
    }
    public var f1: Double {
        let (precision, recall) = (precision, recall)
        return (precision + recall) == 0 ? 0 : 2 * precision * recall / (precision + recall)
    }
}

public struct F1Report: Sendable {
    public let perLabel: [String: LabelScore]
    public let macroF1: Double
    public let microF1: Double
}

/// Computes label F1 of a prediction against the golden set (DT-B AC2; reused by DT-C calibration + DT-G).
/// Multi-label: each (title, label) is a TP/FP/FN. Macro = unweighted mean of per-label F1; micro = global.
public enum TaxonomyScorer {
    /// Keys are `"<mediaType>:<tmdbId>"`. They were bare TMDB ids, which forced every caller to invent its own
    /// way of keeping a movie apart from the series with the same number — the scoring path did it by adding
    /// 10 billion to TV ids. One shared string key removes the need for that trick.
    public static func score(golden: [String: Set<String>], predicted: [String: Set<String>]) -> F1Report {
        var labels = Set<String>()
        golden.values.forEach { labels.formUnion($0) }
        predicted.values.forEach { labels.formUnion($0) }
        let ids = Set(golden.keys).union(predicted.keys)

        var perLabel: [String: LabelScore] = [:]
        var totalTP = 0, totalFP = 0, totalFN = 0
        for label in labels {
            var tp = 0, fp = 0, fn = 0
            for id in ids {
                let inGold = golden[id]?.contains(label) ?? false
                let inPred = predicted[id]?.contains(label) ?? false
                switch (inGold, inPred) {
                case (true, true): tp += 1
                case (false, true): fp += 1
                case (true, false): fn += 1
                case (false, false): break
                }
            }
            perLabel[label] = LabelScore(label: label, truePositives: tp, falsePositives: fp, falseNegatives: fn)
            totalTP += tp; totalFP += fp; totalFN += fn
        }
        let macroF1 = perLabel.isEmpty ? 0 : perLabel.values.map(\.f1).reduce(0, +) / Double(perLabel.count)
        let microPrecision = (totalTP + totalFP) == 0 ? 0 : Double(totalTP) / Double(totalTP + totalFP)
        let microRecall = (totalTP + totalFN) == 0 ? 0 : Double(totalTP) / Double(totalTP + totalFN)
        let microF1 = (microPrecision + microRecall) == 0 ? 0
            : 2 * microPrecision * microRecall / (microPrecision + microRecall)
        return F1Report(perLabel: perLabel, macroF1: macroF1, microF1: microF1)
    }

    /// Single-label primary-genre accuracy (scored apart from the multi-label F1).
    public static func primaryGenreAccuracy(golden: [String: String], predicted: [String: String]) -> Double {
        guard !golden.isEmpty else { return 0 }
        let correct = golden.keys.filter { golden[$0] == predicted[$0] }.count
        return Double(correct) / Double(golden.count)
    }
}
