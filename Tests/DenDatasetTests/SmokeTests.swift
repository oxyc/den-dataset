import XCTest
@testable import DenDataset

/// End-to-end producer smoke: a fixture index store → `finalize`, all offline. No TMDB, no network, no 60k
/// rebuild. Asserts the shipped artifacts (labels JSON, int8 vector blob, dataset.meta.json, gzipped labels)
/// exist and are well-formed. Drives the REAL CLI binary so the tool's own code path is what runs.
///
/// The store is written directly rather than produced by an embed run: `embed-corpus` needs a live
/// den-embed, so seeding its output is the only way to exercise the artifact half with no network.
final class SmokeTests: XCTestCase {
    func testFinalizeProducesArtifacts() throws {
        let fm = FileManager.default
        let outDir = fm.temporaryDirectory.appendingPathComponent("den-dataset-smoke-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: outDir) }

        // The append-only index store: one labels line and one vectors line per title, positionally paired.
        let records = [
            IndexRecord(tmdbId: 603, mediaType: "movie", primaryGenre: "Science Fiction",
                        subgenres: [LabelConfidence(label: "Sci-Fi Action", confidence: 0.9)],
                        moods: [LabelConfidence(label: "Mind-bending", confidence: 0.8)],
                        source: .llm, animated: false),
            IndexRecord(tmdbId: 155, mediaType: "movie", primaryGenre: "Action",
                        subgenres: [LabelConfidence(label: "Crime Thriller", confidence: 0.85)],
                        moods: [LabelConfidence(label: "Dark & Gritty", confidence: 0.7)],
                        source: .llm, animated: false),
            IndexRecord(tmdbId: 27205, mediaType: "movie", primaryGenre: "Science Fiction",
                        subgenres: [LabelConfidence(label: "Heist", confidence: 0.7)],
                        moods: [LabelConfidence(label: "Mind-bending", confidence: 0.75)],
                        source: .llm, animated: false),
        ]
        // 384 dims, the length `--embedding-version e02` implies — finalize refuses a blob whose length
        // contradicts its label.
        let rows = records.enumerated().map { index, record in
            VectorRow(tmdbId: record.tmdbId, v: (0..<384).map { ($0 + index) % 127 - 63 })
        }
        let encoder = JSONEncoder()
        try write(records.map { String(decoding: try encoder.encode($0), as: UTF8.self) }.joined(separator: "\n") + "\n",
                  to: outDir.appendingPathComponent("index/labels.jsonl"))
        try write(rows.map { String(decoding: try encoder.encode($0), as: UTF8.self) }.joined(separator: "\n") + "\n",
                  to: outDir.appendingPathComponent("index/vectors.jsonl"))

        // Label the artifact to match the 384-dim rows (e02); finalize's dim/label guard rejects a bge-m3
        // label on 384-dim vectors.
        try run(["finalize", "--out-dir", outDir.path, "--embedding-version", "e02"])

        // labels-<taxonomyVersion>.json parses and has the 3 records.
        let version = Taxonomy.current.version
        let labelsPath = outDir.appendingPathComponent("labels-\(version).json")
        let labelsData = try Data(contentsOf: labelsPath)
        let artifact = try JSONDecoder().decode(LabelsArtifact.self, from: labelsData)
        XCTAssertEqual(artifact.taxonomyVersion, version)
        XCTAssertEqual(artifact.count, 3)
        XCTAssertEqual(artifact.records.count, 3)
        let inception = artifact.records.first { $0.tmdbId == 27205 }
        XCTAssertNotNil(inception)
        XCTAssert(inception!.subgenres.contains { $0.label == "Heist" }, "the store's labels reach the artifact")

        // vectors-e02.bin header count matches (name + dim both 384/e02, consistent per the guard).
        let vectorsPath = outDir.appendingPathComponent("vectors-e02.bin")
        XCTAssertTrue(FileManager.default.fileExists(atPath: vectorsPath.path), "the artifact is named for the label")
        let vectorsData = try Data(contentsOf: vectorsPath)
        let blob = try VectorBlob.decode(vectorsData)
        XCTAssertEqual(blob.count, 3, "blob header count == records")
        XCTAssertEqual(blob.dim, 384, "the dimension the e02 label implies")
        XCTAssertEqual(vectorsData.count, blob.rowsBase + blob.count * blob.dim,
                       "header + keys + count*dim int8 rows")
        // And each row is named by the title it belongs to, so the labels artifact is no longer the only
        // record of which vector is whose.
        XCTAssertEqual(blob.keys.sorted(),
                       artifact.records.map { VectorBlob.key(mediaType: $0.mediaType, tmdbId: $0.tmdbId) }
                           .sorted(),
                       "the blob names the same titles the labels artifact does")

        // dataset.meta.json — the manifest the Rust server reads.
        let metaPath = outDir.appendingPathComponent("dataset.meta.json")
        let meta = try JSONSerialization.jsonObject(with: Data(contentsOf: metaPath)) as! [String: Any]
        let expectedKeys: Set<String> = [
            "datasetVersion", "taxonomyVersion", "embeddingModel", "dims", "count", "quantization",
            "labelsFile", "vectorsFile", "labelsGzFile", "labelsSha256", "labelsBytes",
            "vectorsSha256", "vectorsBytes", "builtAt", "lastModifiedHttp",
        ]
        XCTAssertEqual(Set(meta.keys), expectedKeys, "meta has exactly the server's keys")
        XCTAssertEqual(meta["embeddingModel"] as? String, "e02")
        XCTAssertEqual(meta["quantization"] as? String, "int8-symmetric-x127")
        XCTAssertEqual(meta["dims"] as? Int, 384)
        XCTAssertEqual(meta["count"] as? Int, 3)
        XCTAssertEqual(meta["labelsFile"] as? String, "labels-\(version).json")
        XCTAssertEqual(meta["vectorsFile"] as? String, "vectors-e02.bin")
        let datasetVersion = meta["datasetVersion"] as? String ?? ""
        XCTAssertEqual(datasetVersion.count, 12, "datasetVersion is 12 hex chars")
        XCTAssert(datasetVersion.allSatisfy { $0.isHexDigit }, "datasetVersion is hex")

        // gzipped labels blob exists.
        XCTAssertTrue(FileManager.default.fileExists(atPath: outDir.appendingPathComponent("labels-\(version).json.gz").path))
    }

    // MARK: - helpers

    private func write(_ text: String, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try text.data(using: .utf8)!.write(to: url)
    }

    private func run(_ arguments: [String]) throws {
        let process = Process()
        process.executableURL = Self.binaryURL
        process.arguments = arguments
        let stderr = Pipe()
        process.standardError = stderr
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            let err = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            XCTFail("taxonomy-backfill \(arguments.joined(separator: " ")) exited \(process.terminationStatus): \(err)")
        }
    }

    /// The `taxonomy-backfill` executable sits in the same products directory as the xctest bundle.
    static let binaryURL: URL = {
        for bundle in Bundle.allBundles where bundle.bundlePath.hasSuffix(".xctest") {
            return bundle.bundleURL.deletingLastPathComponent().appendingPathComponent("taxonomy-backfill")
        }
        // Linux / plain builds: the test runner sits beside the executable.
        return Bundle.main.bundleURL.appendingPathComponent("taxonomy-backfill")
    }()
}
