import XCTest
@testable import DenDataset

/// The failure these cover shipped a corpus that looked perfectly healthy: right vector count, right
/// dimensions, clean hashes, and every title holding someone else's vector.
final class StoreIntegrityTests: XCTestCase {

    private func labelLine(_ id: Int) -> String {
        let record = IndexRecord(tmdbId: id, mediaType: "movie", primaryGenre: "Drama",
                                 subgenres: [], moods: [], source: .llm)
        return String(data: try! JSONEncoder().encode(record), encoding: .utf8)!
    }

    private func vectorLine(_ id: Int) -> String {
        String(data: try! JSONEncoder().encode(VectorRow(tmdbId: id, v: [1, 2, 3])), encoding: .utf8)!
    }

    // MARK: - alignedPrefix

    func testAlignedStoresKeepEveryLine() {
        let labels = [1, 2, 3].map(labelLine)
        let vectors = [1, 2, 3].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 3)
    }

    func testALabelWrittenWithoutItsVectorIsDropped() {
        // The plain crash: `flush` wrote the label line and died before the vector line.
        let labels = [1, 2, 3].map(labelLine)
        let vectors = [1, 2].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    /// The failure that counting alone could not see: a kill mid-`write` leaves a truncated final line,
    /// so both files hold the same NUMBER of lines while the last vector belongs to no title. Truncating
    /// to `min(count, count)` kept that pair and let it ship.
    func testEqualLineCountsWithATornFinalLineAreNotAligned() {
        let labels = [1, 2, 3].map(labelLine)
        var vectors = [1, 2].map(vectorLine)
        vectors.append(String(vectorLine(3).prefix(12)))   // torn mid-write: still one line, not JSON
        XCTAssertEqual(labels.count, vectors.count, "the two files look equal-length — that is the trap")
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    /// The consequence that makes this a corpus-wide bug rather than a one-line one: once the stores are
    /// off by one, EVERY later title carries its neighbour's vector, and the counts still match.
    func testAShiftedStoreStopsAtTheShift() {
        let labels = [10, 20, 30, 40].map(labelLine)
        let vectors = [10, 20, 40, 50].map(vectorLine)   // 30's vector never landed
        XCTAssertEqual(labels.count, vectors.count)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 2)
    }

    func testAnUnparseableLabelLineStopsThePrefix() {
        let labels = [labelLine(1), "{not json", labelLine(3)]
        let vectors = [1, 2, 3].map(vectorLine)
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors), 1)
    }

    func testEmptyStoresAreVacuouslyAligned() {
        XCTAssertEqual(StoreIntegrity.alignedPrefix(labels: [], vectors: []), 0)
    }
}

/// The repair rule and the embedder identity are both hard gates on the pipeline: one decides whether to
/// rewrite the append-only store, the other decides whether a run may append to it at all.
final class RepairAndIdentityTests: XCTestCase {

    // MARK: - When NOT to repair

    func testAnInterruptedWriteIsRepaired() {
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 100, vectorCount: 99, aligned: 99, maxDrop: 1000),
                       .truncate(keeping: 99))
    }

    func testAlignedStoresAreLeftAlone() {
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 100, vectorCount: 100, aligned: 100, maxDrop: 1000),
                       .nothingToDo)
    }

    /// One unparseable line in the middle of a 37.5k store is not a torn write, and truncating to it would
    /// delete the corpus — hours of den-embed time, replaced atomically, with nothing left to recover.
    func testADivergenceInTheMiddleIsRefusedNotTruncated() {
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 37_533, vectorCount: 37_533, aligned: 49,
                                             maxDrop: 1000),
                       .refuse(dropping: 37_484, divergesAtLine: 50))
    }

    /// The case that would erase the store outright: a field added to `IndexRecord` makes every existing
    /// line fail to decode, because synthesized `Decodable` requires every key.
    func testAStoreThatDecodesNowhereIsRefused() {
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 37_533, vectorCount: 37_533, aligned: 0,
                                             maxDrop: 1000),
                       .refuse(dropping: 37_533, divergesAtLine: 1))
    }

    func testTheBoundaryIsRepairedAndOneBeyondItIsNot() {
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 5000, vectorCount: 4000, aligned: 4000, maxDrop: 1000),
                       .truncate(keeping: 4000))
        XCTAssertEqual(StoreIntegrity.repair(labelCount: 5001, vectorCount: 4000, aligned: 4000, maxDrop: 1000),
                       .refuse(dropping: 1001, divergesAtLine: 4001))
    }

    // MARK: - Reading the service's identity

    func testIdentityReadsEveryFieldIncludingTheSnakeCasedOnes() throws {
        let body = Data(#"{"status":"ok","model":"bge-m3","dims":1024,"vector_epoch":1,"runtime":"den-embed/3.1.0","max_tokens":512}"#.utf8)
        let identity = try DenEmbedClient.Identity(healthJSON: body)

        XCTAssertEqual(identity.model, "bge-m3")
        XCTAssertEqual(identity.dims, 1024)
        XCTAssertEqual(identity.runtime, "den-embed/3.1.0")
        XCTAssertEqual(identity.vectorEpoch, 1)
        // The one field whose JSON name differs from its Swift name. A broken CodingKey yields 0 here,
        // which reads as "the service does not report a cap" and disables the truncation guard entirely.
        XCTAssertEqual(identity.maxTokens, 512)
    }

    /// A service too old to report `runtime` is a real answer, not an error — it is the Python generation,
    /// which is what built the corpus shipping today. It has to be distinguishable from the current one.
    func testAServicePredatingTheRuntimeFieldIsIdentifiedAsSuch() throws {
        let body = Data(#"{"status":"ok","model":"bge-m3","dims":1024}"#.utf8)
        let identity = try DenEmbedClient.Identity(healthJSON: body)

        XCTAssertEqual(identity.runtime, "pre-3.0.0")
        XCTAssertEqual(identity.vectorEpoch, 0, "no epoch means a generation that predates the field")
        XCTAssertEqual(identity.maxTokens, 0)
        XCTAssertNotEqual(identity, try DenEmbedClient.Identity(healthJSON: Data(
            #"{"model":"bge-m3","dims":1024,"vector_epoch":1,"runtime":"den-embed/3.1.0","max_tokens":512}"#.utf8)))
    }

    /// An identity file written before `vectorEpoch` existed must still LOAD. This type is persisted to
    /// index/embedder.json and read on every later run, so a required new key makes every out-dir on disk
    /// fail with keyNotFound — finalize refuses to ship, and recordEmbedder (which reads with `try?`) sees
    /// the file as absent and prescribes a literal that does not decode either.
    func testAnIdentityFileWrittenBeforeTheEpochExistedStillLoads() throws {
        for stored in [
            #"{"dims":1024,"maxTokens":512,"model":"bge-m3","runtime":"den-embed/3.0.0"}"#,
            // The exact literal recordEmbedder's error message tells the operator to write.
            #"{"model":"bge-m3","dims":1024,"runtime":"pre-3.0.0","maxTokens":0}"#,
        ] {
            let identity = try JSONDecoder().decode(DenEmbedClient.Identity.self, from: Data(stored.utf8))
            XCTAssertEqual(identity.vectorEpoch, 0, "absent means the pre-epoch generation, not an error")
            XCTAssertEqual(identity.dims, 1024)
        }
    }

    /// The identity is written to disk and compared on the next run, so every field has to survive the
    /// round-trip. Asserted on the ENCODED JSON, not on `==`: equality deliberately ignores `runtime`, so
    /// comparing values here would not notice `runtime` being lost — and `finalize` stamps it into the
    /// shipped manifest as `embedderRuntime`.
    func testIdentityRoundTripsThroughItsStoredForm() throws {
        let original = DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: 1,
                                               runtime: "den-embed/3.1.0", maxTokens: 512)
        let encoded = try JSONEncoder().encode(original)
        let restored = try JSONDecoder().decode(DenEmbedClient.Identity.self, from: encoded)
        XCTAssertEqual(original, restored)

        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(json["model"] as? String, "bge-m3")
        XCTAssertEqual(json["dims"] as? Int, 1024)
        XCTAssertEqual(json["vectorEpoch"] as? Int, 1)
        XCTAssertEqual(json["maxTokens"] as? Int, 512)
        XCTAssertEqual(json["runtime"] as? String, "den-embed/3.1.0",
                       "runtime is ignored by == but is what finalize stamps into the manifest")
    }
}

/// TMDB authenticates with a query PARAMETER, and `URLError`'s description embeds the failing URL — so
/// every timeout on a multi-hour enrich run wrote the real key into out/enrich-<media>.log.
final class RedactTests: XCTestCase {

    func testAnApiKeyInAFailingUrlIsRemoved() {
        let raw = """
        Error Domain=NSURLErrorDomain Code=-1001 "timed out" \
        NSErrorFailingURLStringKey=https://api.themoviedb.org/3/movie/603?api_key=abc123def456&language=en, \
        NSErrorFailingURLKey=https://api.themoviedb.org/3/movie/603?api_key=abc123def456
        """
        let clean = Redact.secrets(raw)

        XCTAssertFalse(clean.contains("abc123def456"), "the key survived: \(clean)")
        XCTAssertTrue(clean.contains("api_key=REDACTED"))
        // Everything an operator needs to diagnose it is still there.
        XCTAssertTrue(clean.contains("-1001"))
        XCTAssertTrue(clean.contains("api.themoviedb.org/3/movie/603"))
        XCTAssertTrue(clean.contains("language=en"), "the & terminated the match, as it must")
    }

    func testOtherCredentialParametersAreRemovedToo() {
        for param in ["access_token", "token", "password"] {
            let clean = Redact.secrets("POST /login?\(param)=s3cr3tvalue failed")
            XCTAssertFalse(clean.contains("s3cr3tvalue"), "\(param) survived")
        }
    }

    func testTextWithNoCredentialIsUnchanged() {
        let raw = "fetch-failure id=603 (HTTP 404)"
        XCTAssertEqual(Redact.secrets(raw), raw)
    }
}

/// A fix that shipped with no test at all — verified: reverting it left the whole suite green.
///
/// The `/discover` half of this class moved out with the worklist: the guard that an error body must not
/// decode as an empty page is now `lib/tmdb.py`'s, and `lib/tmdb_test.py` holds it.
final class SilentEmptyDecodeTests: XCTestCase {

    /// Redact has two passes: the query-parameter regex, and a verbatim sweep for known secret VALUES.
    /// The second exists for text where the key appears without its parameter name.
    func testAKnownSecretValueIsRemovedEvenWithoutItsParameterName() {
        setenv("TMDB_API_KEY", "verysecretkeyvalue123", 1)
        defer { unsetenv("TMDB_API_KEY") }

        let clean = Redact.secrets("auth failed for token verysecretkeyvalue123 (401)")

        XCTAssertFalse(clean.contains("verysecretkeyvalue123"), "the verbatim pass did not run: \(clean)")
        XCTAssertTrue(clean.contains("401"), "the diagnosis must survive")
    }

    /// A short value is not swept verbatim — matching a 3-character secret would redact ordinary words and
    /// destroy the diagnostic instead of protecting anything.
    func testAnImplausiblyShortSecretIsNotSweptVerbatim() {
        setenv("TMDB_API_KEY", "abc", 1)
        defer { unsetenv("TMDB_API_KEY") }

        XCTAssertEqual(Redact.secrets("abcdef is fine"), "abcdef is fine")
    }
}

/// Identity equality decides whether a run may append to a 37.5k-title corpus, so what it ignores matters
/// as much as what it compares.
final class EmbedderIdentityEqualityTests: XCTestCase {

    private func identity(epoch: Int = 1, runtime: String = "den-embed/3.1.0",
                          maxTokens: Int = 512) -> DenEmbedClient.Identity {
        DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: epoch,
                                runtime: runtime, maxTokens: maxTokens)
    }

    /// A release that changes nothing about the numbers must not invalidate a corpus. Comparing the build
    /// string would have demanded a full re-embed — hours of den-embed time — for a log-line fix.
    func testAReleaseThatDoesNotMoveVectorsIsTheSameEmbedder() {
        XCTAssertEqual(identity(runtime: "den-embed/3.1.0"), identity(runtime: "den-embed/3.4.2"))
    }

    /// ...and a release that DOES move them must be caught. That is the whole point: bge-m3/1024 is
    /// reported by every generation, including the ORT 1.22 -> 1.28 bump that shifted int8 output.
    func testABumpedEpochIsADifferentEmbedder() {
        XCTAssertNotEqual(identity(epoch: 1), identity(epoch: 2))
    }

    /// The token cap is part of the identity because truncation changes the vector for any document
    /// longer than it — the corpus shipping today was embedded with no cap at all.
    func testADifferentTokenCapIsADifferentEmbedder() {
        XCTAssertNotEqual(identity(maxTokens: 512), identity(maxTokens: 1024))
    }

    func testThePythonGenerationIsNotTheRustOne() {
        let python = DenEmbedClient.Identity(model: "bge-m3", dims: 1024, vectorEpoch: 0,
                                             runtime: "pre-3.0.0", maxTokens: 0)
        XCTAssertNotEqual(python, identity())
    }
}
