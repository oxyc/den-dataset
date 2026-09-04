import XCTest
@testable import DenDataset

/// The two failures these cover both shipped a corpus that looked perfectly healthy: right vector count,
/// right dimensions, clean hashes, and every title holding someone else's vector — or a manifest that
/// silently stopped naming two thirds of the files it had just published.
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

    // MARK: - firstMisalignment (finalize's last gate)

    func testFirstMisalignmentIsNilWhenEveryLinePairsUp() {
        let records = [1, 2, 3].map {
            IndexRecord(tmdbId: $0, mediaType: "movie", primaryGenre: "Drama",
                        subgenres: [], moods: [], source: .llm)
        }
        let rows = [1, 2, 3].map { VectorRow(tmdbId: $0, v: [0]) }
        XCTAssertNil(StoreIntegrity.firstMisalignment(records: records, rows: rows))
    }

    func testFirstMisalignmentFindsTheShift() {
        let records = [1, 2, 3].map {
            IndexRecord(tmdbId: $0, mediaType: "movie", primaryGenre: "Drama",
                        subgenres: [], moods: [], source: .llm)
        }
        let rows = [1, 99, 3].map { VectorRow(tmdbId: $0, v: [0]) }
        XCTAssertEqual(StoreIntegrity.firstMisalignment(records: records, rows: rows), 1)
    }

    // MARK: - ManifestMerge

    /// The twelve keys the shipped manifest actually carries, and which a `finalize` or `metadata` re-run
    /// used to delete — taking premise search and facets down with them, silently, on both sides.
    /// The real thing, not a hand-copy. A 20-key literal here would drift from the struct silently, and
    /// these tests would then pass against an `owned` set the production write never uses.
    private let owned = DatasetMeta.ownedKeys

    private static let unmodelledKeys = [
        "premiseEmbeddingModel", "premiseDims", "premiseCount",
        "premiseLabelsFile", "premiseLabelsSha256", "premiseLabelsBytes",
        "premiseVectorsFile", "premiseVectorsSha256", "premiseVectorsBytes",
        "facetsFile", "facetsSha256", "facetsBytes",
    ]

    func testKeysTheStructDoesNotModelSurviveARewrite() throws {
        var old: [String: Any] = ["datasetVersion": "old", "count": 1]
        for (i, key) in Self.unmodelledKeys.enumerated() { old[key] = "value-\(i)" }
        let existing = try JSONSerialization.data(withJSONObject: old)
        let fresh = try JSONSerialization.data(withJSONObject: ["datasetVersion": "new", "count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing, owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        for (i, key) in Self.unmodelledKeys.enumerated() {
            XCTAssertEqual(result[key] as? String, "value-\(i)", "\(key) was dropped")
        }
    }

    func testTheStructWinsOnKeysItOwns() throws {
        let existing = try JSONSerialization.data(withJSONObject: ["count": 1, "facetsFile": "facets.bin"])
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing, owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertEqual(result["count"] as? Int, 2, "a stale value must not survive the merge")
        XCTAssertEqual(result["facetsFile"] as? String, "facets.bin")
    }

    /// The half of the merge that matters as much as preserving: a key the struct OWNS but leaves out is
    /// saying "there is no sidecar", and that has to win too.
    ///
    /// `JSONEncoder` omits nil Optionals, so an omitted `metadataFile` was indistinguishable from an
    /// unmodelled key and inherited the previous run's value — a manifest naming a sidecar built for an
    /// older datasetVersion and swearing to its old sha256. Both consumers hard-verify that sha, so the
    /// refresh does not degrade, it STOPS: den-atlas keeps serving the old dataset and the 4-hourly timer
    /// fails silently forever. Worse than the loss the merge was written to prevent.
    func testAnOwnedKeyTheStructOmittedIsNotInherited() throws {
        let existing = try JSONSerialization.data(withJSONObject: [
            "count": 1,
            "metadataFile": "metadata-OLDVERSION.json",
            "metadataSha256": "STALE-SHA",
            "embedderRuntime": "den-embed/2.9.0",
            "facetsFile": "facets.bin",              // NOT owned — must survive
        ])
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing, owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertNil(result["metadataFile"], "a sidecar the struct no longer names must not come back")
        XCTAssertNil(result["metadataSha256"])
        XCTAssertNil(result["embedderRuntime"], "an embedder identity must never be inherited")
        XCTAssertEqual(result["facetsFile"] as? String, "facets.bin", "unmodelled keys still survive")
        XCTAssertEqual(result["count"] as? Int, 2)
    }

    func testAFirstEverWriteHasNothingToPreserve() throws {
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])
        let merged = try ManifestMerge.merge(new: fresh, existing: nil, owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])
        XCTAssertEqual(result.keys.sorted(), ["count"])
    }

    func testAnUnreadableExistingManifestDoesNotBlockTheWrite() throws {
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])
        let merged = try ManifestMerge.merge(new: fresh, existing: Data("{corrupt".utf8), owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])
        XCTAssertEqual(result["count"] as? Int, 2)
    }

    /// Preserved values keep their JSON TYPE. `premiseDims` and the `*Bytes` keys are numbers, and
    /// stringifying them would let a manifest through that den-atlas cannot decode.
    func testPreservedValuesKeepTheirType() throws {
        let existing = try JSONSerialization.data(
            withJSONObject: ["premiseDims": 1024, "premiseCount": 37533, "facetsBytes": 900_123])
        let fresh = try JSONSerialization.data(withJSONObject: ["count": 2])

        let merged = try ManifestMerge.merge(new: fresh, existing: existing, owned: owned)
        let result = try XCTUnwrap(try JSONSerialization.jsonObject(with: merged) as? [String: Any])

        XCTAssertEqual(result["premiseDims"] as? Int, 1024)
        XCTAssertEqual(result["premiseCount"] as? Int, 37533)
        XCTAssertEqual(result["facetsBytes"] as? Int, 900_123)
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

    func testIdentityReadsEveryFieldIncludingTheSnakeCasedOne() throws {
        let body = Data(#"{"status":"ok","model":"bge-m3","dims":1024,"runtime":"den-embed/3.0.0","max_tokens":512}"#.utf8)
        let identity = try DenEmbedClient.Identity(healthJSON: body)

        XCTAssertEqual(identity.model, "bge-m3")
        XCTAssertEqual(identity.dims, 1024)
        XCTAssertEqual(identity.runtime, "den-embed/3.0.0")
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
        XCTAssertEqual(identity.maxTokens, 0)
        XCTAssertNotEqual(identity, try DenEmbedClient.Identity(healthJSON: Data(
            #"{"model":"bge-m3","dims":1024,"runtime":"den-embed/3.0.0","max_tokens":512}"#.utf8)))
    }

    /// The identity is written to disk and compared on the next run, so it has to survive a round-trip
    /// through JSON exactly — a lossy field would make the mixed-embedder guard fire on every run.
    func testIdentityRoundTripsThroughItsStoredForm() throws {
        let original = DenEmbedClient.Identity(model: "bge-m3", dims: 1024,
                                               runtime: "den-embed/3.0.0", maxTokens: 512)
        let restored = try JSONDecoder().decode(DenEmbedClient.Identity.self,
                                                from: try JSONEncoder().encode(original))
        XCTAssertEqual(original, restored)
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

/// Two fixes that shipped with no test at all — verified: reverting either left the whole suite green.
final class SilentEmptyDecodeTests: XCTestCase {

    /// `PagedList.results` defaulting to [] turned any unexpected body — an auth error, a schema change —
    /// into a valid EMPTY page. `worklist`'s collect loop stops after page 1, and the delta pass reports
    /// "0 new titles" rather than failing. Silently, and every day.
    func testAResponseWithoutResultsIsAnError() throws {
        let decoder = JSONDecoder()

        // A real page decodes.
        XCTAssertNoThrow(try decoder.decode(
            TMDBClient.PagedList.self,
            from: Data(#"{"page":1,"total_pages":3,"results":[]}"#.utf8)))

        // An error body is NOT a page with no titles in it.
        XCTAssertThrowsError(try decoder.decode(
            TMDBClient.PagedList.self,
            from: Data(#"{"success":false,"status_message":"Invalid API key"}"#.utf8)),
            "an auth failure must not read as an empty page")
    }

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
