import CryptoKit
import DenDataset
import Foundation

// taxonomy-backfill (DT-C) — the one-time central classification + embedding job, structured as discrete,
// resumable phases so the **Haiku-subagent backend** (chosen in DT-C) can drive it: the Opus loop runs the
// deterministic Swift phases and slots Haiku subagents in for the per-title labels. No external LLM key.
//
//   worklist  — build the universe (TMDB /discover sorted vote_count.desc for the pilot; daily-export parse
//               for the full run) → out/worklist-<media>.json
//   enrich    — next N un-enriched ids → ONE TMDB call each (append_to_response=keywords), drop below the
//               vote floor / anime / fetch failures (logged) → out/enriched/batch-<id>.json (+ checkpoint).
//               The enriched batch is SCRATCH (holds raw TMDB text) — fed to Haiku, never shipped.
//   [Haiku]   — the Opus loop spawns Haiku subagents over the batch → out/votes/batch-<id>-pass<N>.json
//   assemble  — enriched batch + its vote passes → the SAME calibrated aggregation as the in-process path
//               (TaxonomyClassifier.classify(rawVotes:)) → embed + int8-quantize → append to the index store.
//   finalize  — index store → labels-<taxonomy>.json + vectors-<embed>.bin + report.json + dataset.meta.json
//               (DERIVED only). Folds in the former import-dataset.mjs job (meta + gzipped labels).
//   score     — labels vs the golden set → primary-genre accuracy + multi-label F1 + per-family precision
//               (with --gate: exit non-zero if a family misses its target).
//
// Env: TMDB_API_KEY (enrichment only). NO LLM key — the labels come from Haiku subagents, not an API.

@main
struct TaxonomyBackfill {
    static func main() async {
        let argv = CommandLine.arguments
        guard argv.count >= 2 else { usage(); exit(2) }
        let args = Args(Array(argv.dropFirst(2)))
        do {
            switch argv[1] {
            case "worklist": try await Commands.worklist(args)
            case "enrich":   try await Commands.enrich(args)
            case "enrich-ids": try await Commands.enrichIds(args)
            case "escalation": try Commands.escalation(args)
            case "assemble": try Commands.assemble(args)
            case "finalize": try Commands.finalize(args)
            case "score":    try Commands.score(args)
            default: usage(); exit(2)
            }
        } catch let error as ToolError {
            FileHandle.standardError.write(Data("error: \(error.message)\n".utf8)); exit(1)
        } catch {
            FileHandle.standardError.write(Data("error: \(error)\n".utf8)); exit(1)
        }
    }

    static func usage() {
        FileHandle.standardError.write(Data("""
        usage: taxonomy-backfill <command> [flags]
          worklist --mode discover|export --media movie|tv [--count N] [--vote-floor 50] [--file export.json] --out <path>
          enrich   --worklist <path> [--vote-floor 50] [--limit 150] --out-dir <dir>
          escalation --batch-id <n> --out-dir <dir>   (after pass 1: emit titles needing n=3)
          assemble --batch-id <n> --out-dir <dir>
          finalize --out-dir <dir>
          score    --labels <labels.jsonl|labels-t01.json> --golden <golden.json> [--gate]

        """.utf8))
    }
}

struct ToolError: Error { let message: String }

// MARK: - Commands

enum Commands {
    // worklist — the universe. `discover` (vote_count.desc, the highest-vote titles first — the pilot seed);
    // `export` (TMDB's daily ID export, the full run). Anime is filtered uniformly at enrich, not here.
    static func worklist(_ args: Args) async throws {
        let mediaType: MediaType = args["--media"] == "tv" ? .tv : .movie
        let out = try args.require("--out")
        var entries: [WLEntry] = []

        switch args["--mode"] ?? "discover" {
        case "export":
            let file = try args.require("--file")
            guard let text = try? String(contentsOfFile: file, encoding: .utf8) else {
                throw ToolError(message: "can't read export \(file)")
            }
            entries = Worklist.parse(jsonLines: text, mediaType: mediaType).map { WLEntry($0) }
        default:
            let count = args.int("--count") ?? 500
            let floor = args.int("--vote-floor") ?? 50
            let tmdb = try TMDB.client()
            let origins = (args["--origins"] ?? "").split(separator: ",").map(String.init)
            var seen = Set<Int>()
            // Page one `vote_count.desc` query until exhausted or `target` reached.
            func collect(_ query: DiscoverQuery, until target: Int) async throws {
                var page = 1
                while entries.count < target && page <= 500 {
                    let result = try await tmdb.discover(query, page: page)
                    for item in result.items where seen.insert(item.tmdbID.rawValue).inserted {
                        entries.append(WLEntry(tmdbId: item.tmdbID.rawValue, mediaType: mediaType.rawValue))
                    }
                    if page >= result.totalPages { break }
                    page += 1
                }
            }
            if !origins.isEmpty {
                // Foreign-depth expansion (DT-C region-aware floor): one `vote_count.gte` slice per origin
                // country, fully paged. The expansion uses a low floor (e.g. 15) for EU/SA/AU-NZ origins —
                // the band where regional titles live. The vote floor is re-checked at enrich; ids already in
                // the base worklist / checkpoint are skipped there, so this is purely additive.
                for country in origins {
                    try await collect(DiscoverQuery(mediaType: mediaType, originCountry: [country],
                                                    voteCountGte: floor, sortBy: "vote_count.desc"), until: .max)
                }
            } else if count <= 10_000 {
                // A single global query suffices (TMDB serves ≤500 pages × 20 = 10k results) — highest vote first.
                try await collect(DiscoverQuery(mediaType: mediaType, voteCountGte: floor, sortBy: "vote_count.desc"), until: count)
            } else {
                // Past 10k, partition by release year (newest first) to page beyond the per-query ceiling —
                // each year's `vote_count.desc` slice, accumulated + de-duped until `count`.
                let yearMax = args.int("--year-max") ?? 2026
                let yearMin = args.int("--year-min") ?? 1920
                for year in stride(from: yearMax, through: yearMin, by: -1) where entries.count < count {
                    try await collect(DiscoverQuery(
                        mediaType: mediaType, voteCountGte: floor,
                        releaseDateGte: "\(year)-01-01", releaseDateLte: "\(year)-12-31",
                        sortBy: "vote_count.desc"), until: count)
                }
            }
            if origins.isEmpty { entries = Array(entries.prefix(count)) }
        }

        try JSON.writePretty(entries, to: out)
        print("worklist: \(entries.count) \(mediaType.rawValue) ids → \(out)")
    }

    // enrich — next `limit` un-enriched worklist ids → one TMDB call each (append_to_response=keywords),
    // bounded concurrency. Drops below the vote floor / anime / fetch failures (each logged + counted).
    // Writes one scratch batch file for Haiku + advances the resumable checkpoint.
    static func enrich(_ args: Args) async throws {
        let worklistPath = try args.require("--worklist")
        let outDir = try args.require("--out-dir")
        let floor = args.int("--vote-floor") ?? 50
        let limit = args.int("--limit") ?? 150

        let worklist: [WLEntry] = try JSON.read(worklistPath)
        var checkpoint = (try? JSON.read(Layout.enrichCheckpoint(outDir)) as EnrichCheckpoint) ?? EnrichCheckpoint()
        let pending = worklist.filter { !checkpoint.processed.contains($0.tmdbId) }.prefix(limit)
        guard !pending.isEmpty else {
            print(JSON.line(["remaining": 0, "count": 0])); return
        }

        let tmdb = try TMDB.client()
        let batchId = checkpoint.nextBatch
        var survivors: [EnrichedDTO] = []
        var belowFloor = 0, anime = 0, failures = 0, noOverview = 0

        try await withThrowingTaskGroup(of: EnrichOutcome.self) { group in
            for entry in pending {
                group.addTask {
                    do {
                        let title = try await tmdb.classificationRecord(MediaIdentifier(entry.tmdbId, entry.media))
                        if title.voteCount < floor { return .belowFloor(entry.tmdbId) }
                        if isAnime(title) { return .anime(entry.tmdbId) }
                        // Can't classify a stub — drop titles with no / very-short overview (DT-C region-aware floor).
                        if title.overview.trimmingCharacters(in: .whitespacesAndNewlines).count < 20 {
                            return .noOverview(entry.tmdbId)
                        }
                        return .ok(EnrichedDTO(title))
                    } catch {
                        return .failure(entry.tmdbId, "\(error)")
                    }
                }
            }
            for try await outcome in group {
                switch outcome {
                case .ok(let dto): survivors.append(dto)
                case .belowFloor: belowFloor += 1
                case .anime: anime += 1
                case .noOverview: noOverview += 1
                case .failure(let id, let reason):
                    failures += 1
                    Log.append(Layout.enrichLog(outDir), "fetch-failure id=\(id) \(reason)")
                }
            }
        }

        survivors.sort { $0.tmdbId < $1.tmdbId }
        try JSON.writePretty(survivors, to: Layout.enrichedBatch(outDir, batchId))
        for entry in pending { checkpoint.processed.insert(entry.tmdbId) }
        checkpoint.nextBatch += 1
        checkpoint.totals.merge(belowFloor: belowFloor, anime: anime, failures: failures, noOverview: noOverview)
        try JSON.write(checkpoint, to: Layout.enrichCheckpoint(outDir))

        let remaining = worklist.count - checkpoint.processed.count
        print(JSON.line([
            "batchId": batchId, "count": survivors.count, "belowFloor": belowFloor,
            "anime": anime, "noOverview": noOverview, "failures": failures, "remaining": remaining,
            "batch": Layout.enrichedBatch(outDir, batchId),
        ]))
    }

    // enrich-ids — re-fetch an EXPLICIT set of already-vetted ids (taken from a vote file) into one enriched
    // batch, bypassing the worklist/checkpoint/filters. Used to rebuild a scratch enriched batch that lost
    // alignment with its votes, and to gather a targeted re-pass set. The published index never contains this.
    static func enrichIds(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let batchId = try args.requireInt("--batch-id")
        let idsPath = try args.require("--ids")
        let mediaType: MediaType = args["--media"] == "tv" ? .tv : .movie
        let rows: [HaikuVote] = try JSON.read(idsPath)
        let ids = rows.map(\.tmdbId)
        let tmdb = try TMDB.client()
        var out: [EnrichedDTO] = []
        try await withThrowingTaskGroup(of: EnrichedDTO?.self) { group in
            for id in ids {
                group.addTask {
                    (try? await tmdb.classificationRecord(MediaIdentifier(id, mediaType))).map(EnrichedDTO.init)
                }
            }
            for try await dto in group { if let dto { out.append(dto) } }
        }
        out.sort { $0.tmdbId < $1.tmdbId }
        try JSON.writePretty(out, to: Layout.enrichedBatch(outDir, batchId))
        print(JSON.line(["batchId": batchId, "requested": ids.count, "enriched": out.count]))
    }

    // escalation — adaptive self-consistency (DT-C / DT-classification-prompt.md): after pass 1, emit the
    // SUBSET of a batch that needs a 2nd/3rd Haiku pass — primary genre ∈ {Drama, Comedy, Thriller} (the
    // broad/ambiguous ones) OR a borderline top subgenre (max confidence < 0.65). Confident titles keep just
    // pass 1; only the hard cases pay for n=3. A title missing from pass 1 is escalated (so it gets re-tried).
    static func escalation(_ args: Args) throws {
        let outDir = try args.require("--out-dir")
        let batchId = try args.requireInt("--batch-id")
        let borderlinePrimaries: Set<String> = ["Drama", "Comedy", "Thriller"]
        let borderlineConfidence = 0.65

        let enriched: [EnrichedDTO] = try JSON.read(Layout.enrichedBatch(outDir, batchId))
        let pass1: [HaikuVote] = (try? JSON.read(Layout.votePass(outDir, batchId, 1))) ?? []
        let byID = Dictionary(pass1.map { ($0.tmdbId, $0) }, uniquingKeysWith: { a, _ in a })

        let needs = enriched.filter { dto in
            guard let vote = byID[dto.tmdbId] else { return true }       // missing in pass 1 → re-try
            guard let primary = vote.primaryGenre else { return true }   // no primary → uncertain
            if borderlinePrimaries.contains(primary) { return true }     // broad/ambiguous primary → n=3
            // A weak-but-PRESENT top subgenre is borderline; an absent subgenre is "confidently none", not
            // borderline — don't pay for n=3 just because a confident Sci-Fi/Horror has no subgenre.
            if let maxSub = (vote.subgenres ?? []).map(\.confidence).max(), maxSub < borderlineConfidence {
                return true
            }
            return false
        }
        try JSON.writePretty(needs, to: Layout.escalateBatch(outDir, batchId))
        print(JSON.line(["batchId": batchId, "escalate": needs.count, "total": enriched.count,
                         "file": Layout.escalateBatch(outDir, batchId)]))
    }

    // assemble — one enriched batch + its Haiku vote passes → calibrated classification (reused, tested) →
    // embed + quantize → append to the index store. Opus never classifies; the judgment stays in DenDataset.
    static func assemble(_ args: Args) throws {
        let outDir = try args.require("--out-dir")
        let batchId = try args.requireInt("--batch-id")
        let force = args.has("--force")   // re-process already-classified titles (targeted re-pass)

        let enriched: [EnrichedDTO] = try JSON.read(Layout.enrichedBatch(outDir, batchId))
        let passes = try loadVotePasses(outDir: outDir, batchId: batchId)
        guard !passes.isEmpty else { throw ToolError(message: "no vote passes for batch \(batchId) in \(Layout.votesDir(outDir))") }

        // Per-family acceptance thresholds (override for calibration sweeps; default = DenDataset calibrated).
        let defaults = TaxonomyClassifier.Thresholds()
        let thresholds = TaxonomyClassifier.Thresholds(
            subgenre: args.double("--sub-threshold") ?? defaults.subgenre,
            thematic: args.double("--thematic-threshold") ?? defaults.thematic,
            mood: args.double("--mood-threshold") ?? defaults.mood)
        let classifier = TaxonomyClassifier(llm: NoLLM(), samples: passes.count, thresholds: thresholds)
        let embedder = HashingEmbedder()
        var classified = (try? JSON.read(Layout.classifyCheckpoint(outDir)) as ClassifyCheckpoint) ?? ClassifyCheckpoint()
        var noPrimary = 0, missingVotes = 0

        let labelsHandle = try FileIO.appender(Layout.labelsStore(outDir))
        let vectorsHandle = try FileIO.appender(Layout.vectorsStore(outDir))
        defer { try? labelsHandle.close(); try? vectorsHandle.close() }

        for dto in enriched where force || !classified.done.contains(dto.tmdbId) {
            // One raw-JSON string per pass for this title (re-serialized) → the calibrated aggregation seam.
            let raws: [String] = passes.compactMap { $0[dto.tmdbId] }
            guard !raws.isEmpty else { missingVotes += 1; continue }
            let title = dto.toEnrichedTitle()
            guard let classification = classifier.classify(rawVotes: raws, title: title) else {
                noPrimary += 1; classified.done.insert(dto.tmdbId); continue
            }
            let vector = Quantizer.int8(blockingEmbed(embedder, embeddingText(title)))
            let record = classification.indexRecord(animated: title.genreIDs.contains(16))   // TMDB genre 16
            try labelsHandle.writeLine(JSON.encodeLine(record))
            try vectorsHandle.writeLine(JSON.encodeLine(VectorRow(tmdbId: dto.tmdbId, v: vector.map(Int.init))))
            classified.done.insert(dto.tmdbId)
        }
        classified.totals.merge(noPrimary: noPrimary, missingVotes: missingVotes)
        try JSON.write(classified, to: Layout.classifyCheckpoint(outDir))
        print(JSON.line([
            "batchId": batchId, "classifiedTotal": classified.done.count,
            "noPrimary": noPrimary, "missingVotes": missingVotes,
        ]))
    }

    // finalize — the index store → the shipped artifacts. DERIVED labels + quantized vectors ONLY; asserts no
    // raw TMDB text leaked in. Recomputes the run report (coverage + primary-genre dist + confidence buckets)
    // and folds in the former import-dataset.mjs step: dataset.meta.json (the manifest the Rust server reads)
    // + a gzipped copy of the labels blob.
    static func finalize(_ args: Args) throws {
        // e01 was the pilot embedding; e02 is the current HashingEmbedder output and the SHIPPED artifact
        // name (vectors-e02.bin). Not rebuilding the vectors now — this only relabels finalize's output so a
        // re-run matches what the app/server already expect.
        let embeddingVersion = "e02"

        let outDir = try args.require("--out-dir")
        let allRecords: [IndexRecord] = try FileIO.readLines(Layout.labelsStore(outDir)).map { try JSON.decode($0) }
        let allRows: [VectorRow] = try FileIO.readLines(Layout.vectorsStore(outDir)).map { try JSON.decode($0) }
        guard allRecords.count == allRows.count else {
            throw ToolError(message: "store misaligned: \(allRecords.count) labels vs \(allRows.count) vectors")
        }
        // De-dup by (mediaType, tmdbId) keeping the LAST occurrence — a targeted re-pass (assemble --force)
        // appends superseding records, and finalize keeps the newest while preserving aligned vectors.
        var lastIndex: [String: Int] = [:]
        for (i, r) in allRecords.enumerated() { lastIndex["\(r.mediaType):\(r.tmdbId)"] = i }
        let keep = Set(lastIndex.values)
        let records = allRecords.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let rows = allRows.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let vectors: [[Int8]] = rows.map { $0.v.map { Int8(clamping: $0) } }

        let taxonomyVersion = Taxonomy.current.version
        let labels = LabelsArtifact(taxonomyVersion: taxonomyVersion, records: records)
        let labelsBlob = try JSON.encodeSorted(labels)
        if let s = String(data: labelsBlob, encoding: .utf8), s.contains("overview") {
            throw ToolError(message: "REFUSING to ship: raw 'overview' text found in labels artifact")
        }
        let labelsPath = Layout.labelsArtifact(outDir, taxonomyVersion)
        let vectorsPath = Layout.vectorsArtifact(outDir, embeddingVersion)
        let vectorsData = vectorsBlob(vectors)
        try FileIO.write(labelsBlob, to: labelsPath)
        try FileIO.write(vectorsData, to: vectorsPath)

        // Fold in import-dataset.mjs: the manifest + gzipped labels the Rust server serves.
        let dims = vectors.first?.count ?? 0
        let labelsSha = sha256Hex(labelsBlob)
        let vectorsSha = sha256Hex(vectorsData)
        let datasetVersion = String(sha256Hex(Data("\(labelsSha):\(vectorsSha)".utf8)).prefix(12))
        let now = Date()
        let labelsGzPath = try Shell.gzip(labelsPath)   // labels-<tax>.json.gz beside the labels blob
        let meta = DatasetMeta(
            datasetVersion: datasetVersion,
            taxonomyVersion: taxonomyVersion,
            embeddingModel: embeddingVersion,
            dims: dims,
            count: records.count,
            quantization: "int8-symmetric-x127",
            labelsFile: (labelsPath as NSString).lastPathComponent,
            vectorsFile: (vectorsPath as NSString).lastPathComponent,
            labelsGzFile: (labelsGzPath as NSString).lastPathComponent,
            labelsSha256: labelsSha,
            labelsBytes: labelsBlob.count,
            vectorsSha256: vectorsSha,
            vectorsBytes: vectorsData.count,
            builtAt: DateFmt.iso8601(now),
            lastModifiedHttp: DateFmt.rfc1123(now))
        try JSON.writePretty(meta, to: Layout.datasetMeta(outDir))

        var report = RunReport()
        report.processed = records.count
        for record in records {
            report.byPrimaryGenre[record.primaryGenre, default: 0] += 1
            for item in record.subgenres + record.moods {
                report.confidenceHistogram[confidenceBucket(item.confidence), default: 0] += 1
            }
        }
        if let enrichCk: EnrichCheckpoint = try? JSON.read(Layout.enrichCheckpoint(outDir)) {
            report.skippedBelowVoteFloor = enrichCk.totals.belowFloor
            report.fetchFailures = enrichCk.totals.failures
        }
        let extra = ReportExtras(
            anime: (try? JSON.read(Layout.enrichCheckpoint(outDir)) as EnrichCheckpoint)?.totals.anime ?? 0,
            noPrimary: (try? JSON.read(Layout.classifyCheckpoint(outDir)) as ClassifyCheckpoint)?.totals.noPrimary ?? 0,
            report: report)
        try JSON.writePretty(extra, to: Layout.report(outDir))

        print("finalize: \(records.count) titles · labels=\(labelsPath) vectors=\(vectorsPath) meta=\(Layout.datasetMeta(outDir)) dataset=\(datasetVersion)")
        print("primary-genre dist: \(report.byPrimaryGenre.sorted { $0.value > $1.value }.map { "\($0.key):\($0.value)" }.joined(separator: " "))")
    }

    // score — labels vs the golden set: primary-genre accuracy, multi-label F1, per-family precision. The
    // gate (genres/blended ≥0.90, moods ≥0.75) — with --gate, a miss exits non-zero (fail the run).
    static func score(_ args: Args) throws {
        let labelsPath = try args.require("--labels")
        let goldenPath = try args.require("--golden")
        let records = try loadRecords(labelsPath)
        let golden: GoldenSet = try JSON.read(goldenPath)

        // Key by (mediaType, tmdbId): TMDB reuses ids across film/TV, so a bare-id key collides on a
        // mixed-media golden (movie 1781 ≠ tv 1781).
        func key(_ media: String, _ id: Int) -> Int { (media == "tv" ? 10_000_000_000 : 0) + id }
        let byID = Dictionary(records.map { (key($0.mediaType, $0.tmdbId), $0) }, uniquingKeysWith: { a, _ in a })
        let covered = golden.titles.filter { byID[key($0.mediaType, $0.tmdbId)] != nil }
        guard !covered.isEmpty else { throw ToolError(message: "no golden titles present in \(labelsPath) — nothing to score") }

        let goldenLabels = Dictionary(covered.map { (key($0.mediaType, $0.tmdbId), $0.labels) }, uniquingKeysWith: { a, _ in a })
        let predictedLabels = Dictionary(covered.map { g -> (Int, Set<String>) in
            let r = byID[key(g.mediaType, g.tmdbId)]!
            return (key(g.mediaType, g.tmdbId), Set(r.subgenres.map(\.label) + r.moods.map(\.label)))
        }, uniquingKeysWith: { a, _ in a })
        let goldenPrimary = Dictionary(covered.map { (key($0.mediaType, $0.tmdbId), $0.primaryGenre) }, uniquingKeysWith: { a, _ in a })
        let predictedPrimary = Dictionary(covered.map { (key($0.mediaType, $0.tmdbId), byID[key($0.mediaType, $0.tmdbId)]!.primaryGenre) }, uniquingKeysWith: { a, _ in a })

        let f1 = TaxonomyScorer.score(golden: goldenLabels, predicted: predictedLabels)
        let primaryAcc = TaxonomyScorer.primaryGenreAccuracy(golden: goldenPrimary, predicted: predictedPrimary)
        let tax = Taxonomy.current

        // Per-family precision AND recall at the current acceptance thresholds — the table used to set the
        // precision knee (DT-C). Recall = tp/(tp+fn); fn is golden labels the index missed at this cutoff.
        func familyStats(_ labels: [String]) -> (p: Double, r: Double, tp: Int, fp: Int, fn: Int)? {
            let set = Set(labels)
            let scores = f1.perLabel.filter { set.contains($0.key) }.values
            let tp = scores.reduce(0) { $0 + $1.truePositives }
            let fp = scores.reduce(0) { $0 + $1.falsePositives }
            let fn = scores.reduce(0) { $0 + $1.falseNegatives }
            guard tp + fp + fn > 0 else { return nil }
            let p = tp + fp > 0 ? Double(tp) / Double(tp + fp) : 0
            let r = tp + fn > 0 ? Double(tp) / Double(tp + fn) : 0
            return (p, r, tp, fp, fn)
        }

        print("=== golden score (\(covered.count)/\(golden.titles.count) covered, taxonomy \(golden.taxonomyVersion)) ===")
        print(String(format: "primary-genre accuracy: %.3f", primaryAcc))
        print(String(format: "multi-label  micro-F1: %.3f   macro-F1: %.3f", f1.microF1, f1.macroF1))

        let families: [(String, [String], Double)] = [
            ("blended (subgenres)", tax.subgenres, 0.90),
            ("thematic", tax.thematic, 0.90),
            ("moods", tax.moods, 0.75),
        ]
        var gateFailed = false
        for (name, labels, target) in families {
            if let s = familyStats(labels) {
                let miss = s.p < target
                gateFailed = gateFailed || miss
                print(String(format: "family %-20@ precision %.3f  recall %.3f  F1 %.3f (tp=%d fp=%d fn=%d) target P≥%.2f%@",
                             name as NSString, s.p, s.r,
                             (s.p + s.r) > 0 ? 2 * s.p * s.r / (s.p + s.r) : 0,
                             s.tp, s.fp, s.fn, target, miss ? "  ✗ MISS" : "  ✓"))
            } else {
                print("family \(name): no predictions (n/a)")
            }
        }
        // Primary genre is its own family — accuracy is its precision (single-label).
        let primaryMiss = primaryAcc < 0.90
        gateFailed = gateFailed || primaryMiss
        print(String(format: "family %-20@ accuracy  %.3f target 0.90%@",
                     "primary-genre" as NSString, primaryAcc, primaryMiss ? "  ✗ MISS" : "  ✓"))

        if args.has("--gate") && gateFailed {
            FileHandle.standardError.write(Data("gate FAILED: a family missed its precision target\n".utf8))
            exit(3)
        }
    }

    // MARK: - helpers

    private static func loadRecords(_ path: String) throws -> [IndexRecord] {
        if path.hasSuffix(".jsonl") {
            return try FileIO.readLines(path).map { try JSON.decode($0) }
        }
        let artifact: LabelsArtifact = try JSON.read(path)
        return artifact.records
    }

    private static func loadVotePasses(outDir: String, batchId: Int) throws -> [[Int: String]] {
        let dir = Layout.votesDir(outDir)
        let prefix = "batch-\(batchId)-pass"
        let files = (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []
        let passFiles = files.filter { $0.hasPrefix(prefix) && $0.hasSuffix(".json") }.sorted()
        // A malformed pass (a Haiku subagent that returned prose/truncated JSON) is skipped + logged, not
        // fatal — the remaining passes still carry the vote. assemble fails only if NO pass parses.
        return passFiles.compactMap { file in
            let path = (dir as NSString).appendingPathComponent(file)
            guard let votes: [HaikuVote] = try? JSON.read(path) else {
                Log.append(Layout.enrichLog(outDir), "bad-vote-pass \(file) (unparseable JSON, skipped)")
                return nil
            }
            // tmdbId → the per-title JSON string the calibrated aggregation will parse.
            return Dictionary(votes.compactMap { vote -> (Int, String)? in
                guard let data = try? JSONEncoder().encode(vote), let s = String(data: data, encoding: .utf8) else { return nil }
                return (vote.tmdbId, s)
            }, uniquingKeysWith: { a, _ in a })
        }
    }
}

// MARK: - Anime filter (single authority; both worklist modes funnel through enrich)

/// TMDB keyword 210024 = "anime"; Japanese-language Animation is the catch-all. DT-taxonomy.md: **no anime**.
func isAnime(_ title: EnrichedTitle) -> Bool {
    if title.keywords.contains(where: { $0.id == 210024 }) { return true }
    if title.genreIDs.contains(16) && title.originalLanguage == "ja" { return true }
    return false
}

/// The embedding input (kept identical to BackfillPipeline): title + overview + keyword names.
func embeddingText(_ title: EnrichedTitle) -> String {
    ([title.title, title.overview] + title.keywords.map(\.name)).joined(separator: " ")
}

func confidenceBucket(_ confidence: Double) -> String {
    let low = (confidence * 10).rounded(.down) / 10
    return String(format: "%.1f-%.1f", low, low + 0.1)
}

func vectorsBlob(_ vectors: [[Int8]]) -> Data {
    var data = Data()
    let dim = vectors.first?.count ?? 0
    var count = Int32(vectors.count).littleEndian
    var dimension = Int32(dim).littleEndian
    withUnsafeBytes(of: &count) { data.append(contentsOf: $0) }
    withUnsafeBytes(of: &dimension) { data.append(contentsOf: $0) }
    for row in vectors { data.append(contentsOf: row.map { UInt8(bitPattern: $0) }) }
    return data
}

/// Bridge the async embedder to the synchronous assemble loop (HashingEmbedder is pure CPU; no await needed
/// in practice, but the protocol is async). Runs the embedding on a transient semaphore-gated task.
func blockingEmbed(_ embedder: any Embedder, _ text: String) -> [Float] {
    let box = ResultBox()
    let semaphore = DispatchSemaphore(value: 0)
    Task {
        box.value = (try? await embedder.embed(text)) ?? []
        semaphore.signal()
    }
    semaphore.wait()
    return box.value
}

final class ResultBox: @unchecked Sendable { var value: [Float] = [] }

/// No-op LLM — `assemble` constructs a `TaxonomyClassifier` only for its calibrated aggregation
/// (`classify(rawVotes:)`), which never calls the LLM. This stub satisfies the initializer.
struct NoLLM: LLMClient {
    func complete(_ request: LLMRequest) async throws -> String {
        throw ToolError(message: "NoLLM: classification comes from Haiku subagents, not an API")
    }
}

// MARK: - Hashing / gzip / dates (import-dataset.mjs fold-in)

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

/// The manifest the Rust server reads (former import-dataset.mjs output). Field names are the JSON keys.
struct DatasetMeta: Codable {
    let datasetVersion: String
    let taxonomyVersion: String
    let embeddingModel: String
    let dims: Int
    let count: Int
    let quantization: String
    let labelsFile: String
    let vectorsFile: String
    let labelsGzFile: String
    let labelsSha256: String
    let labelsBytes: Int
    let vectorsSha256: String
    let vectorsBytes: Int
    let builtAt: String
    let lastModifiedHttp: String
}

enum DateFmt {
    static func iso8601(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }
    static func rfc1123(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "GMT")
        f.dateFormat = "EEE, dd MMM yyyy HH:mm:ss 'GMT'"
        return f.string(from: date)
    }
}

enum Shell {
    /// gzip a file to `<path>.gz` (keeps the original), returning the .gz path. Shelling to /usr/bin/gzip is
    /// the simplest way to a real gzip container from Foundation (Compression's zlib codec isn't gzip-framed).
    @discardableResult
    static func gzip(_ path: String) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/gzip")
        process.arguments = ["-kf", path]
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw ToolError(message: "gzip failed (\(process.terminationStatus)) for \(path)")
        }
        return path + ".gz"
    }
}

// MARK: - DTOs

struct WLEntry: Codable {
    let tmdbId: Int
    let mediaType: String
    var media: MediaType { mediaType == "tv" ? .tv : .movie }
    init(tmdbId: Int, mediaType: String) { self.tmdbId = tmdbId; self.mediaType = mediaType }
    init(_ e: WorklistEntry) { tmdbId = e.tmdbId; mediaType = e.mediaType.rawValue }
}

/// The scratch enriched record (holds raw TMDB text → never shipped; gitignored). Captures the full
/// EnrichedTitle so `assemble` can rebuild it for grounding, plus the human-readable fields Haiku reads.
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

    init(_ t: EnrichedTitle) {
        tmdbId = t.tmdbId; mediaType = t.mediaType.rawValue; title = t.title; year = t.year
        overview = t.overview; genreIDs = t.genreIDs; genres = t.genreNames
        keywordIDs = t.keywords.map(\.id); keywords = t.keywords.map(\.name)
        originCountry = t.originCountry; originalLanguage = t.originalLanguage; voteCount = t.voteCount
    }

    func toEnrichedTitle() -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType == "tv" ? .tv : .movie, title: title, year: year,
                      overview: overview, genreIDs: genreIDs, genreNames: genres,
                      keywords: zip(keywordIDs, keywords).map { Keyword(id: $0, name: $1) },
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount)
    }
}

/// One Haiku subagent's label call for a title (its vote-pass output). Matches DT-classification-prompt.md.
struct HaikuVote: Codable {
    let tmdbId: Int
    let primaryGenre: String?
    let subgenres: [Label]?
    let moods: [Label]?
    enum CodingKeys: String, CodingKey { case tmdbId, primaryGenre = "primary_genre", subgenres, moods }

    /// A label + confidence — decoded leniently because a small fraction of Haiku passes emit a label as a
    /// bare string (`"Heist"`) or omit the confidence, instead of `{"label":…,"confidence":…}`. Rather than
    /// let one off-schema item reject the whole pass (→ everything spuriously escalates), accept both shapes
    /// with a neutral default confidence. The calibrated aggregation still thresholds across the passes.
    struct Label: Codable {
        let label: String
        let confidence: Double
        static let defaultConfidence = 0.7

        init(from decoder: any Decoder) throws {
            if let bare = try? decoder.singleValueContainer().decode(String.self) {
                label = bare; confidence = Self.defaultConfidence; return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = try c.decode(String.self, forKey: .label)
            confidence = (try? c.decode(Double.self, forKey: .confidence)) ?? Self.defaultConfidence
        }
        func encode(to encoder: any Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(label, forKey: .label); try c.encode(confidence, forKey: .confidence)
        }
        enum CodingKeys: String, CodingKey { case label, confidence }
    }
}

struct VectorRow: Codable { let tmdbId: Int; let v: [Int] }

enum EnrichOutcome {
    case ok(EnrichedDTO)
    case belowFloor(Int)
    case anime(Int)
    case noOverview(Int)
    case failure(Int, String)
}

struct EnrichCheckpoint: Codable {
    var processed: Set<Int> = []
    var nextBatch: Int = 1
    var totals = Totals()
    struct Totals: Codable {
        var belowFloor = 0, anime = 0, failures = 0, noOverview = 0
        init() {}
        // Tolerant decode: a checkpoint written before a field existed must still load (Swift's synthesized
        // Decodable requires every key, so a new field would otherwise reset the whole checkpoint → silent
        // re-enrich from scratch). decodeIfPresent + default keeps old checkpoints valid across field adds.
        init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            belowFloor = try c.decodeIfPresent(Int.self, forKey: .belowFloor) ?? 0
            anime = try c.decodeIfPresent(Int.self, forKey: .anime) ?? 0
            failures = try c.decodeIfPresent(Int.self, forKey: .failures) ?? 0
            noOverview = try c.decodeIfPresent(Int.self, forKey: .noOverview) ?? 0
        }
        mutating func merge(belowFloor: Int, anime: Int, failures: Int, noOverview: Int) {
            self.belowFloor += belowFloor; self.anime += anime; self.failures += failures
            self.noOverview += noOverview
        }
    }
}

struct ClassifyCheckpoint: Codable {
    var done: Set<Int> = []
    var totals = Totals()
    struct Totals: Codable {
        var noPrimary = 0, missingVotes = 0
        mutating func merge(noPrimary: Int, missingVotes: Int) {
            self.noPrimary += noPrimary; self.missingVotes += missingVotes
        }
    }
}

struct ReportExtras: Codable {
    let anime: Int
    let noPrimary: Int
    let report: RunReport
}

// MARK: - Paths

enum Layout {
    static func enrichCheckpoint(_ dir: String) -> String { join(dir, "enrich-checkpoint.json") }
    static func classifyCheckpoint(_ dir: String) -> String { join(dir, "classify-checkpoint.json") }
    static func enrichLog(_ dir: String) -> String { join(dir, "enrich-log.txt") }
    static func enrichedBatch(_ dir: String, _ id: Int) -> String { join(dir, "enriched/batch-\(id).json") }
    static func escalateBatch(_ dir: String, _ id: Int) -> String { join(dir, "escalate/batch-\(id).json") }
    static func votesDir(_ dir: String) -> String { join(dir, "votes") }
    static func votePass(_ dir: String, _ id: Int, _ pass: Int) -> String { join(dir, "votes/batch-\(id)-pass\(pass).json") }
    static func labelsStore(_ dir: String) -> String { join(dir, "index/labels.jsonl") }
    static func vectorsStore(_ dir: String) -> String { join(dir, "index/vectors.jsonl") }
    static func labelsArtifact(_ dir: String, _ v: String) -> String { join(dir, "labels-\(v).json") }
    static func vectorsArtifact(_ dir: String, _ v: String) -> String { join(dir, "vectors-\(v).bin") }
    static func datasetMeta(_ dir: String) -> String { join(dir, "dataset.meta.json") }
    static func report(_ dir: String) -> String { join(dir, "report.json") }
    static func join(_ dir: String, _ rel: String) -> String { (dir as NSString).appendingPathComponent(rel) }
}

// MARK: - TMDB

enum TMDB {
    static func client() throws -> TMDBClient {
        guard let key = ProcessInfo.processInfo.environment["TMDB_API_KEY"], !key.isEmpty else {
            throw ToolError(message: "set TMDB_API_KEY (enrichment requires it)")
        }
        return TMDBClient(apiKey: key, maxConcurrent: 8)
    }
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
    static func write<T: Encodable>(_ value: T, to path: String) throws {
        try FileIO.write(try JSONEncoder().encode(value), to: path)
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
        try data.write(to: URL(fileURLWithPath: path))
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

enum Log {
    static func append(_ path: String, _ message: String) {
        try? FileIO.ensureParent(path)
        if let data = (message + "\n").data(using: .utf8) {
            if let handle = try? FileHandle(forWritingTo: URL(fileURLWithPath: path)) {
                handle.seekToEndOfFile(); handle.write(data); try? handle.close()
            } else {
                try? data.write(to: URL(fileURLWithPath: path))
            }
        }
    }
}

// MARK: - Args

struct Args {
    private var map: [String: String] = [:]
    private var flags: Set<String> = []
    init(_ argv: [String]) {
        var index = 0
        while index < argv.count {
            let key = argv[index]
            guard key.hasPrefix("--") else { index += 1; continue }
            if index + 1 < argv.count, !argv[index + 1].hasPrefix("--") {
                map[key] = argv[index + 1]; index += 2
            } else { flags.insert(key); index += 1 }
        }
    }
    subscript(_ key: String) -> String? { map[key] }
    func has(_ key: String) -> Bool { flags.contains(key) || map[key] != nil }
    func int(_ key: String) -> Int? { map[key].flatMap { Int($0) } }
    func double(_ key: String) -> Double? { map[key].flatMap { Double($0) } }
    func require(_ key: String) throws -> String {
        guard let value = map[key] else { throw ToolError(message: "missing \(key)") }
        return value
    }
    func requireInt(_ key: String) throws -> Int {
        guard let value = int(key) else { throw ToolError(message: "missing/invalid \(key)") }
        return value
    }
}
