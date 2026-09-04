import Foundation

/// Strip credentials out of anything on its way to a log file or the terminal.
///
/// TMDB authenticates with an `api_key` QUERY PARAMETER, and `URLError` embeds the failing URL in its
/// description — so `Log.append(…, "\(error)")` on any timeout or connection drop wrote
/// `NSErrorFailingURLStringKey=https://api.themoviedb.org/…?api_key=<the real key>` into
/// `out/enrich-<media>.log`. Those failures are routine on a multi-hour run, so the key was not merely
/// leakable, it was reliably logged. The out-dir is gitignored, so this is a plaintext key on disk and in
/// anything an operator pastes while asking for help — not a git leak, but not nothing either.
///
/// Applied at the SINKS rather than at each call site: there are a dozen places that interpolate an error,
/// and the next one added would not know to do this.
public enum Redact {
    /// Anything whose value is a secret if it appears as a query parameter.
    private static let secretParams = ["api_key", "access_token", "token", "password"]

    /// Environment variables whose value is a secret wherever it appears verbatim.
    private static let secretVars = [
        "TMDB_API_KEY", "WIKIMEDIA_ENTERPRISE_PASSWORD", "WIKIMEDIA_ENTERPRISE_TOKEN",
    ]

    public static func secrets(_ text: String) -> String {
        var out = text
        for param in secretParams {
            // `param=<value>` up to the next separator — & ? space quote comma or end.
            out = out.replacingOccurrences(
                of: "\(param)=[^&?\\s\"',)]+", with: "\(param)=REDACTED",
                options: .regularExpression)
        }
        // A secret can also reach a message without its parameter name — an auth header echoed back, a
        // config value quoted in an error. Match the value itself when we know it.
        let env = ProcessInfo.processInfo.environment
        for name in secretVars {
            guard let value = env[name], value.count >= 8 else { continue }
            out = out.replacingOccurrences(of: value, with: "REDACTED")
        }
        return out
    }
}
