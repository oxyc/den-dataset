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

/// Opus-confirmed world-knowledge labels are the only thing that lets a Cult/Art House/Epic label survive
/// the 100-vote gate on the obscure tail, so losing one silently discards paid adjudication.
final class WorldKnowledgeTests: XCTestCase {

    /// A legacy bare-Int entry applies to BOTH media. Reading them as movies — which an earlier version
    /// did, claiming movies were what produced them — was measurably wrong: of the 2,950 entries in the
    /// shipped file, 87 are series-only (Berlin Alexanderplatz, The Prisoner, Heimat) and every one is
    /// under the vote floor, so filing them under `movie:` drops their labels entirely.
    func testALegacyIdAppliesToBothMedia() {
        let expanded = WorldKnowledge.expand(["43189": ["Art House", "Epic"]])

        XCTAssertEqual(expanded["tv:43189"], ["Art House", "Epic"], "a series-only id must not be lost")
        XCTAssertEqual(expanded["movie:43189"], ["Art House", "Epic"])
    }

    func testAKeyedEntryIsTakenAsWritten() {
        let expanded = WorldKnowledge.expand(["tv:95": ["Cult"]])

        XCTAssertEqual(expanded["tv:95"], ["Cult"])
        XCTAssertNil(expanded["movie:95"], "a keyed entry says which medium; it must not spread")
    }

    /// A hand-edit in progress. The previous version required EVERY key to be qualified and then fell
    /// through to an Int-keyed decode that cannot parse "tv:95", so a mixed file returned nothing at all —
    /// silently stripping every confirmed label in it.
    func testAMixedFileKeepsBothForms() {
        let expanded = WorldKnowledge.expand(["tv:95": ["Cult"], "603": ["Epic"]])

        XCTAssertEqual(expanded["tv:95"], ["Cult"])
        XCTAssertEqual(expanded["movie:603"], ["Epic"])
        XCTAssertEqual(expanded["tv:603"], ["Epic"])
        XCTAssertEqual(WorldKnowledge.unkeyedCount(["tv:95": ["Cult"], "603": ["Epic"]]), 1)
    }

    func testAnUnparseableKeyIsDroppedRatherThanCrashing() {
        XCTAssertTrue(WorldKnowledge.expand(["not-an-id": ["Cult"]]).isEmpty)
    }

    /// `"007"` is not id 7 and `"+95"` is not id 95 — `Int()` accepts both, so a typo'd key used to attach
    /// an override to a DIFFERENT title. Unusable keys are dropped, and reported rather than dropped
    /// silently, since a lost override is a label nobody notices going missing.
    func testAKeyThatIsNotExactlyAnIdIsRejectedAndReported() {
        let raw = ["007": ["Cult"], "+95": ["Epic"], " 12": ["Cult"], "95.0": ["Cult"]]

        XCTAssertTrue(WorldKnowledge.expand(raw).isEmpty, "none of these are ids")
        XCTAssertEqual(WorldKnowledge.unkeyedCount(raw), 0, "an unusable key is not a countable bare id")
        XCTAssertEqual(WorldKnowledge.unusableKeys(raw), [" 12", "+95", "007", "95.0"])
    }

    /// A keyed entry states which medium was adjudicated; a legacy one cannot. When a file holds both for
    /// the same id, the specific one has to win — and it must win EVERY run. Assigning in Dictionary order
    /// meant the legacy entry took it roughly one run in six.
    func testAKeyedEntryBeatsALegacyOneForTheSameId() {
        for _ in 0..<20 {
            let expanded = WorldKnowledge.expand(["95": ["Epic"], "tv:95": ["Cult"]])
            XCTAssertEqual(expanded["tv:95"], ["Cult"], "the explicit entry must win, every time")
            XCTAssertEqual(expanded["movie:95"], ["Epic"], "the legacy one still fills the other medium")
        }
    }

    func testAnEmptyFileIsEmptyNotAnError() {
        XCTAssertTrue(WorldKnowledge.expand([:]).isEmpty)
        XCTAssertEqual(WorldKnowledge.unkeyedCount([:]), 0)
    }
}
