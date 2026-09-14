import XCTest
@testable import DenDataset

final class TMDBCacheTests: XCTestCase {
    private func scratch() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("tmdb-cache-tests-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    /// The whole point: a title fetched once is read back rather than re-fetched.
    func testRoundTrips() {
        let cache = TMDBCache(directory: scratch())
        let key = TMDBCache.key(path: "/movie/278", query: ["append_to_response": "keywords,credits"])
        XCTAssertNil(cache.read(key), "nothing cached yet")
        cache.write(key, Data(#"{"id":278}"#.utf8))
        XCTAssertEqual(cache.read(key), Data(#"{"id":278}"#.utf8))
    }

    /// A cache directory is exactly where a credential gets copied and forgotten, so the key must not carry
    /// one — not in the hash input, and therefore not in any filename.
    func testKeyIgnoresTheAPIKey() {
        let withKey = TMDBCache.key(path: "/movie/278", query: ["api_key": "s3cret", "language": "en"])
        let without = TMDBCache.key(path: "/movie/278", query: ["language": "en"])
        XCTAssertEqual(withKey, without)
        XCTAssertFalse(withKey.contains("s3cret"))
    }

    func testKeyIsOrderIndependentButPathSensitive() {
        XCTAssertEqual(TMDBCache.key(path: "/movie/1", query: ["a": "1", "b": "2"]),
                       TMDBCache.key(path: "/movie/1", query: ["b": "2", "a": "1"]))
        XCTAssertNotEqual(TMDBCache.key(path: "/movie/1", query: [:]),
                          TMDBCache.key(path: "/tv/1", query: [:]),
                          "movie 1 and series 1 are different titles")
    }

    /// `/discover` is how the pipeline finds titles that are new or have newly crossed the vote floor.
    /// Serving it from disk would hide precisely what it is asked to surface.
    func testOnlyDetailEndpointsAreCacheable() {
        XCTAssertTrue(TMDBCache.isCacheable(path: "/movie/278"))
        XCTAssertTrue(TMDBCache.isCacheable(path: "/tv/1396"))
        XCTAssertFalse(TMDBCache.isCacheable(path: "/discover/movie"))
        XCTAssertFalse(TMDBCache.isCacheable(path: "/movie/popular"))
        XCTAssertFalse(TMDBCache.isCacheable(path: "/movie/278/credits"))
    }

    /// Vote counts drift and the vote floor gates on them, so an entry must not live forever.
    func testExpiredEntryReadsAsAbsent() {
        let directory = scratch()
        let key = TMDBCache.key(path: "/movie/278", query: [:])
        TMDBCache(directory: directory).write(key, Data(#"{"id":278}"#.utf8))
        XCTAssertNotNil(TMDBCache(directory: directory, ttl: 3600).read(key))
        XCTAssertNil(TMDBCache(directory: directory, ttl: -1).read(key), "past its TTL")
    }

    /// A broken entry must degrade to a network fetch, never fail the run.
    func testUnreadableEntryReadsAsAbsent() {
        let directory = scratch()
        let cache = TMDBCache(directory: directory)
        let key = TMDBCache.key(path: "/movie/278", query: [:])
        cache.write(key, Data())
        XCTAssertNil(cache.read(key), "an empty body is not a hit")
    }

    /// Decoding proves nothing — every ClassificationWire field is Optional, so `{}` and TMDB's own error
    /// envelope both decode cleanly and would then be served for the whole TTL.
    func testOnlyRealTitleRecordsAreWorthCaching() {
        func ok(_ s: String, appended: Bool = false) -> Bool {
            TMDBClient.isCacheableBody(Data(s.utf8), expectingAppendedResources: appended)
        }
        XCTAssertFalse(ok("{}"))
        XCTAssertFalse(ok(#"{"success":false,"status_code":34,"status_message":"Not found."}"#))
        XCTAssertFalse(ok(#"{"id":278,"title":"   "}"#), "a blank name is not a record")
        XCTAssertFalse(ok("not json at all"))
        XCTAssertTrue(ok(#"{"id":278,"title":"The Shawshank Redemption"}"#))
        XCTAssertTrue(ok(#"{"id":1396,"name":"Breaking Bad"}"#), "series carry `name`, not `title`")

        // A 200 that silently dropped the appended sub-resources yields a title with no keywords, no
        // director and no cast — indistinguishable downstream from a title that genuinely has none.
        XCTAssertFalse(ok(#"{"id":278,"title":"X"}"#, appended: true))
        XCTAssertTrue(ok(#"{"id":278,"title":"X","keywords":{},"credits":{}}"#, appended: true))
    }

    func testEnvironmentSwitchesCachingOff() {
        XCTAssertNil(TMDBCache.fromEnvironment(["TMDB_CACHE": "0"]))
        XCTAssertNotNil(TMDBCache.fromEnvironment([:]))
        XCTAssertEqual(TMDBCache.fromEnvironment(["TMDB_CACHE_TTL_DAYS": "2"])?.ttl, 2 * 24 * 60 * 60)
    }
}
