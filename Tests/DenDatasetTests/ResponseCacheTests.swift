import XCTest
@testable import DenDataset

final class ResponseCacheTests: XCTestCase {
    private func scratch() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("response-cache-tests-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func cache(_ namespace: String = "tmdb", ttl: TimeInterval = 3600,
                       directory: URL? = nil) -> ResponseCache {
        ResponseCache(namespace: namespace, directory: directory ?? scratch(), ttl: ttl)
    }

    /// The whole point: a response fetched once is read back rather than re-fetched.
    func testRoundTrips() {
        let cache = cache()
        let key = cache.key(path: "/movie/278", query: ["append_to_response": "keywords,credits"])
        XCTAssertNil(cache.read(key), "nothing cached yet")
        cache.write(key, Data(#"{"id":278}"#.utf8))
        XCTAssertEqual(cache.read(key), Data(#"{"id":278}"#.utf8))
    }

    /// A cache directory is exactly where a credential gets copied and forgotten, so no key — and therefore
    /// no filename — may carry one.
    func testKeyIgnoresCredentials() {
        let cache = cache()
        let withKey = cache.key(path: "/movie/278", query: ["api_key": "s3cret", "language": "en"])
        let without = cache.key(path: "/movie/278", query: ["language": "en"])
        XCTAssertEqual(withKey, without)
        XCTAssertFalse(withKey.contains("s3cret"))
        XCTAssertEqual(cache.key(path: "/x", query: ["token": "t"]), cache.key(path: "/x", query: [:]))
        XCTAssertEqual(cache.key(path: "/x", query: ["API_KEY": "t"]), cache.key(path: "/x", query: [:]),
                       "credential names are matched case-insensitively")
    }

    func testKeyIsOrderIndependentButPathAndNamespaceSensitive() {
        let cache = cache()
        XCTAssertEqual(cache.key(path: "/movie/1", query: ["a": "1", "b": "2"]),
                       cache.key(path: "/movie/1", query: ["b": "2", "a": "1"]))
        XCTAssertNotEqual(cache.key(path: "/movie/1", query: [:]), cache.key(path: "/tv/1", query: [:]),
                          "movie 1 and series 1 are different titles")
        // Two sources must not be able to collide on a shared path.
        let wiki = ResponseCache(namespace: "wiki", directory: scratch(), ttl: 3600)
        XCTAssertNotEqual(cache.key(path: "/w/api.php", query: [:]),
                          wiki.key(path: "/w/api.php", query: [:]))
    }

    /// Entries of different sources live apart, so one can be cleared without touching the other.
    func testNamespacesAreIsolatedOnDisk() {
        let directory = scratch()
        let tmdb = cache("tmdb", directory: directory)
        let wiki = cache("wiki", directory: directory)
        let key = tmdb.key(path: "/shared", query: [:])
        tmdb.write(key, Data("tmdb".utf8))
        XCTAssertEqual(tmdb.read(key), Data("tmdb".utf8))
        XCTAssertNil(wiki.read(wiki.key(path: "/shared", query: [:])))
    }

    func testExpiredEntryReadsAsAbsent() {
        let directory = scratch()
        let writer = cache("tmdb", directory: directory)
        let key = writer.key(path: "/movie/278", query: [:])
        writer.write(key, Data(#"{"id":278}"#.utf8))
        XCTAssertNotNil(cache("tmdb", ttl: 3600, directory: directory).read(key))
        XCTAssertNil(cache("tmdb", ttl: -1, directory: directory).read(key), "past its TTL")
    }

    /// A broken entry must degrade to a live fetch, never fail the run.
    func testUnreadableEntryReadsAsAbsent() {
        let cache = cache()
        let key = cache.key(path: "/movie/278", query: [:])
        cache.write(key, Data())
        XCTAssertNil(cache.read(key), "an empty body is not a hit")
    }

    // MARK: - Policies

    func testEnvironmentSwitchesCachingOff() {
        XCTAssertNil(WikiCachePolicy.cache(["DEN_CACHE": "0"]), "the global switch")
        XCTAssertNil(WikiCachePolicy.cache(["WIKI_CACHE": "off"]), "the per-source switch")
        XCTAssertNotNil(WikiCachePolicy.cache(["TMDB_CACHE": "0"]), "one source off leaves the other on")
        XCTAssertEqual(WikiCachePolicy.cache(["WIKI_CACHE_TTL_DAYS": "2"])?.ttl, 2 * 24 * 60 * 60)
        // "0 days" reads as "off", not as a write-only cache that never returns a hit.
        XCTAssertNil(WikiCachePolicy.cache(["WIKI_CACHE_TTL_DAYS": "0"]))
    }
}
