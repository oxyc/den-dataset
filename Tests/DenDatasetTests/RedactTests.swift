import XCTest
@testable import DenDataset

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
