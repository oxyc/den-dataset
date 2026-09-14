import Foundation
import CryptoKit

/// An on-disk cache of upstream HTTP responses, so work already paid for is not paid for twice.
///
/// The pipeline re-reads the whole corpus often. A full re-enrich is ~60k TMDB detail calls plus ~80k
/// Wikipedia requests, and the run that motivated this spent most of its ~10h re-fetching material that had
/// not changed. WHAT is safe to cache differs per source, so each caller decides that; this type only knows
/// how to store a body under a key, honestly and atomically.
///
/// ## The credential never touches the disk
///
/// Keys are built from a path and a query with the credential parameters REMOVED, so no filename and nothing
/// written can carry one. A cache directory is exactly the kind of place a secret gets copied into and then
/// forgotten, so this is enforced here rather than left to each call site to remember.
public struct ResponseCache: Sendable {
    /// Separates one source's entries from another's, so `/movie/1` on TMDB cannot collide with a Wikipedia
    /// page of the same path, and one source's cache can be cleared without touching the rest.
    public let namespace: String
    public let directory: URL
    public let ttl: TimeInterval

    /// Query parameters that must never reach a key, and therefore never a filename.
    static let credentialKeys: Set<String> = ["api_key", "token", "access_token", "key", "apikey"]

    public init(namespace: String, directory: URL, ttl: TimeInterval) {
        self.namespace = namespace
        self.directory = directory
        self.ttl = ttl
    }

    /// `path` + the query, minus credentials, hashed with the namespace. Sorted, so an equivalent request
    /// maps to one entry regardless of parameter order.
    public func key(path: String, query: [String: String]) -> String {
        let safe = query.filter { !Self.credentialKeys.contains($0.key.lowercased()) }
            .sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }.joined(separator: "&")
        let digest = SHA256.hash(data: Data("\(namespace)\u{1}\(path)?\(safe)".utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private func fileURL(_ key: String) -> URL {
        // Two-character fan-out: tens of thousands of entries in one directory makes every lookup a linear
        // scan on some filesystems, and the directory itself unusable from a shell.
        directory.appendingPathComponent(namespace)
            .appendingPathComponent(String(key.prefix(2)))
            .appendingPathComponent("\(key).json")
    }

    /// The cached body, or nil when absent, unreadable or past its TTL. Never throws: a broken entry must
    /// degrade to a live fetch, never fail the run.
    public func read(_ key: String) -> Data? {
        let url = fileURL(key)
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let modified = attributes[.modificationDate] as? Date,
              Date().timeIntervalSince(modified) < ttl,
              let data = try? Data(contentsOf: url), !data.isEmpty else { return nil }
        return data
    }

    /// Write via a unique temp file + atomic replace. The pipeline fans out across task groups, so two tasks
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

    // MARK: - Configuration

    /// Root directory for every namespace: `DEN_CACHE_DIR`, else `.cache`.
    public static func root(_ env: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        URL(fileURLWithPath: env["DEN_CACHE_DIR"] ?? ".cache")
    }

    /// A namespaced cache, or nil when caching is switched off for it. `DEN_CACHE=0` disables everything;
    /// `<NAMESPACE>_CACHE=0` disables one source; `<NAMESPACE>_CACHE_TTL_DAYS` overrides its lifetime.
    public static func configured(namespace: String, defaultTTLDays: Double,
                                  env: [String: String] = ProcessInfo.processInfo.environment)
        -> ResponseCache? {
        func off(_ value: String?) -> Bool {
            guard let value = value?.lowercased() else { return false }
            return ["0", "off", "no", "false"].contains(value)
        }
        let prefix = namespace.uppercased()
        if off(env["DEN_CACHE"]) || off(env["\(prefix)_CACHE"]) { return nil }
        let days = env["\(prefix)_CACHE_TTL_DAYS"].flatMap(Double.init) ?? defaultTTLDays
        // A zero or negative TTL means every read misses — a write-only cache, which is never what someone
        // reaching for "0" wants. Treat it as "off", which is.
        guard days > 0 else { return nil }
        return ResponseCache(namespace: namespace, directory: root(env), ttl: days * 24 * 60 * 60)
    }
}

/// Which TMDB paths are worth keeping, and for how long.
public enum TMDBCachePolicy {
    public static let namespace = "tmdb"
    /// A year. The TTL is a BACKSTOP against an entry living forever unnoticed, not the freshness mechanism —
    /// see the note on `WikiCachePolicy.defaultTTLDays`.
    ///
    /// Almost nothing in a detail record changes: title, year, genres, keywords, credits and runtime are
    /// settled once a title has shipped. `voteCount` drifts, and the vote floor reads it — but a below-floor
    /// title is no longer checkpointed, so it is re-judged on every run rather than dropped for good, which
    /// was the only reason a short expiry mattered here.
    public static let defaultTTLDays: Double = 365

    public static func cache(_ env: [String: String] = ProcessInfo.processInfo.environment)
        -> ResponseCache? {
        ResponseCache.configured(namespace: namespace, defaultTTLDays: defaultTTLDays, env: env)
    }

    /// True for the per-title detail endpoints. `/discover` is deliberately excluded: its whole job is to
    /// surface titles that are new or have newly crossed the vote floor, so serving it from disk would hide
    /// exactly what it is asked for.
    public static func isCacheable(path: String) -> Bool {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true)
        // "/movie/278" and "/tv/1396" — exactly two segments, the second an id. A longer path (…/credits) or
        // a non-numeric second segment (/movie/popular) is a different kind of endpoint.
        guard parts.count == 2, parts[0] == "movie" || parts[0] == "tv", Int(parts[1]) != nil else {
            return false
        }
        return true
    }
}

/// Which Wikipedia/Wikidata responses are worth keeping, and for how long.
public enum WikiCachePolicy {
    public static let namespace = "wiki"
    /// A year, deliberately — not a staleness estimate.
    ///
    /// A short expiry looks prudent and is not: it re-fetches EVERYTHING on a schedule nobody chose, to catch
    /// the few percent that moved. Measured over ten weeks, 38% of plots differed in some way but only ~4%
    /// moved the embedding far enough to matter — so expiry is a bad instrument for this, because it cannot
    /// tell those apart and pays full price for the answer.
    ///
    /// Freshness is a DECISION here, taken two ways. Deliberately: `WIKI_CACHE=0` (or `DEN_CACHE=0`) makes a
    /// run re-read everything. Precisely: each grounded title stores the article and revision it was read
    /// from, so a refresh can ask Wikipedia for current revids in bulk and re-fetch only what actually moved
    /// (den-dataset#5). Both beat "it has been a week".
    ///
    /// The TTL that remains is a backstop, so an entry cannot outlive the schema that wrote it unnoticed.
    public static let defaultTTLDays: Double = 365

    public static func cache(_ env: [String: String] = ProcessInfo.processInfo.environment)
        -> ResponseCache? {
        ResponseCache.configured(namespace: namespace, defaultTTLDays: defaultTTLDays, env: env)
    }
}
