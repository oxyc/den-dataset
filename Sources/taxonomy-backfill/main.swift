import DenDataset
import Foundation

// taxonomy-backfill — the fetch and artifact half of the producer, structured as discrete, resumable phases.
// Labelling is no longer here: the decision-only pass (`scripts/v2/run_combined.py`, the `classify` stage)
// produces the labels and facets the corpus join reads, so this tool gathers the inputs that pass needs and
// turns already-decided labels into the shipped artifacts.
//
//   facts         — the CC0 Wikidata facts den-atlas /recommend ranks on.
//   recluster     — k-means over the shipped vectors; groups the vocabulary has no word for.
//
// The embedding is `pipeline/embed.py` and the shipped artifacts are `pipeline/finalize.py`.
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
    // facts — the CC0 facts sidecar den-atlas /recommend ranks on. Takes an explicit id list (the DELTA: the
    // titles atlas has never seen) or the shipped labels. Needs no plot, no classification and no embedding,
    // which is what lets it cover brand-new releases the >=50-vote worklist floor cannot reach.
    static func facts(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let batchSize = args.int("--batch") ?? 100
        // Ids as "movie:123,tv:456" or a file of the same, one per line or whitespace-separated.
        var keys: [String] = []
        if let inline = args["--ids"] {
            let text = FileManager.default.fileExists(atPath: inline)
                ? try String(contentsOfFile: inline, encoding: .utf8) : inline
            keys = text.split(whereSeparator: { ", \n\t".contains($0) }).map(String.init)
        } else {
            let labels: LabelsArtifact = try JSON.read(try args.require("--labels"))
            keys = labels.records.map { "\($0.mediaType):\($0.tmdbId)" }
        }
        // hasVector is FALSE for delta records and true for corpus ones. /recommend must never let a
        // vectorless record into an ANN path, so this is stated per record rather than inferred.
        let hasVector = args.has("--has-vector")

        var byType: [String: [Int]] = [:]
        for key in keys {
            let parts = key.split(separator: ":")
            guard parts.count == 2, let id = Int(parts[1]) else { continue }
            byType[String(parts[0]), default: []].append(id)
        }
        let total = byType.values.reduce(0) { $0 + $1.count }
        FileHandle.standardError.write(Data("  facts: \(total) titles, \(WikidataFacts.specs.count) properties\n".utf8))

        let source = WikipediaSource()
        // RESUME. A full-corpus pass is ~9k SPARQL requests over hours, and writing only at the end means one
        // dropped connection loses all of it. The raw per-title fields are checkpointed as they arrive, and a
        // re-run skips ids already present.
        let checkpoint = (outDir as NSString).appendingPathComponent("facts-fields.json")
        var fields: [String: [String: WikidataFacts.FieldValue]] = (try? JSON.read(checkpoint)) ?? [:]
        // --titles-only backfills just the titles hop over records the checkpoint already holds. Without it
        // the resume skips every finished id, so a field added after a completed scrape could never be filled
        // without re-fetching all 24 properties.
        let titlesOnly = args.has("--titles-only")
        if !fields.isEmpty {
            FileHandle.standardError.write(Data("  resuming from \(fields.count) checkpointed titles\n".utf8))
            for (type, ids) in byType {
                byType[type] = ids.filter {
                    let r = fields["\(type):\($0)"]
                    return titlesOnly ? (r?["titles"] == nil) : (r == nil)
                }
            }
        }
        var done = 0
        var skipped = 0
        for (type, ids) in byType {
            let mediaType: MediaType = type == "tv" ? .tv : .movie
            for start in stride(from: 0, to: ids.count, by: batchSize) {
                let slice = Array(ids[start..<min(start + batchSize, ids.count)])
                do {
                    for spec in WikidataFacts.specs where !titlesOnly && !(spec.tvOnly && mediaType != .tv) {
                        let got = try await source.facts(spec: spec, forTMDBIds: slice, mediaType: mediaType)
                        for (id, value) in got { fields["\(type):\(id)", default: [:]][spec.key] = value }
                        // Pace the scrape. Firing 24 requests back-to-back per batch sustains ~3/s for hours,
                        // which WDQS throttles: the run then fast-fails on intermittent 429s rather than timing
                        // out, and a restart loop retries the same batch forever without advancing. Measured at
                        // 0.36 s/request, this roughly halves throughput and is the difference between finishing
                        // and stalling at 16,500.
                        try await Task.sleep(nanoseconds: 300_000_000)
                    }
                    // Titles are their own hop: search needs the enwiki article title, the label, P1476 and every
                    // alias, and atlas's title index carries only TMDB's ORIGINAL title today — so "parasite" and
                    // "spirited away" miss while "Gisaengchung" and "Sen to Chihiro" hit.
                    let t = try await source.titles(forTMDBIds: slice, mediaType: mediaType)
                    for (id, v) in t {
                        var m: [String: WikidataFacts.FieldValue] = [:]
                        if let a = v.article ?? v.label { m["en"] = .string(WikidataFacts.strippedArticleSuffix(a)) }
                        if let o = v.original ?? v.label { m["orig"] = .string(o) }
                        if !v.aliases.isEmpty { m["aliases"] = .list(v.aliases.sorted()) }
                        if !m.isEmpty { fields["\(type):\(id)", default: [:]]["titles"] = .object(m) }
                    }
                } catch {
                    // A batch that dies after Transport's retries must not end the run. This is 24 requests
                    // per 100 ids against WDQS for hours: one of them WILL eventually time out, and throwing
                    // here abandoned every title after it — a scrape died at 3,100 of 8,949 with the other
                    // 5,849 untouched, despite the checkpoint being per batch.
                    //
                    // A full pass also drops whatever this batch half-wrote. Its resume keys on a row
                    // EXISTING, so a row holding the 9 properties that landed before the timeout would read
                    // as finished and the title would ship missing the other 15 — silently, and only in the
                    // titles unlucky enough to straddle a failure. One re-fetch is cheaper than that.
                    //
                    // `--titles-only` must NOT drop the row: there the resume deliberately keeps rows that
                    // already exist (it selects on a missing `titles` key), so those rows hold a COMPLETE
                    // set of facts from an earlier pass, and deleting one over a failed titles hop would
                    // destroy 24 properties to retry a 25th.
                    if !titlesOnly {
                        for id in slice { fields.removeValue(forKey: "\(type):\(id)") }
                    }
                    skipped += slice.count
                    try JSON.write(fields, to: checkpoint)
                    FileHandle.standardError.write(Data(
                        "  facts: batch of \(slice.count) \(type) FAILED, left for a later pass — \(error)\n".utf8))
                    continue
                }
                done += slice.count
                try JSON.write(fields, to: checkpoint)
                FileHandle.standardError.write(Data("  facts \(done)/\(total)…\n".utf8))
            }
        }

        // Resolve every Q-id that actually appears, once, into the shared `entities` map. Names come from the
        // label service with "en,mul" — Wikidata has moved proper names to `mul`, and asking for "en" alone
        // returns the bare Q-id, which is how Christopher Nolan went missing from the doc facts.
        // BOTH shapes. A spec that is `single: true` collapses to `.string(qid)`, never `.list`, so a
        // harvest that only walked lists never saw it: `franchise` is the one entity spec declared that
        // way, and all 3,019 of its Q-ids went unlabelled for as long as this loop existed. The 68 that
        // did resolve only did so because they happened to appear in some other field's list. Downstream,
        // an unlabelled entity is silently dropped, so the column read as "almost no title has a
        // franchise" rather than as a bug.
        var qids = Set<String>()
        for row in fields.values {
            for (_, value) in row {
                switch value {
                case .list(let items): for i in items where i.hasPrefix("Q") { qids.insert(i) }
                case .string(let s) where s.hasPrefix("Q"): qids.insert(s)
                default: break
                }
            }
        }
        // Resolve ONLY names we do not already have, and persist them beside the fields. This pass ran over
        // every Q-id in the whole accumulated checkpoint on every restart: at 16,500 titles that is 92,036
        // entities, 307 sequential requests, minutes of work redone each attempt — so a resumed run spent its
        // entire life here and never reached a new batch. The checkpoint froze at exactly the point where this
        // pass outgrew the run, which looked like a WDQS timeout and was not.
        let namesPath = (outDir as NSString).appendingPathComponent("facts-entities.json")
        var rawEntities: [String: [String: String]] = (try? JSON.read(namesPath)) ?? [:]
        let unresolved = qids.subtracting(rawEntities.keys)
        FileHandle.standardError.write(Data(
            "  entity names: \(rawEntities.count) cached, \(unresolved.count) to resolve\n".utf8))
        if !unresolved.isEmpty {
            // entityDetails, not entityNames: search needs the ALIASES ("tom hanks" against a record holding
            // only a Q-id) and P4985 lets a client open a person page without a name lookup.
            for (qid, info) in try await source.entityDetails(Array(unresolved)) {
                var e: [String: String] = [:]
                if let n = info.name { e["en"] = n }
                if let p = info.tmdbPersonId { e["tmdbPersonId"] = p }
                if !info.aliases.isEmpty { e["aliases"] = info.aliases.sorted().joined(separator: "\u{1F}") }
                if !e.isEmpty { rawEntities[qid] = e }
            }
            try JSON.write(rawEntities, to: namesPath)
        }

        // WHAT EACH ADAPTATION IS ADAPTED FROM. `basedOn` is a bare Q-id, which links adaptations of one
        // source to each other but cannot answer "films based on books" — nothing in it says whether the
        // target is a novel, a manga or a video game. One P31 hop over the distinct targets does, and it is
        // cheap: ~6k source works against 38.7k titles, resolved once and checkpointed like the names above.
        //
        // It earns a browse row (4,750 titles) and a ranking signal — someone who reliably picks adaptations
        // should see more of them — so it belongs on the record rather than in a hardcoded catalogue.
        let sourceTypesPath = (outDir as NSString).appendingPathComponent("facts-source-types.json")
        var sourceTypes: [String: [String]] = (try? JSON.read(sourceTypesPath)) ?? [:]
        let sourceQIDs = Set(fields.values.flatMap { row -> [String] in
            if case .list(let items)? = row["basedOn"] { return items }
            return []
        })
        let unresolvedSources = sourceQIDs.subtracting(sourceTypes.keys)
        FileHandle.standardError.write(Data(
            "  source kinds: \(sourceTypes.count) cached, \(unresolvedSources.count) to resolve\n".utf8))
        if !unresolvedSources.isEmpty {
            for (qid, types) in try await source.instanceOf(Array(unresolvedSources)) {
                sourceTypes[qid] = types
            }
            // Remember the ones Wikidata states nothing for, or every run re-asks the same dead ends.
            for qid in unresolvedSources where sourceTypes[qid] == nil { sourceTypes[qid] = [] }
            try JSON.write(sourceTypes, to: sourceTypesPath)
        }
        var kindCounts: [String: Int] = [:]
        for (key, row) in fields {
            guard case .list(let targets)? = row["basedOn"] else { continue }
            let kinds = Set(targets.compactMap { qid -> String? in
                guard let types = sourceTypes[qid], !types.isEmpty else { return nil }
                return WikidataFacts.sourceKind(forTypes: types)?.rawValue
            })
            guard !kinds.isEmpty else { continue }
            fields[key]?["basedOnKind"] = .list(kinds.sorted())
            for kind in kinds { kindCounts[kind, default: 0] += 1 }
        }
        FileHandle.standardError.write(Data(
            "  basedOnKind: \(kindCounts.sorted { $0.value > $1.value }.map { "\($0.key)=\($0.value)" }.joined(separator: " "))\n".utf8))

        // Genre names keep Wikidata's media suffix ("drama television series"), which is a poor display string
        // and would defeat the TMDB match. Strip it for genres only — a PERSON named "... film" is not a thing
        // we want to rewrite.
        var entities = rawEntities
        let genreQIDs = Set(fields.values.flatMap { row -> [String] in
            if case .list(let items)? = row["genres"] { return items }
            return []
        })
        for qid in genreQIDs {
            if let name = entities[qid]?["en"] {
                let stripped = WikipediaSource.strippedGenre(name)
                if !stripped.isEmpty { entities[qid] = ["en": stripped] }
            }
        }
        let genreMap = WikidataFacts.genreMap(entities: entities) { $0 }
        FileHandle.standardError.write(Data("  genreMap: \(genreMap.count) of \(genreQIDs.count) genres map to TMDB ids\n".utf8))

        struct Out: Encodable {
            let schema: Int
            let datasetVersion: String
            let genreMap: [String: [String: Int]]
            let entities: [String: Entity]
            let records: [Rec]
            /// One entity as it SHIPS. The scrape checkpoints aliases as a 0x1F-joined string because its
            /// store is `[String: String]`, but that is an internal encoding: consumers read `aliases` as a
            /// LIST, and atlas's `RawEntity.aliases` is typed `Vec<String>`. Emitting the joined string
            /// instead made a 27 MB facts file unparseable at its first entity — atlas dropped the whole
            /// file and ran `facts_unusable`, losing people search, imdbId, countries and /recommend, from
            /// one character in one field. The split belongs here, once, at the boundary.
            struct Entity: Encodable {
                let en: String?
                let tmdbPersonId: String?
                let aliases: [String]?

                init(_ fields: [String: String]) {
                    en = fields["en"]
                    tmdbPersonId = fields["tmdbPersonId"]
                    let joined = fields["aliases"] ?? ""
                    let parts = joined.split(separator: "\u{1F}").map(String.init)
                    aliases = parts.isEmpty ? nil : parts
                }
            }
            struct Rec: Encodable {
                let mediaType: String
                let tmdbId: Int
                let hasVector: Bool
                let fields: [String: WikidataFacts.FieldValue]
                func encode(to encoder: Encoder) throws {
                    var c = encoder.container(keyedBy: Key.self)
                    try c.encode(mediaType, forKey: Key("mediaType"))
                    try c.encode(tmdbId, forKey: Key("tmdbId"))
                    try c.encode(hasVector, forKey: Key("hasVector"))
                    // Fields are inlined, not nested under "fields": atlas reads record.genres, not
                    // record.fields.genres, and an absent key is how "unknown" is expressed.
                    for (k, v) in fields { try c.encode(v, forKey: Key(k)) }
                }
                struct Key: CodingKey {
                    let stringValue: String; var intValue: Int? { nil }
                    init(_ s: String) { stringValue = s }
                    init?(stringValue s: String) { stringValue = s }
                    init?(intValue: Int) { nil }
                }
            }
        }
        let meta: DatasetMeta? = try? JSON.read(Layout.datasetMeta(outDir))
        let records = fields.keys.sorted().compactMap { key -> Out.Rec? in
            let parts = key.split(separator: ":")
            guard parts.count == 2, let id = Int(parts[1]) else { return nil }
            return Out.Rec(mediaType: String(parts[0]), tmdbId: id, hasVector: hasVector,
                           fields: fields[key] ?? [:])
        }
        let version = meta?.datasetVersion ?? "unversioned"
        let path = (outDir as NSString).appendingPathComponent("facts-\(version).json")
        try JSON.write(Out(schema: 1, datasetVersion: version, genreMap: genreMap,
                           entities: entities.mapValues(Out.Entity.init), records: records), to: path)
        // `skipped` is reported rather than swallowed: a pass that gave up on batches is not a finished
        // scrape, and the caller's next move (run it again to sweep them) depends on knowing the number.
        print(JSON.line(["facts": records.count, "entities": entities.count, "genreMap": genreMap.count, "path": path,
                         "skippedAfterFailure": skipped, "hasVector": hasVector ? 1 : 0]))
    }

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

// MARK: - Paths

enum Layout {
    static func datasetMeta(_ dir: String) -> String { join(dir, "dataset.meta.json") }
    static func join(_ dir: String, _ rel: String) -> String { (dir as NSString).appendingPathComponent(rel) }
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
    static func line(_ dict: [String: Any]) -> String {
        (try? JSONSerialization.data(withJSONObject: dict))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
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
            name: "facts",
            summary: "The CC0 facts sidecar den-atlas /recommend ranks on. Needs no plot and no embedding.",
            flags: [
                .value("--out-dir", "<dir>", "facts-fields.json (the resumable checkpoint) is written here",
                       required: true),
                .value("--ids", "<movie:1,tv:2|path>",
                       "the ids to scrape, inline or as a file of the same — the DELTA path. Without it, "
                       + "--labels names the corpus"),
                .value("--labels", "<labels-t02.json>", "the shipped labels, when --ids is not given"),
                .value("--batch", "<n>", "ids per SPARQL request (default 100)"),
                .bare("--has-vector",
                      "mark each record as having a vector. FALSE for delta records: /recommend must never "
                      + "let a vectorless record into an ANN path"),
                .bare("--titles-only",
                      "backfill just the titles hop over ids the checkpoint already holds — the resume "
                      + "otherwise skips every finished id"),
            ],
            run: { try await Commands.facts($0) }),
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
