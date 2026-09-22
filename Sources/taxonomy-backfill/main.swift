import DenDataset
import Foundation

// taxonomy-backfill — the fetch and artifact half of the producer, structured as discrete, resumable phases.
// Labelling is no longer here: the decision-only pass (`scripts/v2/run_combined.py`, the `classify` stage)
// produces the labels and facets the corpus join reads, so this tool gathers the inputs that pass needs and
// turns already-decided labels into the shipped artifacts.
//
//   embed-corpus  — compose(facts + already-decided tags + plot) → den-embed → append to the index store.
//
// The index store becomes the shipped artifacts in `pipeline/finalize.py`.
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

    // embed-corpus — build bge-m3 vectors for the EXISTING (already-shipped) labels from the Wikipedia-plot
    // enrichment, WITHOUT re-classifying. Composes facts + the existing tags + the wiki plot, batch-embeds via
    // den-embed, and writes a FRESH index store (labels = the existing records verbatim, aligned to new
    // vectors) into a dedicated out-dir. This is the "semantic vectors now" path: it upgrades the app's ANN
    // from lexical FNV to bge-m3 immediately, reusing the labels we already ship, while the fresh plot-grounded
    // reclassification (which improves the LABELS) is run later. `finalize --out-dir <same>` emits the artifact.
    static func embedCorpus(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let labelsPath = try args.require("--labels")            // the existing labels-t02.json (its tags per title)
        let enrichedDir = args["--enriched-dir"] ?? Layout.enrichedDir(outDir)
        // --doc-facts switches the doc to the CC0 shape (no title, no year, no cast; director + genre from
        // Wikidata). Absent, the doc is composed exactly as before, so this cannot change an existing run.
        struct DocFactsRow: Codable { let directors: [String]; let genres: [String] }
        // Measured on the real corpus, not a synthetic probe: dropping cast/title/year made DIRECTOR the
        // identity token cast used to be, because the surviving clauses are a larger share of a shorter doc.
        // Kubrick's The Shining and Dr. Strangelove went 0.565 -> 0.684, closer than most genuine thematic
        // pairs. Dropping the clause takes the same-director gap from +0.035 to +0.104 while costing the
        // same-actor gap only 0.071 -> 0.056 — the only shape that beats the previous doc on BOTH controls.
        let dropDirector = args.has("--doc-drop-director")
        let docFacts: [String: DocFactsRow]? = try args["--doc-facts"].map { path in
            let loaded: [String: DocFactsRow] = try JSON.read(path)
            FileHandle.standardError.write(Data("  doc-facts: \(loaded.count) rows (CC0 doc shape)\n".utf8))
            return loaded
        }
        // Small chunk by default: den-embed activation memory scales with the batch, so keep requests modest.
        // 15, not 16. den-embed's per-request budget is sum(min(actual_tokens, max_tokens)) <= 8192, and
        // 16 fits ONLY because the min() clips every doc to exactly 16x512 = 8192 and the test is `>`.
        // Raise den-embed's MAX_TOKENS — which `assertDocFits` explicitly advises — and the clip stops
        // binding, a 16-doc request can exceed the budget, and it 413s. Transport treats 413 as definitive,
        // so the run dies on its first flush having written nothing: the same shape as the max_batch bug.
        let chunk = args.int("--chunk") ?? 15
        let limit = args.int("--limit")                          // optional cap (testing)
        // Idle between requests, so a long run can share a laptop. den-embed is already nice 20, but nice only
        // orders CPU contention — it does not stop bge-m3 from holding its activations resident, and a machine
        // deep in swap feels slow no matter how politely the work is scheduled. A pause leaves real gaps the
        // rest of the system can reclaim memory in. Cost is linear and predictable: chunk 15 over ~38k titles
        // is ~2,600 flushes, so each 1000ms of pause adds ~45 minutes.
        let pauseMS = args.int("--pause-ms") ?? 0
        // Cap the PLOT portion (facts + tags are always kept). 4000 chars keeps the median plot whole and every
        // mid-plot genre pivot the length audit found, dropping only low-value end-of-plot twist tails — the
        // knee between similarity quality and bge-m3's O(seq^2) embedding cost.
        // 1500, because every run appends to the SAME store and must compose comparable documents, and
        // 4000 + facts cannot fit any token cap den-embed will accept (its ceiling is 1024 tokens, so
        // ~4096 chars). The old default was from the Python era, which had no token cap at all.
        let plotCap = args.int("--plot-cap") ?? 1500

        // Lean lookup: the existing label record (tags) per (mediaType, tmdbId). No plot text held — the plots
        // are streamed one enriched batch at a time below, so peak memory stays bounded (this was the OOM bug).
        let existing: LabelsArtifact = try JSON.read(labelsPath)
        var labelByKey: [String: IndexRecord] = [:]
        for record in existing.records { labelByKey["\(record.mediaType):\(record.tmdbId)"] = record }

        // Everything that can refuse the run happens BEFORE the store is touched: an unreachable service, a
        // different embedder than built this store, or a plot cap the service would silently truncate. The
        // repair below rewrites files, and a run that cannot do any work has no business repairing anything.
        let denEmbed = DenEmbedClient()
        // `--dump-docs` composes and embeds nothing, so it must not require an embedder to exist. Gating on
        // one here would mean standing up a service on THIS machine purely to write text — and the whole
        // reason the documents are being dumped is that this machine's embedder is the wrong one.
        let dumpOnly = args["--dump-docs"] != nil
        if !dumpOnly {
            let embedder = try await recordEmbedder(outDir: outDir, client: denEmbed, plotCap: plotCap)
            FileHandle.standardError.write(Data("  embedder: \(embedder.label)\n".utf8))
        }
        // The embedder identity cannot see how the document was composed, and two runs of the same service
        // over the same corpus differ entirely on one clause. Refuse a shape change the same way.
        // `dropDirector` only means anything in the lean path — `ComposedDoc.build` always emits the
        // director clause and never consults the flag. Recording it on a full-shape run would refuse two
        // runs that compose byte-identical documents, so normalise it rather than store a value that does
        // not describe the output.
        let composition = EmbedderGate.Composition(docShape: docFacts != nil ? "lean" : "full",
                                                   dropDirector: docFacts != nil && dropDirector,
                                                   plotCap: plotCap)
        try recordComposition(outDir: outDir, docShape: composition.docShape,
                              dropDirector: composition.dropDirector, plotCap: composition.plotCap)
        FileHandle.standardError.write(Data("  composition: \(composition.label)\n".utf8))

        // RESUME: append to an existing store, skipping titles already embedded. A crash (e.g. den-embed OOM)
        // loses at most the current chunk — re-running continues from where it stopped. First reconcile the two
        // append-only stores in case a kill landed between a label line and its vector line, which would leave
        // them unpaired from that point on.
        try reconcileStore(Layout.labelsStore(outDir), Layout.vectorsStore(outDir))
        var done: Set<String> = []
        if FileManager.default.fileExists(atPath: Layout.labelsStore(outDir)) {
            for line in try FileIO.readLines(Layout.labelsStore(outDir)) {
                if let r: IndexRecord = try? JSON.decode(line) { done.insert("\(r.mediaType):\(r.tmdbId)") }
            }
        }
        let labelsHandle = try FileIO.appender(Layout.labelsStore(outDir))
        let vectorsHandle = try FileIO.appender(Layout.vectorsStore(outDir))
        defer { try? labelsHandle.close(); try? vectorsHandle.close() }

        var buffer: [(record: IndexRecord, doc: String)] = []
        var written = 0, skipped = done.count, missing = 0

        // `--dump-docs <path>`: compose and write `{"key":…,"doc":…}` per line, embedding nothing.
        //
        // Composition must not be reimplemented anywhere else — it is 87% plot, capped, in the exact shape
        // recorded in composition.json — but the embedding must happen on whichever den-embed answers live
        // queries, because a vector only means anything within one embedding space.
        //
        // oxyc/den-dataset#21 measured two den-embeds disagreeing on 525 of 1024 dims for identical input
        // and read it as an ISA difference. It is not: the two hosts ran different MAX_TOKENS, which
        // truncates different documents. Matched, arm64 and x86_64 return BYTE-IDENTICAL int8 vectors —
        // 24/24 across Intel AVX2 and AMD AVX-512. So the thing that must match is the service's
        // configuration, not its CPU, and `data/embed-canary.json` is what checks it.
        //
        // Dumping still earns its place: it lets the DOCUMENTS travel instead of the vectors, so composition
        // stays here, embedding happens on the serving space, nothing is reimplemented and no service is
        // exposed.
        let dumpPath = args["--dump-docs"]
        let dumpHandle = try dumpPath.map { try FileIO.appender($0) }
        defer { try? dumpHandle?.close() }

        func flush() async throws {
            guard !buffer.isEmpty else { return }
            if let dumpHandle {
                for item in buffer {
                    try dumpHandle.writeLine(JSON.encodeLine(
                        DocRow(key: "\(item.record.mediaType):\(item.record.tmdbId)", doc: item.doc)))
                    written += 1
                }
                buffer.removeAll(keepingCapacity: true)
                if written % 2000 == 0 {
                    FileHandle.standardError.write(Data("  composed \(written) (skipped \(skipped))…\n".utf8))
                }
                return
            }
            let vectors = try await denEmbed.embedManyInt8(buffer.map(\.doc))
            guard vectors.count == buffer.count else {
                throw ToolError(message: "den-embed returned \(vectors.count) vectors for \(buffer.count) docs")
            }
            for (item, vector) in zip(buffer, vectors) {
                try labelsHandle.writeLine(JSON.encodeLine(item.record))
                try vectorsHandle.writeLine(JSON.encodeLine(VectorRow(tmdbId: item.record.tmdbId, v: vector.map(Int.init))))
                written += 1
            }
            buffer.removeAll(keepingCapacity: true)
            if written % 2000 == 0 { FileHandle.standardError.write(Data("  embedded \(written) (skipped \(skipped))…\n".utf8)) }
            if pauseMS > 0 { try await Task.sleep(nanoseconds: UInt64(pauseMS) * 1_000_000) }
        }

        // Stream the enriched batch files one at a time — only ONE batch of plots is in memory at once.
        //
        // BATCH-NUMBER order, not `sorted()`: a key can appear in several batches and 505 of them disagree
        // about `hasWikiPlot`, so the order decides whether a title embeds with its plot or with "". The
        // `done` set below makes this first-wins, so oldest-first would keep the stale answer; reading newest
        // last and letting it overwrite is what `finalize` already does when it de-dups.
        let files = EnrichedBatches.orderedNames(inDirectory: enrichedDir).reversed()
        outer: for file in files {
            let dtos: [EnrichedDTO] = try JSON.read((enrichedDir as NSString).appendingPathComponent(file))
            for dto in dtos {
                let key = "\(dto.mediaType):\(dto.tmdbId)"
                if done.contains(key) { continue }
                guard let record = labelByKey[key] else { missing += 1; continue }  // enriched but not in shipped labels
                let title = dto.toEnrichedTitle()
                let tags = record.subgenres.map(\.label) + record.moods.map(\.label)
                // Plot clause only from the WIKIPEDIA plot (ToS-clean); a no-wiki-plot title composes on facts+tags.
                let plot = title.hasWikiPlot ? Self.cappedPlot(title.overview, maxChars: plotCap) : ""
                let doc: String
                if let docFacts {
                    // CC0 shape: Wikidata's director + genre, our own tags, the Wikipedia plot. `createdBy` is
                    // Wikidata (P170) on a batch the current enrichment wrote; an older batch can still carry
                    // TMDB `created_by` names where Wikidata had none, until it is enriched again.
                    let f = docFacts["\(dto.mediaType):\(dto.tmdbId)"]
                    doc = ComposedDoc.buildLean(directors: dropDirector ? [] : (f?.directors ?? []),
                                                creators: title.createdBy,
                                                genres: f?.genres ?? [], tags: tags, plot: plot)
                } else {
                    doc = ComposedDoc.build(title: title, tags: tags, plot: plot)
                }
                buffer.append((record, doc))
                done.insert(key)
                if buffer.count >= chunk { try await flush() }
                if let limit, written + buffer.count >= limit { break outer }
            }
        }
        try await flush()
        print(JSON.line(["written": written, "skipped": skipped, "missingLabel": missing,
                         "store": Layout.labelsStore(outDir)]))
    }

    /// Record WHICH embedder is building this store, and refuse to append to a store built by a different one.
    ///
    /// The store is append-only and resumable across days, and `finalize` only ever checked that the vectors
    /// share one LENGTH — which every bge-m3 generation does. So a run resumed after a den-embed upgrade
    /// quietly produced a corpus half-embedded by each, with nothing anywhere able to say so. Same reason the
    /// identity lands in the manifest: the app and den-atlas embed live queries through the service, and a
    /// corpus embedded by a different generation retrieves subtly wrong neighbours while looking healthy.
    /// Check the service against the store, and against what this run intends to send it, BEFORE writing
    /// anything down. Persisting the identity first meant a run that `assertDocFits` then refused had
    /// already recorded the current service against an empty store — so following the error's own advice
    /// (raise den-embed's MAX_TOKENS and retry) hit the mismatch guard instead, on a store with zero rows,
    /// and the operator had to know to delete index/embedder.json by hand.
    @discardableResult
    static func recordEmbedder(outDir: String, client: DenEmbedClient,
                               plotCap: Int) async throws -> DenEmbedClient.Identity {
        let path = Layout.embedderIdentity(outDir)
        let now = try await client.identity()
        // Identity first: on a service upgrade "this would mix two embedders" is the finding that matters,
        // and leading with the plot-cap error sent the operator off to fix the lesser one.
        let previous: DenEmbedClient.Identity? = try? JSON.read(path)
        var hasRows = false
        if FileManager.default.fileExists(atPath: Layout.labelsStore(outDir)) {
            hasRows = !(try FileIO.readLines(Layout.labelsStore(outDir))).isEmpty
        }

        switch EmbedderGate.decide(previous: previous, now: now, storeHasRows: hasRows) {
        case .mismatch(let was, let isNow):
            // Differing ONLY in the epoch means the file predates that field, not that a different
            // embedder built the store — and "restore the previous service" is then impossible advice,
            // naming the same build on both sides of the message.
            let epochOnly = previous.map {
                $0.vectorEpoch == 0 && now.vectorEpoch > 0 && $0.model == now.model
                    && $0.dims == now.dims && $0.maxTokens == now.maxTokens
            } ?? false
            throw ToolError(message: "this store was embedded by \(was) but den-embed now reports "
                + "\(isNow) — appending would mix two embedders into one corpus. "
                + (epochOnly
                   ? "These differ only in the epoch, so \(path) predates that field rather than recording "
                     + "a different embedder: if this service did build the store, add a vectorEpoch of "
                     + "\(now.vectorEpoch) to that file."
                   : "Either restore the previous service, or start a fresh --out-dir and re-embed."))
        case .unknownProvenance:
            // This WAS the shipped store's case — 37.5k rows from the Python/ORT-1.22 service, with no
            // identity file. It no longer is: the 2026-09-13 re-embed records den-embed/5.1.1 in
            // `out-t02-cc0b/index/embedder.json`, so the path below is for a store from before that.
            throw ToolError(message: "\(outDir) holds an existing store but no \(path), so what embedded it "
                + "is unknown and appending \(now.label) may mix two embedders. Write that file with the "
                + "identity that built it — a corpus from before the Rust rewrite is "
                + #"{"model":"bge-m3","dims":1024,"runtime":"pre-3.0.0","maxTokens":0}"#
                + " — or start a fresh --out-dir.")
        case .matches:
            try assertDocFits(plotCap: plotCap, embedder: now)
            try await verifyEmbeddingSpace(outDir: outDir, client: client)
            return now
        case .firstUse:
            try assertDocFits(plotCap: plotCap, embedder: now)
            try await verifyEmbeddingSpace(outDir: outDir, client: client)
            try FileIO.ensureParent(path)
            try JSON.writePretty(now, to: path)
            return now
        }
    }

    /// The known-answer test, run before this command writes its first vector, and recorded where
    /// `finalize` will find it.
    ///
    /// It sits AFTER the embedder gate deliberately. On a service upgrade "this would mix two embedders
    /// into one corpus" is the finding that matters and names the store that is at risk; leading with the
    /// canary would report the same event as an anonymous space change. The canary's job is the case the
    /// gate cannot see — a service whose `/health` is identical and whose numbers are not.
    static func verifyEmbeddingSpace(outDir: String, client: DenEmbedClient) async throws {
        let canary = EmbedSpaceCanary.defaultPath()
        guard FileManager.default.fileExists(atPath: canary) else {
            throw ToolError(message: "no embedding canary at \(canary), so nothing can say which space "
                + "this service embeds into. Run from the repo root, or point DEN_EMBED_CANARY at "
                + "data/embed-canary.json.")
        }
        let stamp: EmbedSpaceCanary.Stamp
        do {
            stamp = try await EmbedSpaceCanary.verify(
                path: canary, client: client,
                url: DenEmbedClient.defaultBaseURL().absoluteString) { line in
                    FileHandle.standardError.write(Data("  \(line)\n".utf8))
                }
        } catch let failure as EmbedSpaceCanary.Failure {
            throw ToolError(message: failure.description)
        }
        try FileIO.ensureParent(Layout.embeddingSpace(outDir))
        try JSON.writePretty(stamp, to: Layout.embeddingSpace(outDir))
    }

    /// The same guard for how the document is composed, which `recordEmbedder` cannot see.
    ///
    /// Recovered by experiment rather than found written down: the shipped store is the lean shape with the
    /// director clause dropped at a 3500-char cap, established by re-embedding probe titles and comparing
    /// bytes to the shipped rows (12/12 exact at those settings; 10/12 with the director clause kept — the
    /// two failures being the only director-carrying probes; 2/12 at cap 1500 — the two matches being
    /// short-plot titles no cap can affect).
    ///
    /// The DROP-DIRECTOR flag is pinned exactly. The CAP is not: `cappedPlot` snaps back to the last ". ",
    /// so each probe is insensitive across an interval, and intersecting the ten gives [3479..3534] — every
    /// value in that window reproduces the shipped bytes. 3500 is the value because it is the only round one
    /// in the window and `docs/OPERATE.md` already prescribed it, not because the bytes single it out.
    static func recordComposition(outDir: String, docShape: String,
                                  dropDirector: Bool, plotCap: Int) throws {
        let path = Layout.compositionIdentity(outDir)
        let now = EmbedderGate.Composition(docShape: docShape, dropDirector: dropDirector, plotCap: plotCap)
        let previous: EmbedderGate.Composition? = try? JSON.read(path)
        var hasRows = false
        if FileManager.default.fileExists(atPath: Layout.labelsStore(outDir)) {
            hasRows = !(try FileIO.readLines(Layout.labelsStore(outDir))).isEmpty
        }
        switch EmbedderGate.decideComposition(previous: previous, now: now, storeHasRows: hasRows) {
        case .mismatch(let was, let isNow):
            throw ToolError(message: "this store's documents were composed as \(was) but this run composes "
                + "\(isNow) — appending would put two document shapes in one vector space, which no "
                + "similarity score can separate afterwards. Match the recorded settings, or start a fresh "
                + "--out-dir.")
        case .unknownProvenance:
            // Deliberately not adopting the current settings: that would write a guess down as a fact, and
            // the guard would then pass forever on the store that actually has the problem.
            //
            // The suggested values are `out-t02-cc0b`'s and NOBODY ELSE'S. Several other stores have rows
            // and no record — out-t02, out-t02-cc0, out-t02-rebuild, out-vecnow* — and they were composed
            // differently. An operator who pastes these into one of those does exactly what this branch
            // exists to prevent, so the message has to say whose values they are.
            throw ToolError(message: "\(outDir) holds rows but no \(path), so how its documents were "
                + "composed is unknown and appending \(now.label) may mix two shapes. If this store is "
                + #"out-t02-cc0b (the shipped one) its composition is {"docShape":"lean","#
                + #""dropDirector":true,"plotCap":3500} — write that file. For any OTHER store these "#
                + "values are wrong: recover them by re-embedding a few long-plot titles and comparing "
                + "bytes against its own rows (docs/OPERATE.md, \"Recovering a store's composition\"), or "
                + "start a fresh --out-dir.")
        case .matches:
            return
        case .firstUse:
            try FileIO.ensureParent(path)
            try JSON.writePretty(now, to: path)
        }
    }

    /// Refuse to compose documents the service will silently cut in half.
    ///
    /// den-embed truncates at `max_tokens` server-side, returns a normal-looking vector, and says nothing —
    /// no error, no field in the response. So a producer that composes documents longer than the service
    /// will embed loses their tails silently, across the whole corpus.
    ///
    /// The corpus shipping TODAY was re-embedded 2026-09-13 (`builtAt 2026-09-13T19:22:39Z`,
    /// `embedderRuntime den-embed/5.1.1`, `maxTokens 1024`). Its plot cap is NOT recorded anywhere —
    /// `out-t02-cc0b/embed.log` names the doc shape and row count but no cap, `embed-corpus`'s own default
    /// is 1500 and `scripts/embed-corpus-run.sh` defaults 3500. `EmbedderGate` cannot see the cap any more
    /// than it can see the doc shape, so appending to that store risks docs truncated differently from the
    /// 38,532 already in it. Establish the cap before any top-up.
    ///
    /// The PREVIOUS corpus used no cap at all, established from timestamps rather than current defaults
    /// (three earlier versions of this comment got it wrong in every direction): its `dataset.meta.json`
    /// recorded builtAt 2026-07-05T07:22:47Z, and 8f93235 — the commit that introduced plot capping at all —
    /// was authored 11:40:55Z, four hours LATER. At its parent, BOTH producers read
    /// `let plot = title.hasWikiPlot ? title.overview : ""`. It ran against the Python service, five weeks
    /// before the Rust rewrite, and that service had no token cap whatsoever.
    ///
    /// Plot is ~87% of the composed document by length, so this cap is most of what the vector sees. Per
    /// title: at 512 tokens ~61% of titles truncate, keeping ~73% of their plot; at 1024 only ~21% do,
    /// keeping ~97%. Hence the 1024/3500 defaults in embed-corpus-run.sh — NOT summarisation, which
    /// compresses harder than the truncation it replaces and drops the proper nouns retrieval matches on.
    static func assertDocFits(plotCap: Int, embedder: DenEmbedClient.Identity) throws {
        guard embedder.maxTokens > 0 else { return }   // a service too old to report it
        let factsAndTags = 500          // the composed doc's non-plot half
        let budget = embedder.maxTokens * 4
        guard plotCap + factsAndTags <= budget else {
            throw ToolError(message: "--plot-cap \(plotCap) composes documents of roughly "
                + "\(plotCap + factsAndTags) chars, but \(embedder.label) truncates at \(embedder.maxTokens) "
                + "tokens (~\(budget) chars) and would cut them silently. Lower --plot-cap to "
                + "\(budget - factsAndTags) or below, or raise MAX_TOKENS on the service — its "
                + "ceiling is 1024, above which it exceeds the memory the container is given.")
        }
    }

    /// Cap a plot to `maxChars`, ending on the last sentence boundary within the cap (so the embedded doc reads
    /// as complete prose rather than a mid-word cut). Facts + tags are composed separately and never capped.
    static func cappedPlot(_ plot: String, maxChars: Int) -> String {
        guard plot.count > maxChars else { return plot }
        let head = String(plot.prefix(maxChars))
        if let stop = head.range(of: ". ", options: .backwards) {
            return String(head[..<stop.lowerBound]) + "."
        }
        return head
    }

    /// Truncate two append-only store files to their longest prefix of lines that both parse AND name the same
    /// title — repairs a crash that wrote a label line but not its vector line (or vice versa).
    ///
    /// Line count alone is not enough. `flush` writes label-then-vector per title, so a kill between the two
    /// leaves labels one line longer; but a kill mid-`write` leaves a TRUNCATED final line, which still counts
    /// as a line — so the two files can look equal-length while the last vector belongs to no title. Repairing
    /// on count alone kept that pair, and `finalize` compared only counts, so every title after the tear would
    /// ship carrying its neighbour's vector: no error anywhere, and a whole tail of the corpus retrieving the
    /// wrong titles.
    /// A tear loses the in-flight chunk, so this only ever drops a TAIL — and it refuses anything larger.
    ///
    /// Checking alignment rather than line counts means a disagreement can now be found ANYWHERE in the
    /// file, not just at the end, and truncating to the first one would delete everything after it. A store
    /// whose 50th line is unparseable would lose 37,000 rows; a store written before a field was added to
    /// `IndexRecord` (whose synthesized `Decodable` requires every key) would decode nothing and be erased
    /// outright. Both are hours of den-embed time, destroyed by a repair that runs before any work starts,
    /// with an atomic replace leaving nothing to recover. Repairing on line count alone could never do that,
    /// so the alignment check needs a bound the count check did not.
    ///
    /// Beyond the bound this is not a tear and this is not the tool for it: name the line and change nothing.
    static let maxTearRepair = 1000

    static func reconcileStore(_ labelsPath: String, _ vectorsPath: String) throws {
        guard FileManager.default.fileExists(atPath: labelsPath),
              FileManager.default.fileExists(atPath: vectorsPath) else { return }
        let labels = try FileIO.readLines(labelsPath)
        let vectors = try FileIO.readLines(vectorsPath)
        let n = StoreIntegrity.alignedPrefix(labels: labels, vectors: vectors)
        switch StoreIntegrity.repair(labelCount: labels.count, vectorCount: vectors.count,
                                     aligned: n, maxDrop: maxTearRepair) {
        case .nothingToDo:
            return
        case .refuse(let dropping, let line):
            throw ToolError(message: "the store diverges at line \(line); repairing that would discard "
                + "\(dropping) rows, which is a corrupt store rather than an interrupted write. Nothing was "
                + "changed — inspect line \(line) of \(labelsPath) and \(vectorsPath).")
        case .truncate:
            let dropped = max(labels.count, vectors.count) - n
            FileHandle.standardError.write(Data(("  repaired an interrupted write: dropped \(dropped) "
                + "unpaired row(s), store now \(n)\n").utf8))
        }
        func rewrite(_ lines: [String], _ path: String) throws {
            let body = n == 0 ? "" : lines.prefix(n).joined(separator: "\n") + "\n"
            try FileIO.write(Data(body.utf8), to: path)
        }
        if labels.count != n { try rewrite(labels, labelsPath) }
        if vectors.count != n { try rewrite(vectors, vectorsPath) }
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

// MARK: - DTOs

/// The enriched record `pipeline/enrich.py` writes, as the embed pass reads it back.
struct EnrichedDTO: Codable {
    let tmdbId: Int
    let mediaType: String
    let title: String
    let year: Int?
    let overview: String
    let genreIDs: [Int]
    let genres: [String]
    let keywordIDs: [Int]
    let keywords: [String]
    let originCountry: [String]
    let originalLanguage: String?
    let voteCount: Int
    // FP-2: credits feed the composed embedding doc; `hasWikiPlot` marks `overview` as the live Wikipedia plot
    // (re-grounded at enrich) so `embed-corpus` composes the Plot clause only when a real plot was found.
    let director: String?
    let topCast: [String]
    /// TV showrunners — the credit that links a series to its creator's other work, since `director` is
    /// null for nearly all series.
    let createdBy: [String]
    /// Minutes, from Wikidata — the enriched record has no other runtime source.
    let runtimeMinutes: Int?
    let hasWikiPlot: Bool
    /// The enwiki article the plot came from and the revision it was read at — the two facts a refresh needs
    /// to ask "did this move?" in bulk instead of re-reading every plot to find out.
    let plotArticle: String?
    let plotRevId: Int?
    /// Why there is no plot, when there is none — `noArticle`, `noSection`, `belowFloor`, `fetchFailed`.
    /// Written so a later pass re-runs the subset a fix reaches, not the whole corpus.
    let noPlotReason: String?
    /// Which headings the plot came from. The text is a concatenation, so one article name no longer says
    /// where it came from, and a heading-rule change can target the articles it affects.
    let plotSections: [String]
    /// Which Wikipedia the plot came from; "en" unless the fallback found it elsewhere.
    let plotLanguage: String?
    /// Which candidate the plot came from — `own`, `own-other-language` or `source-work` — and whether a
    /// redirect moved the fetch off the article that was asked for. Together they answer "is this text
    /// about this title?", which nothing downstream can re-derive from the article name.
    ///
    /// Both absent on every batch written before the enrich pass recorded them: that is UNKNOWN, and a
    /// reader must report it as unknown rather than read it as `own`/false.
    let plotArticleRole: String?
    let plotArticleRedirected: Bool?
    /// The LENGTH of TMDB's overview, never its text — the stub check's only input. See `EnrichedTitle`.
    let overviewChars: Int

    // Tolerant decode: a scratch batch written before FP-2's fields existed (or a hand-authored fixture)
    // must still load — decodeIfPresent + default keeps the new credit/plot fields optional.
    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        tmdbId = try c.decode(Int.self, forKey: .tmdbId)
        mediaType = try c.decode(String.self, forKey: .mediaType)
        title = try c.decode(String.self, forKey: .title)
        year = try c.decodeIfPresent(Int.self, forKey: .year)
        overview = try c.decodeIfPresent(String.self, forKey: .overview) ?? ""
        genreIDs = try c.decodeIfPresent([Int].self, forKey: .genreIDs) ?? []
        genres = try c.decodeIfPresent([String].self, forKey: .genres) ?? []
        keywordIDs = try c.decodeIfPresent([Int].self, forKey: .keywordIDs) ?? []
        keywords = try c.decodeIfPresent([String].self, forKey: .keywords) ?? []
        originCountry = try c.decodeIfPresent([String].self, forKey: .originCountry) ?? []
        originalLanguage = try c.decodeIfPresent(String.self, forKey: .originalLanguage)
        voteCount = try c.decodeIfPresent(Int.self, forKey: .voteCount) ?? 0
        director = try c.decodeIfPresent(String.self, forKey: .director)
        topCast = try c.decodeIfPresent([String].self, forKey: .topCast) ?? []
        createdBy = try c.decodeIfPresent([String].self, forKey: .createdBy) ?? []
        runtimeMinutes = try c.decodeIfPresent(Int.self, forKey: .runtimeMinutes)
        hasWikiPlot = try c.decodeIfPresent(Bool.self, forKey: .hasWikiPlot) ?? false
        // Absent on every batch written before the refresh fields existed. Nil reads as "revision unknown",
        // which a refresh must treat as changed — re-reading a plot is cheap, pinning a stale one is not.
        plotArticle = try c.decodeIfPresent(String.self, forKey: .plotArticle)
        plotRevId = try c.decodeIfPresent(Int.self, forKey: .plotRevId)
        noPlotReason = try c.decodeIfPresent(String.self, forKey: .noPlotReason)
        plotSections = try c.decodeIfPresent([String].self, forKey: .plotSections) ?? []
        plotLanguage = try c.decodeIfPresent(String.self, forKey: .plotLanguage)
        // Absent on every batch enriched before the pass recorded which candidate won. Left nil, which
        // every reader must treat as "cannot tell" — defaulting to `own` would report 47,529 titles as
        // correctly grounded on the strength of a field that was never written.
        plotArticleRole = try c.decodeIfPresent(String.self, forKey: .plotArticleRole)
        plotArticleRedirected = try c.decodeIfPresent(Bool.self, forKey: .plotArticleRedirected)
        // Batches written before the overview was dropped at the client boundary still carry its text. Fall
        // back to its length so a re-read of those keeps the same stub verdict — the text itself is ignored.
        overviewChars = try c.decodeIfPresent(Int.self, forKey: .overviewChars)
            ?? overview.trimmingCharacters(in: .whitespacesAndNewlines).count
    }

    func toEnrichedTitle() -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType == "tv" ? .tv : .movie, title: title, year: year,
                      overview: overview, genreIDs: genreIDs, genreNames: genres,
                      keywords: zip(keywordIDs, keywords).map { Keyword(id: $0, name: $1) },
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast, createdBy: createdBy,
                      runtimeMinutes: runtimeMinutes, hasWikiPlot: hasWikiPlot,
                      plotArticle: plotArticle, plotRevId: plotRevId, overviewChars: overviewChars,
                      noPlotReason: noPlotReason, plotSections: plotSections,
                      plotLanguage: plotLanguage,
                      // No role recorded is no provenance, not a default one — `PlotProvenance` has no
                      // "unknown" case because the absence of the value IS the unknown.
                      plotProvenance: plotArticleRole.flatMap(PlotArticleRole.init(rawValue:))
                          .map { PlotProvenance(role: $0, redirected: plotArticleRedirected) })
    }
}

// MARK: - Paths

enum Layout {
    static func enrichedDir(_ dir: String) -> String { join(dir, "enriched") }
    static func embedderIdentity(_ dir: String) -> String { join(dir, "index/embedder.json") }
    static func embeddingSpace(_ dir: String) -> String { join(dir, "index/embedding-space.json") }
    static func compositionIdentity(_ dir: String) -> String { join(dir, "index/composition.json") }
    static func labelsStore(_ dir: String) -> String { join(dir, "index/labels.jsonl") }
    static func vectorsStore(_ dir: String) -> String { join(dir, "index/vectors.jsonl") }
    static func datasetMeta(_ dir: String) -> String { join(dir, "dataset.meta.json") }
    static func join(_ dir: String, _ rel: String) -> String { (dir as NSString).appendingPathComponent(rel) }
}

// MARK: - JSON / file IO

enum JSON {
    static func read<T: Decodable>(_ path: String) throws -> T {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        return try JSONDecoder().decode(T.self, from: data)
    }
    static func decode<T: Decodable>(_ s: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(s.utf8))
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
    static func writePretty<T: Encodable>(_ value: T, to path: String) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try FileIO.write(try encoder.encode(value), to: path)
    }
    static func encodeSorted<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }
    static func encodeLine<T: Encodable>(_ value: T) -> String {
        (try? String(data: JSONEncoder().encode(value), encoding: .utf8)) ?? "{}"
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
    static func readLines(_ path: String) throws -> [String] {
        let text = try String(contentsOfFile: path, encoding: .utf8)
        return text.split(whereSeparator: \.isNewline).map(String.init)
    }
    static func appender(_ path: String) throws -> LineAppender {
        try ensureParent(path)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        return try LineAppender(path: path)
    }
}

final class LineAppender {
    private let handle: FileHandle
    init(path: String) throws {
        handle = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
        handle.seekToEndOfFile()
    }
    func writeLine(_ s: String) throws { handle.write(Data((s + "\n").utf8)) }
    func close() throws { try handle.close() }
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
            name: "embed-corpus",
            summary: "Embed ALREADY-DECIDED labels: compose the document, embed, append to the index store.",
            flags: [
                .value("--out-dir", "<dir>", "the run directory the index store is written into", required: true),
                .value("--labels", "<labels-t02.json>", "the existing labels whose tags each document carries",
                       required: true),
                .value("--enriched-dir", "<dir>", "where the enriched batches (and their plots) live "
                       + "(default: <out-dir>/enriched)"),
                .value("--doc-facts", "<doc-facts.json>",
                       "switch the document to the CC0 lean shape — no title, year or cast; director and genre "
                       + "from Wikidata. Absent, the FULL shape is composed, which is a different vector space"),
                .bare("--doc-drop-director",
                      "drop the director clause from the lean document. Measured: it takes the same-director "
                      + "gap from +0.035 to +0.104 while costing the same-actor gap only 0.071 → 0.056"),
                .value("--chunk", "<n>",
                       "documents per den-embed request (default 15). Bounded by den-embed's per-request token "
                       + "budget: 8192 / --plot-cap's token cost. Above it every request is a 413, which is "
                       + "not retried, so the run dies on its first flush having written nothing"),
                .value("--plot-cap", "<chars>",
                       "cap the plot clause (default 1500 — every run appends to the SAME store and must "
                       + "compose comparable documents). Recorded in index/composition.json"),
                .value("--limit", "<n>", "stop after N newly-embedded titles; the run is resumable, so this "
                       + "segments a long one"),
                .value("--pause-ms", "<ms>",
                       "idle between requests so a long run can share a busy machine. At --chunk 15 each "
                       + "1000ms costs ~45min over a full corpus"),
                .value("--dump-docs", "<path>",
                       "write the composed documents and embed NOTHING, so they can be embedded on the "
                       + "den-embed that will SERVE them. What must match is that service's configuration "
                       + "(MAX_TOKENS above all), not its CPU — the canary is what checks it"),
            ],
            run: { try await Commands.embedCorpus($0) }),
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
