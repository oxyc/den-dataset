import Foundation
import CryptoKit

/// An on-disk cache of TMDB **detail** responses, so a title fetched once is not fetched again.
///
/// The pipeline re-reads the whole corpus often — a full re-enrich is ~60k detail calls, and the run that
/// motivated this spent hours of its ~10h re-fetching records that had not changed. Detail responses are the
/// right thing to cache: `/movie/{id}` and `/tv/{id}` are addressed by a stable id, and almost everything in
/// them (title, year, genres, keywords, credits, runtime) is immutable in practice.
///
/// `/discover` is deliberately NOT cached. It is a moving query — its whole job is to surface titles that are
/// new or have newly crossed the vote floor — so serving it from disk would defeat the delta pass that finds
/// them.
///
/// ## Staleness
///
/// `voteCount` does drift, and the vote floor gates on it, so entries carry a TTL rather than living forever.
/// A stale vote count only ever misplaces a title near the floor boundary, which the next expiry corrects;
/// re-fetching 60k records to learn that a handful crossed 15 votes is the worse trade.
///
/// ## The API key never touches the disk
///
/// The cache key is derived from the path and the query with `api_key` REMOVED, so no filename, and nothing
/// written, can carry the credential. This is not incidental — a cache directory is exactly the kind of place
/// a secret gets copied into and then forgotten.
public struct TMDBCache: Sendable {
    public let directory: URL
    public let ttl: TimeInterval

    /// Default TTL. Long enough that re-runs within a working period are free, short enough that vote counts
    /// and newly-added keywords are not frozen for a season.
    public static let defaultTTL: TimeInterval = 30 * 24 * 60 * 60

    public init(directory: URL, ttl: TimeInterval = TMDBCache.defaultTTL) {
        self.directory = directory
        self.ttl = ttl
    }

    /// The cache rooted wherever `TMDB_CACHE_DIR` points, else `.cache/tmdb` beside the working directory.
    /// Returns nil when caching is switched off, so the client can hold an Optional and branch once.
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment)
        -> TMDBCache? {
        if let off = env["TMDB_CACHE"], off == "0" || off.lowercased() == "off" { return nil }
        let path = env["TMDB_CACHE_DIR"] ?? ".cache/tmdb"
        let ttl = env["TMDB_CACHE_TTL_DAYS"].flatMap(Double.init).map { $0 * 24 * 60 * 60 } ?? defaultTTL
        return TMDBCache(directory: URL(fileURLWithPath: path), ttl: ttl)
    }

    /// True for the per-title detail endpoints, which are the ones worth caching. Anything else — `/discover`
    /// above all — goes to the network every time.
    public static func isCacheable(path: String) -> Bool {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true)
        // "/movie/278" and "/tv/1396" — exactly two segments, the second an id. A longer path (…/credits)
        // or a non-numeric second segment (/movie/popular) is a different kind of endpoint.
        guard parts.count == 2, parts[0] == "movie" || parts[0] == "tv", Int(parts[1]) != nil else {
            return false
        }
        return true
    }

    /// `path` + the query, minus the credential, hashed. Sorted so an equivalent request maps to one entry.
    public static func key(path: String, query: [String: String]) -> String {
        let safe = query.filter { $0.key != "api_key" }.sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }.joined(separator: "&")
        let digest = SHA256.hash(data: Data("\(path)?\(safe)".utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private func fileURL(_ key: String) -> URL {
        // Two-character fan-out: 60k entries in one directory makes every lookup a linear scan on some
        // filesystems, and `ls` on the cache unusable.
        directory.appendingPathComponent(String(key.prefix(2))).appendingPathComponent("\(key).json")
    }

    /// The cached body, or nil when absent, unreadable or past its TTL. Never throws: a broken cache entry
    /// must degrade to a network fetch, never fail the run.
    public func read(_ key: String) -> Data? {
        let url = fileURL(key)
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let modified = attributes[.modificationDate] as? Date,
              Date().timeIntervalSince(modified) < ttl,
              let data = try? Data(contentsOf: url), !data.isEmpty else { return nil }
        return data
    }

    /// Write via a unique temp file + atomic replace. The pipeline fans out across a task group, so two tasks
    /// can write the same key at once; without this a reader can see a half-written body and decode garbage.
    public func write(_ key: String, _ data: Data) {
        let url = fileURL(key)
        let parent = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        let temp = parent.appendingPathComponent(".\(key).\(UUID().uuidString).tmp")
        guard (try? data.write(to: temp)) != nil else { return }
        if (try? FileManager.default.replaceItemAt(url, withItemAt: temp)) == nil {
            try? FileManager.default.removeItem(at: temp)
        }
    }
}
