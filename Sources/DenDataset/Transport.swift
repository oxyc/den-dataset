import Foundation

/// Bounded retry with exponential backoff for the producer's outbound HTTP (TMDB, Wikidata, Wikipedia).
/// A multi-hour/day batch run WILL hit transient 429/5xx/timeouts from these public APIs; without retry a
/// single blip silently drops a title (or a whole batch's grounding). Only *transient* failures retry —
/// a 404/400/decoding error throws straight through so the caller can treat it as a definitive miss.
public enum Transport {
    /// Retryable HTTP status: rate-limit (429), request-timeout (408), and any 5xx server error.
    public static func isTransient(status: Int) -> Bool {
        status == 408 || status == 429 || (500...599).contains(status)
    }

    /// Whether an error is worth retrying: a transient HTTP status from either client, a TMDB transport error,
    /// or a URLSession connectivity/timeout error. Everything else (404, 400, decoding) is definitive.
    public static func isRetryable(_ error: Error) -> Bool {
        if let e = error as? WikipediaError, case .http(let s) = e { return isTransient(status: s) }
        if let e = error as? DenEmbedError, case .http(let s) = e { return isTransient(status: s) }
        if let e = error as? TMDBError {
            if case .http(let s) = e { return isTransient(status: s) }
            if case .transport = e { return true }
            return false
        }
        if let e = error as? URLError {
            return [.timedOut, .networkConnectionLost, .notConnectedToInternet,
                    .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed].contains(e.code)
        }
        return false
    }

    /// Run `op`, retrying on a retryable error up to `attempts` times with doubling backoff (0.5s, 1s, 2s…).
    /// Re-throws the last error once attempts are exhausted or the error is non-retryable.
    public static func retrying<T>(attempts: Int = 4, baseDelay: UInt64 = 500_000_000,
                                   _ op: () async throws -> T) async throws -> T {
        var delay = baseDelay
        var attempt = 0
        while true {
            attempt += 1
            do {
                return try await op()
            } catch {
                guard attempt < attempts, isRetryable(error) else { throw error }
                try? await Task.sleep(nanoseconds: delay)
                delay *= 2
            }
        }
    }
}
