import XCTest
@testable import DenDataset

/// `DatasetMeta` and `ClassifyCheckpoint` were in the CLI target, which the tests cannot import — and that
/// is not incidental. It is why the media-key bug in this checkpoint survived long enough to cost 940 TV
/// series: the two types that WERE moved into the library got tests, and the one that wasn't did not.
final class ManifestTests: XCTestCase {

    private func meta(metadataFile: String? = nil, embedderRuntime: String? = nil) -> DatasetMeta {
        DatasetMeta(datasetVersion: "v1", taxonomyVersion: "t02", embeddingModel: "bge-m3", dims: 1024,
                    count: 2, quantization: "int8-symmetric-x127", labelsFile: "labels-t02.json",
                    vectorsFile: "vectors-bge-m3.bin", labelsGzFile: "labels-t02.json.gz",
                    labelsSha256: "aa", labelsBytes: 1, vectorsSha256: "bb", vectorsBytes: 2,
                    builtAt: "2026-01-01T00:00:00Z", lastModifiedHttp: "Thu, 01 Jan 2026 00:00:00 GMT",
                    metadataFile: metadataFile, embedderRuntime: embedderRuntime)
    }

    /// The guarantee `ownedKeys` rests on. If a field is ever added to `DatasetMeta` without a matching
    /// `CodingKeys` case, it is absent from the encoded JSON *and* absent from `ownedKeys` — which is
    /// precisely the combination that makes `ManifestMerge` inherit the previous run's value for it.
    func testEveryEncodedKeyIsAnOwnedKey() throws {
        let full = DatasetMeta(datasetVersion: "v1", taxonomyVersion: "t02", embeddingModel: "bge-m3",
                               dims: 1024, count: 2, quantization: "q", labelsFile: "l", vectorsFile: "v",
                               labelsGzFile: "g", labelsSha256: "a", labelsBytes: 1, vectorsSha256: "b",
                               vectorsBytes: 2, builtAt: "t", lastModifiedHttp: "h",
                               metadataFile: "m", metadataSha256: "ms", metadataBytes: 3,
                               embedderRuntime: "den-embed/3.0.0", embedderMaxTokens: 512)
        let encoded = try JSONSerialization.jsonObject(with: try JSONEncoder().encode(full))
        let keys = Set(try XCTUnwrap(encoded as? [String: Any]).keys)

        XCTAssertEqual(keys, DatasetMeta.ownedKeys,
                       "a field is encoded but not owned (or vice versa) — ManifestMerge would mishandle it")
    }

    /// The end-to-end shape of the merge, through the real `ownedKeys` rather than a hand-copied literal.
    func testAManifestRewriteKeepsForeignKeysAndDropsItsOwnOmittedOnes() throws {
        let existing = try JSONSerialization.data(withJSONObject: [
            "facetsFile": "facets.bin",                    // no producer in this repo — must survive
            "premiseVectorsFile": "vectors-premise.bin",
            "metadataFile": "metadata-OLD.json",           // owned, and the new meta omits it
            "embedderRuntime": "den-embed/2.9.0",
        ])
        let fresh = try JSONEncoder().encode(meta())

        let merged = try ManifestMerge.merge(new: fresh, existing: existing, owned: DatasetMeta.ownedKeys)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertEqual(result["facetsFile"] as? String, "facets.bin")
        XCTAssertEqual(result["premiseVectorsFile"] as? String, "vectors-premise.bin")
        XCTAssertNil(result["metadataFile"])
        XCTAssertNil(result["embedderRuntime"])
    }

    /// `namingSidecar` is a hand-written 20-argument copy, and it is the ONE place the CodingKeys
    /// compile-time guarantee does not reach: a future field with a defaulted init parameter compiles and
    /// is silently dropped here — the facets/premise loss class through a new door.
    ///
    /// So this compares the whole encoded manifest rather than spot-checking a few fields. Verified by
    /// mutation: the five-field version passed while `namingSidecar` zeroed labelsSha256, labelsBytes,
    /// vectorsSha256 and vectorsBytes.
    func testNamingASidecarChangesTheSidecarFieldsAndNothingElse() throws {
        let before = meta(embedderRuntime: "den-embed/3.0.0")
        let after = before.namingSidecar(file: "metadata-v1.json", sha256: "cc", bytes: 9)

        XCTAssertEqual(after.metadataFile, "metadata-v1.json")
        XCTAssertEqual(after.metadataSha256, "cc")
        XCTAssertEqual(after.metadataBytes, 9)

        func fieldsOtherThanTheSidecar(_ m: DatasetMeta) throws -> [String: String] {
            let encoded = try JSONSerialization.jsonObject(with: try JSONEncoder().encode(m))
            let dict = try XCTUnwrap(encoded as? [String: Any])
            return dict
                .filter { !["metadataFile", "metadataSha256", "metadataBytes"].contains($0.key) }
                .mapValues { "\($0)" }
        }
        XCTAssertEqual(try fieldsOtherThanTheSidecar(after), try fieldsOtherThanTheSidecar(before),
                       "namingSidecar altered or dropped a field it does not own")
    }
}

/// TMDB's movie and TV id namespaces OVERLAP, and this checkpoint keyed on a bare Int — so `assemble`
/// skipped any TV title whose id had already been classified as a movie. All 940 colliding TV series in
/// the shipped corpus were lost that way, after their enrichment had already been paid for.
final class ClassifyCheckpointTests: XCTestCase {

    func testTheSameIdInBothMediaAreDistinctEntries() {
        var ck = ClassifyCheckpoint()
        ck.done.insert(ClassifyCheckpoint.key("movie", 95))

        XCTAssertTrue(ck.done.contains(ClassifyCheckpoint.key("movie", 95)))
        XCTAssertFalse(ck.done.contains(ClassifyCheckpoint.key("tv", 95)),
                       "tv 95 is Buffy; movie 95 is Armageddon. Conflating them cost 940 series.")
    }

    /// A legacy bare-Int checkpoint must LOAD (resetting it re-classifies and re-embeds the whole out-dir)
    /// but must not be trusted as-is — nothing in it records which media each id belonged to.
    func testALegacyIntCheckpointLoadsAndAsksToBeRebuilt() throws {
        let legacy = Data(#"{"done":[603,95,27205],"totals":{"noPrimary":4,"missingVotes":1}}"#.utf8)
        let ck = try JSONDecoder().decode(ClassifyCheckpoint.self, from: legacy)

        XCTAssertTrue(ck.needsMigration)
        XCTAssertTrue(ck.done.isEmpty, "the legacy ids are unusable as keys; assemble rebuilds from the store")
        XCTAssertEqual(ck.totals.noPrimary, 4, "the counters still carry over")
    }

    func testAnAlreadyKeyedCheckpointIsNotMigratedAgain() throws {
        let keyed = Data(#"{"done":["movie:603","tv:95"],"totals":{"noPrimary":0,"missingVotes":0}}"#.utf8)
        let ck = try JSONDecoder().decode(ClassifyCheckpoint.self, from: keyed)

        XCTAssertFalse(ck.needsMigration)
        XCTAssertEqual(ck.done, ["movie:603", "tv:95"])
    }

    func testAnEmptyCheckpointIsNotMistakenForALegacyOne() throws {
        let empty = Data(#"{"done":[],"totals":{"noPrimary":0,"missingVotes":0}}"#.utf8)
        XCTAssertFalse(try JSONDecoder().decode(ClassifyCheckpoint.self, from: empty).needsMigration)
    }

    /// `needsMigration` is a load-time signal, not state. Persisting it would make every later run redo the
    /// rebuild — and, once the legacy file is gone, rebuild from a store it no longer matches.
    func testNeedsMigrationNeverReachesDisk() throws {
        var ck = ClassifyCheckpoint()
        ck.needsMigration = true
        ck.done = ["movie:1"]

        let encoded = try JSONSerialization.jsonObject(with: try JSONEncoder().encode(ck))
        let keys = Set(try XCTUnwrap(encoded as? [String: Any]).keys)

        XCTAssertEqual(keys, ["done", "totals"])
        XCTAssertFalse(try JSONDecoder().decode(ClassifyCheckpoint.self,
                                                from: try JSONEncoder().encode(ck)).needsMigration)
    }

    /// Matches `EnrichCheckpoint.Totals`: a checkpoint written before a counter existed must still load,
    /// or adding one makes assemble refuse every checkpoint on disk.
    func testTotalsSurviveAFieldItDoesNotKnow() throws {
        let ck = try JSONDecoder().decode(ClassifyCheckpoint.self, from: Data(
            #"{"done":["movie:1"],"totals":{"noPrimary":7}}"#.utf8))
        XCTAssertEqual(ck.totals.noPrimary, 7)
        XCTAssertEqual(ck.totals.missingVotes, 0)
    }
}

extension ClassifyCheckpointTests {
    /// The rebuild that follows a legacy load needs something to check itself against. Without the count,
    /// an absent or truncated labels store rebuilds to nothing and `assemble` silently re-classifies and
    /// re-embeds the whole out-dir — the exact reset the loud-checkpoint guard refuses to perform.
    func testALegacyCheckpointCarriesItsCountForTheRebuildToCheck() throws {
        let legacy = Data(#"{"done":[603,95,27205],"totals":{"noPrimary":0,"missingVotes":0}}"#.utf8)
        let ck = try JSONDecoder().decode(ClassifyCheckpoint.self, from: legacy)

        XCTAssertEqual(ck.legacyCount, 3)
        XCTAssertTrue(ck.needsMigration)
    }

    func testLegacyCountNeverReachesDisk() throws {
        var ck = ClassifyCheckpoint()
        ck.legacyCount = 37_533
        let keys = Set(try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try JSONEncoder().encode(ck)) as? [String: Any]).keys)
        XCTAssertEqual(keys, ["done", "totals"])
    }
}

/// The sidecar's row order feeds `metadataSha256`, which the app folds into its sync key — so an unstable
/// order costs every device a ~4.6 MB re-download of a file that did not change.
final class SidecarOrderTests: XCTestCase {
    private func row(_ id: Int, _ media: String) -> PosterMeta {
        PosterMeta(tmdbId: id, mediaType: media, title: "t", posterPath: nil, year: nil)
    }

    /// Calls the PRODUCTION comparator. The previous version of this test declared its own copy and so
    /// tested Swift's tuple `<` — mutation confirmed it stayed green while the real sort was reverted.
    func testTheOrderIsTotalAcrossCollidingIds() {
        let rows = [row(95, "tv"), row(95, "movie"), row(12, "movie")]
        let expected = ["12:movie", "95:movie", "95:tv"]

        for _ in 0..<50 {
            let got = SidecarOrder.sorted(rows.shuffled()).map { "\($0.tmdbId):\($0.mediaType)" }
            XCTAssertEqual(got, expected, "the same rows must always encode to the same bytes")
        }
    }

    func testConfidenceInTheIdStillDominates() {
        XCTAssertTrue(SidecarOrder.before(row(12, "tv"), row(95, "movie")))
    }
}

/// Whether a run may append to an existing store. Disabling this decision wholesale used to leave the
/// entire suite green, because it lived inline in the CLI target the tests cannot import.
final class EmbedderGateTests: XCTestCase {
    private func identity(epoch: Int = 1, maxTokens: Int = 512) -> DenEmbedClient.Identity {
        DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: epoch,
                                runtime: "den-embed/3.1.0", maxTokens: maxTokens)
    }

    func testAFreshOutDirRecordsTheIdentityAndProceeds() {
        XCTAssertEqual(EmbedderGate.decide(previous: nil, now: identity(), storeHasRows: false), .firstUse)
    }

    func testTheSameEmbedderMayAppend() {
        XCTAssertEqual(EmbedderGate.decide(previous: identity(), now: identity(), storeHasRows: true),
                       .matches)
    }

    /// The failure this whole mechanism exists for: two generations of vectors in one corpus, with every
    /// field either side used to compare (bge-m3, 1024) identical.
    func testADifferentEmbedderIsRefused() {
        let decision = EmbedderGate.decide(previous: identity(epoch: 1), now: identity(epoch: 2),
                                           storeHasRows: true)
        guard case .mismatch = decision else { return XCTFail("expected a mismatch, got \(decision)") }
    }

    /// Rows with no recorded identity must NOT adopt the current service. Doing so writes a guess down as
    /// a fact, and the guard then passes forever on the one corpus that actually has the problem — the
    /// shipped 37.5k-title store, built by the Python service.
    func testAnExistingStoreWithNoRecordedIdentityIsRefused() {
        XCTAssertEqual(EmbedderGate.decide(previous: nil, now: identity(), storeHasRows: true),
                       .unknownProvenance)
    }

    /// A release that does not move vectors must not invalidate a corpus — that is why the comparison
    /// ignores the build string.
    func testANewBuildOfTheSameEpochMayStillAppend() {
        let old = DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: 1,
                                          runtime: "den-embed/3.1.0", maxTokens: 512)
        let new = DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: 1,
                                          runtime: "den-embed/3.9.9", maxTokens: 512)
        XCTAssertEqual(EmbedderGate.decide(previous: old, now: new, storeHasRows: true), .matches)
    }
}
