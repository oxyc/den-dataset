import DenDataset
import Foundation

// taxonomy-backfill — the fetch and artifact half of the producer, structured as discrete, resumable phases.
// Labelling is no longer here: the decision-only pass (`scripts/v2/run_combined.py`, the `classify` stage)
// produces the labels and facets the corpus join reads, so this tool gathers the inputs that pass needs and
// turns already-decided labels into the shipped artifacts.
//
//   recluster     — k-means over the shipped vectors; groups the vocabulary has no word for.
//
// The embedding is `pipeline/embed.py`, the shipped artifacts are `pipeline/finalize.py`, and the facts are
// `pipeline/facts.py`.
//
// The enrichment that feeds them is `pipeline/enrich.py`. No LLM key — this tool does not classify.

@main
struct TaxonomyBackfill {
    static func main() async {
        let argv = CommandLine.arguments
        // Help is answered before argv is validated: a `--help` must not have to satisfy the required flags
        // it is being asked to describe.
        guard argv.count >= 2 else { Spec.printOverview(to: .standardError); exit(2) }
        if Spec.isHelp(argv[1]) { Spec.printOverview(to: .standardOutput); exit(0) }
        guard let command = Spec.command(named: argv[1]) else {
            FileHandle.standardError.write(Data("unknown command '\(argv[1])'\n\n".utf8))
            Spec.printOverview(to: .standardError); exit(2)
        }
        let rest = Array(argv.dropFirst(2))
        if rest.contains(where: Spec.isHelp) { command.printHelp(to: .standardOutput); exit(0) }
        do {
            try await command.run(Args(rest, declaring: command.flags))
        } catch let error as ToolError {
            FileHandle.standardError.write(Data(Redact.secrets("error: \(error.message)\n").utf8)); exit(1)
        } catch {
            FileHandle.standardError.write(Data(Redact.secrets("error: \(error)\n").utf8)); exit(1)
        }
    }
}

struct ToolError: Error { let message: String }

// MARK: - Commands

enum Commands {
    // MARK: - recluster (DT-F weekly)

    /// Cluster the shipped vectors and report groups the existing vocabulary does NOT explain — candidate
    /// emergent subgenres for the review queue.
    ///
    /// The signal is **label purity**: for each cluster, how dominant its most common existing label is. A
    /// tight cluster whose members share no label is the interesting case — the embedding found a coherent
    /// group the taxonomy has no word for. High-purity clusters are just "Heist" rediscovering itself and
    /// are dropped.
    ///
    /// Reports, never edits. A cluster is a hypothesis: naming it is a human judgement (and a taxonomy bump,
    /// which under DT-F forces a whole-universe pass), so this writes candidates and stops.
    static func recluster(_ args: Args) throws {
        let labelsPath = try args.require("--labels")
        let vectorsPath = try args.require("--vectors")
        let out = try args.require("--out")
        let k = args.int("--k") ?? 200
        let iterations = args.int("--iterations") ?? 8
        let minSize = args.int("--min-size") ?? 25
        let maxPurity = Double(args["--max-purity"] ?? "") ?? 0.35
        let minCohesion = Double(args["--min-cohesion"] ?? "") ?? 0.55

        let labels: LabelsArtifact = try JSON.read(labelsPath)
        let blob = try Data(contentsOf: URL(fileURLWithPath: vectorsPath))
        let decoded: VectorBlob.Decoded
        do { decoded = try VectorBlob.decode(blob) } catch {
            throw ToolError(message: "\(vectorsPath): \(error)")
        }
        let count = decoded.count
        let dim = decoded.dim
        // The blob names its rows, so this is an identity check rather than a count check: purity is
        // reported per label, and a blob whose rows belong to other titles than the ones this labels file
        // describes would report a clean-looking purity for clusters built from the wrong vectors.
        let labelKeys = labels.records.map { VectorBlob.key(mediaType: $0.mediaType, tmdbId: $0.tmdbId) }
        guard decoded.keys == labelKeys else {
            let mismatch = zip(decoded.keys, labelKeys).enumerated().first { $0.element.0 != $0.element.1 }
            throw ToolError(message: "\(vectorsPath) (\(count)×\(dim)) names different titles than "
                + "\(labelsPath) (\(labels.records.count) records)"
                + (mismatch.map { ": row \($0.offset) is \(VectorBlob.mediaType(of: $0.element.0)):"
                    + "\(VectorBlob.tmdbId(of: $0.element.0)) in the blob and "
                    + "\(VectorBlob.mediaType(of: $0.element.1)):\(VectorBlob.tmdbId(of: $0.element.1)) "
                    + "in the labels" } ?? ""))
        }

        // Unit-normalized Doubles once: k-means runs `iterations × k × count` dot products, so paying the
        // conversion per access would dominate the run.
        var rows = [[Double]](repeating: [], count: count)
        blob.withUnsafeBytes { raw in
            let base = raw.baseAddress!.advanced(by: decoded.rowsBase).assumingMemoryBound(to: Int8.self)
            for i in 0..<count {
                var v = [Double](repeating: 0, count: dim)
                var norm = 0.0
                for d in 0..<dim { let x = Double(base[i * dim + d]); v[d] = x; norm += x * x }
                if norm > 0 { let inv = 1 / norm.squareRoot(); for d in 0..<dim { v[d] *= inv } }
                rows[i] = v
            }
        }

        // Deterministic seeding: stride-sample rather than random, so a weekly run is comparable to the last
        // one instead of reshuffling every cluster id.
        let stride = Swift.max(1, count / Swift.max(k, 1))
        var centroids: [[Double]] = (0..<k).compactMap { i in
            let idx = i * stride
            return idx < count ? rows[idx] : nil
        }
        guard !centroids.isEmpty else { throw ToolError(message: "no centroids — is the corpus empty?") }

        var assignment = [Int](repeating: 0, count: count)
        for _ in 0..<iterations {
            for i in 0..<count {
                var best = 0
                var bestScore = -Double.greatestFiniteMagnitude
                for (c, centroid) in centroids.enumerated() {
                    var dot = 0.0
                    for d in 0..<dim { dot += rows[i][d] * centroid[d] }
                    if dot > bestScore { bestScore = dot; best = c }
                }
                assignment[i] = best
            }
            var sums = [[Double]](repeating: [Double](repeating: 0, count: dim), count: centroids.count)
            var counts = [Int](repeating: 0, count: centroids.count)
            for i in 0..<count {
                let c = assignment[i]
                counts[c] += 1
                for d in 0..<dim { sums[c][d] += rows[i][d] }
            }
            for c in 0..<centroids.count where counts[c] > 0 {
                var norm = 0.0
                for d in 0..<dim { norm += sums[c][d] * sums[c][d] }
                if norm > 0 { let inv = 1 / norm.squareRoot(); for d in 0..<dim { sums[c][d] *= inv } }
                centroids[c] = sums[c]
            }
        }

        struct Candidate: Codable {
            let cluster: Int
            let size: Int
            let dominantLabel: String?
            let purity: Double
            /// Mean cosine of members to their centroid. This is the discriminator: low purity ALONE just
            /// finds grab-bags (and at a coarse k, nearly every cluster is one). Low purity plus HIGH
            /// cohesion is the interesting case — a tight group the vocabulary has no word for.
            let cohesion: Double
            let examples: [String]
        }
        var members = [[Int]](repeating: [], count: centroids.count)
        for i in 0..<count { members[assignment[i]].append(i) }

        var candidates: [Candidate] = []
        for (cluster, idxs) in members.enumerated() where idxs.count >= minSize {
            var tally: [String: Int] = [:]
            for i in idxs {
                for lc in labels.records[i].subgenres { tally[lc.label, default: 0] += 1 }
            }
            let dominant = tally.max { $0.value != $1.value ? $0.value < $1.value : $0.key > $1.key }
            let purity = Double(dominant?.value ?? 0) / Double(idxs.count)
            guard purity <= maxPurity else { continue }   // already explained by an existing label
            var cohesionSum = 0.0
            for i in idxs {
                var dot = 0.0
                for d in 0..<dim { dot += rows[i][d] * centroids[cluster][d] }
                cohesionSum += dot
            }
            let cohesion = cohesionSum / Double(idxs.count)
            guard cohesion >= minCohesion else { continue }   // loose grab-bag, not an emergent group
            candidates.append(Candidate(
                cluster: cluster, size: idxs.count, dominantLabel: dominant?.key, purity: purity,
                cohesion: cohesion,
                examples: idxs.prefix(8).map { "\(labels.records[$0].mediaType):\(labels.records[$0].tmdbId)" }))
        }
        // Tightest first: cohesion is what makes a candidate worth a human's time, not raw size.
        candidates.sort { $0.cohesion != $1.cohesion ? $0.cohesion > $1.cohesion : $0.cluster < $1.cluster }
        try JSON.write(candidates, to: out)
        let summary = "recluster: \(candidates.count) emergent candidate(s) of \(centroids.count) clusters "
            + "(size >= \(minSize), purity <= \(maxPurity)) -> \(out)\n"
        FileHandle.standardError.write(Data(summary.utf8))
    }

}

// MARK: - JSON / file IO

enum JSON {
    static func read<T: Decodable>(_ path: String) throws -> T {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        return try JSONDecoder().decode(T.self, from: data)
    }
    /// `.sortedKeys` because the output has to be BYTE-stable across runs, not merely equal as JSON.
    ///
    /// A bare `JSONEncoder` emits a struct's keys in an unspecified order, and Swift reseeds its hash per
    /// process, so two runs over identical data produced identical rows in identical order and still hashed
    /// differently. Measured on the poster sidecar: three consecutive runs, three sha256s, the same 5,933,843
    /// bytes, and a parsed diff showing zero differing rows — only the key order inside each object moved.
    ///
    /// That is not cosmetic. The app folds `metadataSha256` into its syncKey, so every publish re-downloaded
    /// the whole 5.9 MB sidecar on every device even when nothing in it had changed. `SidecarOrder` fixed the
    /// ROW order for exactly this reason and could not fix this, because the remaining instability is inside
    /// the rows.
    static func write<T: Encodable>(_ value: T, to path: String) throws {
        try FileIO.write(try encodeSorted(value), to: path)
    }
    static func encodeSorted<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }
}

enum FileIO {
    static func ensureParent(_ path: String) throws {
        let dir = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }
    static func write(_ data: Data, to path: String) throws {
        try ensureParent(path)
        // Atomic (temp-file + rename): a crash/power-loss/disk-full mid-write must not leave a truncated file.
        // The enrich checkpoint especially — a partial write there silently resets all resume progress.
        try data.write(to: URL(fileURLWithPath: path), options: .atomic)
    }
}

// MARK: - Flag declarations

/// One command-line flag, declared once.
///
/// The declaration is the ONLY way a flag exists: `Args` refuses argv naming a flag no table declares, and
/// traps when the CODE reads one. That second half is what keeps the tables from rotting — a flag added to a
/// command body and not to its table fails the first time that path runs, so there is no separate "is the
/// registry still current?" check to remember to write.
struct Flag {
    /// `.value` consumes the next argv token; `.bare` is present-or-absent.
    enum Shape {
        case value(placeholder: String)
        case bare
    }

    let name: String
    let shape: Shape
    /// One line, shown by `--help`. This is the flag's documentation — there is nowhere else to put it.
    let help: String
    /// Refused before the command body runs, rather than wherever the body happens to read it.
    let required: Bool

    var takesValue: Bool {
        if case .value = shape { return true }
        return false
    }

    /// `--name <placeholder>` as `--help` prints it.
    var spelling: String {
        if case .value(let placeholder) = shape { return "\(name) \(placeholder)" }
        return name
    }

    static func value(_ name: String, _ placeholder: String, _ help: String, required: Bool = false) -> Flag {
        Flag(name: name, shape: .value(placeholder: placeholder), help: help, required: required)
    }

    static func bare(_ name: String, _ help: String) -> Flag {
        Flag(name: name, shape: .bare, help: help, required: false)
    }
}

/// A subcommand: its name, what it does, the flags it may be given, and what to run. Dispatch reads this
/// table rather than a switch, so a command cannot exist in one and be missing from the other.
struct Subcommand {
    let name: String
    let summary: String
    let flags: [Flag]
    let run: (Args) async throws -> Void
}

enum Spec {
    /// Every subcommand, in pipeline order — which is also the order `--help` lists them in.
    static let commands: [Subcommand] = [
        Subcommand(
            name: "recluster",
            summary: "Cluster the shipped vectors and report groups the vocabulary does not explain (DT-F weekly).",
            flags: [
                .value("--labels", "<labels-tNN.json>", "the labels naming each vector", required: true),
                .value("--vectors", "<vectors-eNN.bin>", "the shipped vector blob", required: true),
                .value("--out", "<report.json>", "where to write the emergent-candidate report", required: true),
                .value("--k", "<n>", "number of clusters (default 200)"),
                .value("--iterations", "<n>", "k-means iterations (default 8)"),
                .value("--min-size", "<n>", "ignore clusters smaller than this (default 25)"),
                .value("--max-purity", "<0..1>",
                       "report only clusters whose most common existing label is below this share (default "
                       + "0.35) — a pure cluster is just an existing label rediscovering itself"),
                .value("--min-cohesion", "<0..1>", "report only clusters at least this tight (default 0.55)"),
            ],
            run: { try Commands.recluster($0) }),
    ]

    static func command(named name: String) -> Subcommand? { commands.first { $0.name == name } }

    static func isHelp(_ token: String) -> Bool { token == "--help" || token == "-h" }

    /// The subcommand list — what a bare or unknown invocation gets.
    static func printOverview(to handle: FileHandle) {
        var text = "usage: taxonomy-backfill <command> [flags]\n\n"
        let width = commands.map(\.name.count).max() ?? 0
        for command in commands {
            text += "  " + command.name.padding(toLength: width, withPad: " ", startingAt: 0)
                + "  " + Text.wrapped(command.summary, indent: width + 4) + "\n"
        }
        text += "\nrun `taxonomy-backfill <command> --help` for a command's flags.\n"
        handle.write(Data(text.utf8))
    }
}

extension Subcommand {
    /// This command's declared flags with their help — the only description of them that exists.
    func printHelp(to handle: FileHandle) {
        var text = "usage: taxonomy-backfill \(name) [flags]\n\n  "
            + Text.wrapped(summary, indent: 2) + "\n\n"
        let width = flags.map(\.spelling.count).max() ?? 0
        for flag in flags {
            let body = (flag.required ? "(required) " : "") + flag.help
            text += "  " + flag.spelling.padding(toLength: width, withPad: " ", startingAt: 0)
                + "  " + Text.wrapped(body, indent: width + 4) + "\n"
        }
        handle.write(Data(text.utf8))
    }
}

enum Text {
    /// Wrap to a terminal-ish width, indenting every line after the first so it lines up under the first.
    static func wrapped(_ text: String, indent: Int, width: Int = 108) -> String {
        var lines: [String] = []
        var line = ""
        for word in text.split(separator: " ") {
            if line.isEmpty { line = String(word) }
            else if line.count + 1 + word.count <= width - indent { line += " " + word }
            else { lines.append(line); line = String(word) }
        }
        if !line.isEmpty { lines.append(line) }
        return lines.joined(separator: "\n" + String(repeating: " ", count: indent))
    }
}

// MARK: - Args

struct Args {
    private let declared: [String: Flag]
    private var map: [String: String] = [:]
    private var present: Set<String> = []

    /// Parse `argv` against one subcommand's declarations, refusing everything they do not describe.
    ///
    /// Each refusal closes a way the previous reader lost an argument in silence:
    ///
    /// - An unrecognised flag was kept and never read. A misspelled `--doc-drop-director` on `embed-corpus`
    ///   left `dropDirector` false, exited 0, and composed a DIFFERENT embedding document — which on a fresh
    ///   out-dir `recordComposition` then wrote into index/composition.json as that store's recorded truth,
    ///   matching every later run against it. The documents embed, the vectors rank, the neighbours look
    ///   plausible: nothing downstream can tell.
    /// - A value flag followed by another flag became a BARE one, so its default silently stood:
    ///   `--plot-cap --chunk 7` capped the plot at 1500 and gave the 7 to `--chunk`.
    /// - A missing required flag only surfaced wherever the body happened to read it, which for an
    ///   expensive command is after it has already done work.
    init(_ argv: [String], declaring declarations: [Flag]) throws {
        declared = Dictionary(uniqueKeysWithValues: declarations.map { ($0.name, $0) })
        var index = 0
        while index < argv.count {
            let token = argv[index]
            guard token.hasPrefix("--") else {
                throw ToolError(message: "unexpected argument '\(token)' — every input to this command is a "
                    + "--flag, so a bare word is either a stray value or a flag missing its dashes")
            }
            guard let flag = declared[token] else {
                throw ToolError(message: "unknown flag \(token) — it would have been accepted and never "
                    + "read.\(Self.suggestion(for: token, among: declarations))")
            }
            present.insert(token)
            guard flag.takesValue else { index += 1; continue }
            guard index + 1 < argv.count, !argv[index + 1].hasPrefix("--") else {
                let followed = index + 1 < argv.count ? "'\(argv[index + 1])'" : "nothing"
                throw ToolError(message: "\(flag.spelling) takes a value and was followed by \(followed) "
                    + "— without one the flag would silently fall back to its default")
            }
            map[token] = argv[index + 1]
            index += 2
        }
        if let missing = declarations.first(where: { $0.required && !present.contains($0.name) }) {
            throw ToolError(message: "missing required \(missing.spelling) — \(missing.help)")
        }
    }

    /// The declaration for `key`, or a trap.
    ///
    /// Reading an undeclared flag is a programming error, not operator input: `--help` does not list it and
    /// argv carrying it was refused, so answering nil would reinstate exactly the silence this type removes.
    private func declaration(_ key: String) -> Flag {
        guard let flag = declared[key] else {
            preconditionFailure("\(key) is not declared on this command — add it to the command's flags in Spec")
        }
        return flag
    }

    subscript(_ key: String) -> String? {
        precondition(declaration(key).takesValue, "\(key) is declared bare — read it with has()")
        return map[key]
    }
    func has(_ key: String) -> Bool { _ = declaration(key); return present.contains(key) }
    func int(_ key: String) -> Int? { self[key].flatMap { Int($0) } }
    func double(_ key: String) -> Double? { self[key].flatMap { Double($0) } }
    func require(_ key: String) throws -> String {
        guard let value = self[key] else { throw ToolError(message: "missing \(key)") }
        return value
    }
    func requireInt(_ key: String) throws -> Int {
        guard let value = int(key) else { throw ToolError(message: "missing/invalid \(key)") }
        return value
    }

    /// " Did you mean --x?" for the nearest declared flag, or "" when none is close.
    ///
    /// A near miss is the whole point of refusing: it turns a typo that changed the output into a one-line
    /// fix. The budget keeps it honest — suggesting `--k` for `--gate` would help nobody.
    private static func suggestion(for token: String, among declarations: [Flag]) -> String {
        let budget = max(2, token.count / 3)
        let nearest = declarations
            .map { ($0.name, editDistance($0.name, token)) }
            .filter { $0.1 <= budget }
            .min { $0.1 != $1.1 ? $0.1 < $1.1 : $0.0 < $1.0 }
        guard let nearest else { return "" }
        return " Did you mean \(nearest.0)?"
    }

    /// Levenshtein distance, one row at a time.
    private static func editDistance(_ a: String, _ b: String) -> Int {
        let a = Array(a), b = Array(b)
        guard !a.isEmpty else { return b.count }
        guard !b.isEmpty else { return a.count }
        var previous = Array(0...b.count)
        var current = previous
        for i in 1...a.count {
            current[0] = i
            for j in 1...b.count {
                current[j] = a[i - 1] == b[j - 1]
                    ? previous[j - 1]
                    : 1 + min(previous[j - 1], previous[j], current[j - 1])
            }
            swap(&previous, &current)
        }
        return previous[b.count]
    }
}
