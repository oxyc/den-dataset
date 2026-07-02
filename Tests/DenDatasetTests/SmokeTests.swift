import XCTest
@testable import DenDataset

/// End-to-end producer smoke: a fixture enriched batch + one fixture vote pass (with a bare-string label to
/// exercise the lenient decode) → `assemble` → `finalize`, all offline. No TMDB, no network, no 60k rebuild.
/// Asserts the shipped artifacts (labels JSON, int8 vector blob, dataset.meta.json, gzipped labels) exist and
/// are well-formed. Drives the REAL CLI binary so the tool's own code path is what runs.
final class SmokeTests: XCTestCase {
    func testAssembleThenFinalizeProducesArtifacts() throws {
        let fm = FileManager.default
        let outDir = fm.temporaryDirectory.appendingPathComponent("den-dataset-smoke-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: outDir) }

        // Fixture enriched batch (scratch input Haiku would read) — 3 titles, one carrying the heist keyword
        // (id 10051) so the grounding bonus fires.
        let enriched = """
        [
          {"tmdbId":603,"mediaType":"movie","title":"The Matrix","year":1999,
           "overview":"A hacker learns reality is a simulation and joins a rebellion against machines.",
           "genreIDs":[28,878],"genres":["Action","Science Fiction"],
           "keywordIDs":[83,9882],"keywords":["saviour","dystopia"],
           "originCountry":["US"],"originalLanguage":"en","voteCount":24000},
          {"tmdbId":155,"mediaType":"movie","title":"The Dark Knight","year":2008,
           "overview":"Batman faces the Joker, an agent of chaos threatening Gotham City.",
           "genreIDs":[28,80,18],"genres":["Action","Crime","Drama"],
           "keywordIDs":[9715],"keywords":["superhero"],
           "originCountry":["US"],"originalLanguage":"en","voteCount":30000},
          {"tmdbId":27205,"mediaType":"movie","title":"Inception","year":2010,
           "overview":"A thief who steals corporate secrets through dream-sharing pulls off one last heist.",
           "genreIDs":[28,878,12],"genres":["Action","Science Fiction","Adventure"],
           "keywordIDs":[10051],"keywords":["heist"],
           "originCountry":["US"],"originalLanguage":"en","voteCount":34000}
        ]
        """
        try write(enriched, to: outDir.appendingPathComponent("enriched/batch-1.json"))

        // Fixture vote pass — one Haiku subagent's labels. Inception's subgenre is a BARE STRING ("Heist")
        // rather than {label,confidence}, exercising the lenient decode (defaults to 0.7).
        let votes = """
        [
          {"tmdbId":603,"primary_genre":"Science Fiction",
           "subgenres":[{"label":"Sci-Fi Action","confidence":0.9}],
           "moods":[{"label":"Mind-bending","confidence":0.8}]},
          {"tmdbId":155,"primary_genre":"Action",
           "subgenres":[{"label":"Crime Thriller","confidence":0.85}],
           "moods":["Dark & Gritty"]},
          {"tmdbId":27205,"primary_genre":"Science Fiction",
           "subgenres":["Heist"],
           "moods":[{"label":"Mind-bending","confidence":0.75}]}
        ]
        """
        try write(votes, to: outDir.appendingPathComponent("votes/batch-1-pass1.json"))

        // Drive the real CLI.
        try run(["assemble", "--batch-id", "1", "--out-dir", outDir.path])
        try run(["finalize", "--out-dir", outDir.path])

        // labels-t01.json parses and has the 3 records.
        let labelsPath = outDir.appendingPathComponent("labels-t01.json")
        let labelsData = try Data(contentsOf: labelsPath)
        let artifact = try JSONDecoder().decode(LabelsArtifact.self, from: labelsData)
        XCTAssertEqual(artifact.taxonomyVersion, "t01")
        XCTAssertEqual(artifact.count, 3)
        XCTAssertEqual(artifact.records.count, 3)
        // Lenient bare-string label survived as a real thematic label (with the heist grounding bonus).
        let inception = artifact.records.first { $0.tmdbId == 27205 }
        XCTAssertNotNil(inception)
        XCTAssert(inception!.subgenres.contains { $0.label == "Heist" }, "bare-string 'Heist' decoded + kept")

        // vectors-e02.bin header count matches (the e01→e02 rename).
        let vectorsPath = outDir.appendingPathComponent("vectors-e02.bin")
        XCTAssertTrue(FileManager.default.fileExists(atPath: vectorsPath.path), "shipped name is vectors-e02.bin")
        let vectorsData = try Data(contentsOf: vectorsPath)
        let count = vectorsData.subdata(in: 0..<4).withUnsafeBytes { Int32(littleEndian: $0.load(as: Int32.self)) }
        let dim = vectorsData.subdata(in: 4..<8).withUnsafeBytes { Int32(littleEndian: $0.load(as: Int32.self)) }
        XCTAssertEqual(count, 3, "blob header count == records")
        XCTAssertEqual(dim, 384, "default embedding dimension")
        XCTAssertEqual(vectorsData.count, 8 + Int(count) * Int(dim), "header + count*dim int8 rows")

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
        XCTAssertEqual(meta["labelsFile"] as? String, "labels-t01.json")
        XCTAssertEqual(meta["vectorsFile"] as? String, "vectors-e02.bin")
        let datasetVersion = meta["datasetVersion"] as? String ?? ""
        XCTAssertEqual(datasetVersion.count, 12, "datasetVersion is 12 hex chars")
        XCTAssert(datasetVersion.allSatisfy { $0.isHexDigit }, "datasetVersion is hex")

        // gzipped labels blob exists.
        XCTAssertTrue(FileManager.default.fileExists(atPath: outDir.appendingPathComponent("labels-t01.json.gz").path))
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
