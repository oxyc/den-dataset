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
            case "assemble": try await Commands.assemble(args)
            case "embed-corpus": try await Commands.embedCorpus(args)
            case "finalize": try Commands.finalize(args)
            case "metadata": try await Commands.metadata(args)
            case "score":    try Commands.score(args)
            case "recluster": try Commands.recluster(args)
            default: usage(); exit(2)
            }
        } catch let error as ToolError {
            FileHandle.standardError.write(Data(Redact.secrets("error: \(error.message)\n").utf8)); exit(1)
        } catch {
            FileHandle.standardError.write(Data(Redact.secrets("error: \(error)\n").utf8)); exit(1)
        }
    }

    static func usage() {
        FileHandle.standardError.write(Data("""
        usage: taxonomy-backfill <command> [flags]
          worklist --mode discover|export|delta --media movie|tv [--count N] [--vote-floor 50] [--file export.json] --out <path>
                   delta:  --since YYYY-MM-DD [--known <labels-tNN.json>]   (DT-F daily freshness pass)
          enrich   --worklist <path> [--vote-floor 50] [--limit 150] --out-dir <dir>
          enrich-ids --ids <a,b,c> --media movie|tv --out-dir <dir>   (targeted re-enrich)
          escalation --batch-id <n> --out-dir <dir>   (after pass 1: emit titles needing n=3)
          assemble --batch-id <n> --out-dir <dir>
          embed-corpus --labels <existing labels-t02.json> --out-dir <dir> [--enriched-dir <dir>]
                       [--chunk 15] [--plot-cap 1500] [--limit N]
                       (--chunk is bounded by den-embed's per-request token budget: 8192 / --plot-cap's
                        token cost. Above it every request is a 413, which is not retried.)
          finalize --out-dir <dir>
          metadata --out-dir <dir> [--skip-fetch] [--limit N]
                   (the poster sidecar; its filename carries the datasetVersion, so run it after EVERY
                    finalize that changed the corpus, before publishing)
          score    --labels <labels.jsonl|labels-t02.json> --golden <golden.json> [--gate]
          recluster --labels <labels-tNN.json> --vectors <vectors-eNN.bin> [--k 200] [--iterations 8]
                    [--min-size 25] [--max-purity 0.35] [--min-cohesion 0.55] --out <report.json>  (DT-F weekly)

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
        // DT-F — the daily freshness pass: titles released since `--since` that clear the vote floor and are
        // NOT already in the published labels.
        //
        // Uses `discover` with a release-date window rather than `/movie/changes`. `/changes` is a firehose of
        // ids with no vote or date signal, so it costs one detail lookup PER id just to discover that almost
        // all of them are below the floor. A dated `vote_count.desc` slice selects the same titles in a few
        // paged calls. The trade: a re-release or a late metadata fix on an OLD title won't be picked up —
        // acceptable, because `assemble` re-reads whatever is in the worklist and a periodic full pass covers
        // drift, whereas paying per-id daily does not scale.
        case "delta":
            let since = try args.require("--since")
            let floor = args.int("--vote-floor") ?? 50
            let tmdb = try TMDB.client()
            // Titles already published are skipped: the point of a delta is to classify what is NEW, and
            // re-running the catalogue daily is exactly the cost this pass exists to avoid.
            var known = Set<Int>()
            if let knownPath = args["--known"] {
                struct KnownLabels: Decodable {
                    struct Record: Decodable { let tmdbId: Int; let mediaType: String }
                    let records: [Record]
                }
                let labels: KnownLabels = try JSON.read(knownPath)
                known = Set(labels.records.filter { $0.mediaType == mediaType.rawValue }.map(\.tmdbId))
            }
            var seen = Set<Int>()
            var page = 1
            while page <= 500 {
                let query = DiscoverQuery(mediaType: mediaType, voteCountGte: floor,
                                          releaseDateGte: since, sortBy: "vote_count.desc")
                let result = try await tmdb.discover(query, page: page)
                for item in result.items where seen.insert(item.tmdbID.rawValue).inserted {
                    guard !known.contains(item.tmdbID.rawValue) else { continue }
                    entries.append(WLEntry(tmdbId: item.tmdbID.rawValue, mediaType: mediaType.rawValue))
                }
                if page >= result.totalPages { break }
                page += 1
            }
            FileHandle.standardError.write(Data(
                "delta: \(entries.count) new title(s) since \(since) at vote-floor \(floor) (skipped \(known.count) known)\n".utf8))
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
        // Distinguish "absent (first run)" from "present but corrupt (resume state)": a bare `try?` would
        // silently reset a truncated checkpoint to empty and re-enrich the whole universe. Fail loudly instead.
        let ckPath = Layout.enrichCheckpoint(outDir)
        var checkpoint: EnrichCheckpoint
        if FileManager.default.fileExists(atPath: ckPath) {
            do { checkpoint = try JSON.read(ckPath) } catch {
                throw ToolError(message: "enrich checkpoint at \(ckPath) is unreadable (\(error)); refusing to "
                    + "reset progress — restore it, or delete it to intentionally start fresh")
            }
        } else {
            checkpoint = EnrichCheckpoint()
        }
        let pending = worklist.filter { !checkpoint.processed.contains(EnrichCheckpoint.key($0.media, $0.tmdbId)) }.prefix(limit)
        guard !pending.isEmpty else {
            print(JSON.line(["remaining": 0, "count": 0])); return
        }

        let tmdb = try TMDB.client()
        let batchId = checkpoint.nextBatch
        var titles: [EnrichedTitle] = []
        var belowFloor = 0, anime = 0, failures = 0, noOverview = 0
        // Ids whose failure was TRANSIENT (429/5xx/timeout, retries already exhausted in transport). These are
        // NOT checkpointed, so the next run retries them — rather than permanently dropping a title on a blip.
        var deferred = Set<Int>()

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
                        return .ok(title)
                    } catch {
                        // Transient → defer (retry next run); definitive (404/decoding) → a real dead id, drop.
                        return Transport.isRetryable(error)
                            ? .transientFailure(entry.tmdbId, "\(error)")
                            : .failure(entry.tmdbId, "\(error)")
                    }
                }
            }
            for try await outcome in group {
                switch outcome {
                case .ok(let title): titles.append(title)
                case .belowFloor: belowFloor += 1
                case .anime: anime += 1
                case .noOverview: noOverview += 1
                case .failure(let id, let reason):
                    failures += 1
                    Log.append(Layout.enrichLog(outDir), "fetch-failure id=\(id) \(reason)")
                case .transientFailure(let id, let reason):
                    deferred.insert(id)
                    Log.append(Layout.enrichLog(outDir), "fetch-deferred id=\(id) (transient: \(reason))")
                }
            }
        }

        // FP-2 — re-ground on Wikipedia: ONE Wikidata SPARQL maps the surviving ids to their enwiki articles,
        // then each title's plot is fetched live. Where a plot exists it REPLACES the TMDB overview (ToS-clean
        // grounding for the Haiku classifier); titles keep the TMDB overview only where Wikipedia has no plot.
        let media: MediaType = pending.first?.media ?? .movie
        // A transient-exhausted Wikidata failure aborts the batch (nothing written/checkpointed) so the whole
        // batch retries — far better than silently marking all its titles tags-only. A *successful* SPARQL with
        // an id simply absent from the results is a definitive no-article (that title stays tags-only).
        let mapping: [Int: WikipediaSource.Mapping]
        do {
            mapping = try await WikipediaSource().wikidata(forTMDBIds: titles.map(\.tmdbId), mediaType: media)
        } catch {
            throw ToolError(message: "Wikidata mapping failed for batch \(batchId) after retries (\(error)); "
                + "nothing written — re-run to retry this batch")
        }

        var withPlot = 0
        var grounded: [EnrichedTitle] = []
        for outcome in try await regroundOnWikipedia(titles, mapping: mapping, log: Layout.enrichLog(outDir)) {
            switch outcome {
            case .grounded(let title): grounded.append(title); withPlot += 1
            case .noPlot(let title): grounded.append(title)
            case .deferred(let id): deferred.insert(id)   // transient plot fetch — retry next run, don't checkpoint
            }
        }
        var survivors = grounded.map(EnrichedDTO.init)

        survivors.sort { $0.tmdbId < $1.tmdbId }
        try JSON.writePretty(survivors, to: Layout.enrichedBatch(outDir, batchId))
        // Checkpoint every pending id EXCEPT the deferred (transient) ones — those stay pending for a retry.
        for entry in pending where !deferred.contains(entry.tmdbId) {
            checkpoint.processed.insert(EnrichCheckpoint.key(entry.media, entry.tmdbId))
        }
        checkpoint.nextBatch += 1
        checkpoint.totals.merge(belowFloor: belowFloor, anime: anime, failures: failures, noOverview: noOverview)
        try JSON.write(checkpoint, to: Layout.enrichCheckpoint(outDir))

        // Per-media remaining (the shared checkpoint also holds the other media's keys).
        let remaining = worklist.filter { !checkpoint.processed.contains(EnrichCheckpoint.key($0.media, $0.tmdbId)) }.count
        print(JSON.line([
            "batchId": batchId, "count": survivors.count, "belowFloor": belowFloor,
            "anime": anime, "noOverview": noOverview, "failures": failures, "deferred": deferred.count,
            "remaining": remaining, "wikiPlot": withPlot, "tagsOnly": survivors.count - withPlot,
            "batch": Layout.enrichedBatch(outDir, batchId),
        ]))
    }

    /// The per-title result of the Wikipedia plot hop: grounded on a real plot, a definitive no-plot (kept on
    /// the TMDB overview), or deferred because the fetch failed transiently (retry next run — don't checkpoint).
    enum PlotOutcome {
        case grounded(EnrichedTitle)
        case noPlot(EnrichedTitle)
        case deferred(Int)
    }

    /// Minimum plot length to re-ground on (chars). Below this, a "Plot" section is a bare one-line logline that
    /// adds little grounding over the TMDB overview it would replace — keep the title tags-only instead.
    static let wikiPlotFloor = 200

    /// Fetch each title's live Wikipedia plot (bounded concurrency) and classify the outcome. A missing mapping
    /// or a plot section that is absent / below the floor is a definitive `noPlot`; a transient fetch failure
    /// (429/5xx/timeout, retries exhausted) is `deferred` so the id is retried on the next run.
    static func regroundOnWikipedia(_ titles: [EnrichedTitle], mapping: [Int: WikipediaSource.Mapping],
                                    log: String) async throws -> [PlotOutcome] {
        let wiki = WikipediaSource()
        let gate = 4   // gentle on the public Wikipedia API
        var out: [PlotOutcome] = []
        var index = 0
        while index < titles.count {
            let slice = Array(titles[index..<min(index + gate, titles.count)])
            let outcomes = try await withThrowingTaskGroup(of: PlotOutcome.self) { group -> [PlotOutcome] in
                for title in slice {
                    group.addTask {
                        guard let article = mapping[title.tmdbId]?.article else { return .noPlot(title) }
                        do {
                            guard let plot = try await wiki.plot(articleTitle: article),
                                  plot.count >= wikiPlotFloor else { return .noPlot(title) }
                            return .grounded(title.groundedOnWikiPlot(plot))
                        } catch {
                            if Transport.isRetryable(error) {
                                Log.append(log, "plot-deferred id=\(title.tmdbId) (transient: \(error))")
                                return .deferred(title.tmdbId)
                            }
                            // Definitive (e.g. 404 on a stale sitelink) — keep the title on its TMDB overview.
                            Log.append(log, "plot-miss id=\(title.tmdbId) (\(error))")
                            return .noPlot(title)
                        }
                    }
                }
                var acc: [PlotOutcome] = []
                for try await outcome in group { acc.append(outcome) }
                return acc
            }
            out.append(contentsOf: outcomes)
            index += gate
        }
        return out
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
        // Small chunk by default: den-embed activation memory scales with the batch, so keep requests modest.
        let chunk = args.int("--chunk") ?? 16
        let limit = args.int("--limit")                          // optional cap (testing)
        // Cap the PLOT portion (facts + tags are always kept). 4000 chars keeps the median plot whole and every
        // mid-plot genre pivot the length audit found, dropping only low-value end-of-plot twist tails — the
        // knee between similarity quality and bge-m3's O(seq^2) embedding cost.
        // 1500, matching `assemble`: both append to the SAME store and must compose comparable documents,
        // and 4000 + facts cannot fit any token cap den-embed will accept (its ceiling is 1024 tokens, so
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
        let embedder = try await recordEmbedder(outDir: outDir, client: denEmbed, plotCap: plotCap)
        FileHandle.standardError.write(Data("  embedder: \(embedder.label)\n".utf8))

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

        func flush() async throws {
            guard !buffer.isEmpty else { return }
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
        }

        // Stream the enriched batch files one at a time — only ONE batch of plots is in memory at once.
        let files = ((try? FileManager.default.contentsOfDirectory(atPath: enrichedDir)) ?? [])
            .filter { $0.hasPrefix("batch-") && $0.hasSuffix(".json") }.sorted()
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
                buffer.append((record, ComposedDoc.build(title: title, tags: tags, plot: plot)))
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
    /// (raise DEN_EMBED_MAX_TOKENS and retry) hit the mismatch guard instead, on a store with zero rows,
    /// and the operator had to know to delete index/embedder.json by hand.
    @discardableResult
    static func recordEmbedder(outDir: String, client: DenEmbedClient,
                               plotCap: Int) async throws -> DenEmbedClient.Identity {
        let path = Layout.embedderIdentity(outDir)
        let now = try await client.identity()
        // Identity first: on a service upgrade "this would mix two embedders" is the finding that matters,
        // and leading with the plot-cap error sent the operator off to fix the lesser one.
        if let previous: DenEmbedClient.Identity = try? JSON.read(path) {
            if previous != now {
                throw ToolError(message: "this store was embedded by \(previous.label) but den-embed now "
                    + "reports \(now.label) — appending would mix two embedders into one corpus. Either "
                    + "restore the previous service, or start a fresh --out-dir and re-embed from scratch.")
            }
            try assertDocFits(plotCap: plotCap, embedder: now)
            return now
        }
        // A store with rows but no recorded identity predates this check, and adopting the CURRENT service as
        // its identity would write a guess down as a fact — the guard would then pass forever on the one
        // corpus that actually has the problem. The shipped store is exactly that case: 37.5k rows embedded
        // by the Python/ORT-1.22 service, which this one does not match.
        if FileManager.default.fileExists(atPath: Layout.labelsStore(outDir)),
           !(try FileIO.readLines(Layout.labelsStore(outDir))).isEmpty {
            throw ToolError(message: "\(outDir) holds an existing store but no \(path), so what embedded it "
                + "is unknown and appending \(now.label) may mix two embedders. Write that file with the "
                + "identity that built it — a corpus from before the Rust rewrite is "
                + #"{"model":"bge-m3","dims":1024,"runtime":"pre-3.0.0","maxTokens":0}"#
                + " — or start a fresh --out-dir.")
        }
        try assertDocFits(plotCap: plotCap, embedder: now)
        try FileIO.ensureParent(path)
        try JSON.writePretty(now, to: path)
        return now
    }

    /// Refuse to compose documents the service will silently cut in half.
    ///
    /// den-embed truncates at `max_tokens` server-side, returns a normal-looking vector, and says nothing —
    /// no error, no field in the response. The Python service it replaced had no token cap at all, so the
    /// shipped corpus was embedded from documents of up to ~4000 plot chars while a re-embed today gets
    /// ~2000: half of every long plot dropped, uniformly and invisibly, across the whole corpus.
    ///
    /// Parity with the old corpus is NOT reachable by raising the cap. den-embed's ceiling is 1024 tokens
    /// because measured peak RSS is 1219 MB there and 1598 MB at 2048, against a 1536 MB cgroup — so the cap
    /// is real and the plot cap is what has to give. That is acceptable (the alignment that matters is
    /// corpus versus QUERY, and both go through this service) as long as it is a decision, not a surprise.
    ///
    /// ~4 chars per token for English prose. Compared against the configured cap rather than a sampled
    /// document on purpose: the answer must not depend on which title happens to be first.
    static func assertDocFits(plotCap: Int, embedder: DenEmbedClient.Identity) throws {
        guard embedder.maxTokens > 0 else { return }   // a service too old to report it
        let factsAndTags = 500          // the composed doc's non-plot half
        let budget = embedder.maxTokens * 4
        guard plotCap + factsAndTags <= budget else {
            throw ToolError(message: "--plot-cap \(plotCap) composes documents of roughly "
                + "\(plotCap + factsAndTags) chars, but \(embedder.label) truncates at \(embedder.maxTokens) "
                + "tokens (~\(budget) chars) and would cut them silently. Lower --plot-cap to "
                + "\(budget - factsAndTags) or below, or raise DEN_EMBED_MAX_TOKENS on the service — its "
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

    // assemble — one enriched batch + its Haiku vote passes → calibrated classification (reused, tested) →
    // embed + quantize → append to the index store. Opus never classifies; the judgment stays in DenDataset.
    static func assemble(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let batchId = try args.requireInt("--batch-id")
        let force = args.has("--force")   // re-process already-classified titles (targeted re-pass)
        // ToS: a no-Wikipedia-plot title was classified from the TMDB overview PROSE (the enrichment fallback),
        // so its labels derive from TMDB expressive text. TMDB's terms forbid that use — drop those titles from
        // the shipped index entirely. Their vectors were already plot-empty; here we skip the whole record.
        let requireWikiPlot = args.has("--require-wiki-plot")
        // Cap the embedded plot (assemble used the FULL ~6000-char overview → slow O(seq²) embeds + big RAM).
        // The setup/premise dominates the vector anyway; a cap speeds it up and bounds memory.
        let plotCap = args.int("--plot-cap") ?? 1500
        // Embedder: `den-embed` (default, FP-2 — bge-m3 int8[1024] via the service) or `fnv` (offline
        // HashingEmbedder fallback, e.g. for a network-free run/test). The composed doc feeds BOTH.
        let embedderKind = args["--embedder"] ?? "den-embed"
        let denEmbed = DenEmbedClient()
        let fnv = HashingEmbedder()
        // Same guard as embed-corpus: assemble appends to the SAME store, so it is just as able to mix two
        // generations of the service into one corpus. `--embedder fnv` is the offline fallback and has no
        // service to ask.
        if embedderKind != "fnv" {
            try await recordEmbedder(outDir: outDir, client: denEmbed, plotCap: plotCap)
        }

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
        // Loud on a corrupt checkpoint, like `enrich` twenty lines away — a silent reset here re-classifies
        // and re-embeds the whole out-dir, which is hours of den-embed time rather than a wrong answer, but
        // it should still be the operator's decision.
        let classifyCkPath = Layout.classifyCheckpoint(outDir)
        var classified: ClassifyCheckpoint
        if FileManager.default.fileExists(atPath: classifyCkPath) {
            do { classified = try JSON.read(classifyCkPath) } catch {
                throw ToolError(message: "classify checkpoint at \(classifyCkPath) is unreadable (\(error)); "
                    + "refusing to reset progress — restore it, or delete it to intentionally start fresh")
            }
        } else {
            classified = ClassifyCheckpoint()
        }
        // Repaired BEFORE the rebuild below reads it — embed-corpus does it in this order too. Reading
        // first would mark rows done that the repair is about to drop, and they would never be re-embedded.
        try reconcileStore(Layout.labelsStore(outDir), Layout.vectorsStore(outDir))

        // A legacy bare-Int checkpoint cannot say which media each id belonged to, so rebuild the set from
        // the labels store, which records mediaType per row. Titles that were classified but DROPPED
        // (noPrimary / no-wiki) have no store row and are re-processed once — CPU and an embed, no LLM
        // spend — which is the cost of recovering the 940 TV series the un-keyed set was hiding.
        if classified.needsMigration {
            var rebuilt: Set<String> = []
            if FileManager.default.fileExists(atPath: Layout.labelsStore(outDir)) {
                for line in try FileIO.readLines(Layout.labelsStore(outDir)) {
                    if let r: IndexRecord = try? JSON.decode(line) {
                        rebuilt.insert(ClassifyCheckpoint.key(r.mediaType, r.tmdbId))
                    }
                }
            }
            // The store is the only record of which media each legacy id was, so a rebuild that finds far
            // fewer than the checkpoint claimed means the store is missing or truncated — not that the work
            // was never done. Silently accepting it re-classifies and re-embeds the whole out-dir.
            if rebuilt.count * 2 < classified.legacyCount {
                throw ToolError(message: "the classify checkpoint records \(classified.legacyCount) titles "
                    + "but only \(rebuilt.count) are in \(Layout.labelsStore(outDir)), so migrating it "
                    + "would discard most of the run's progress. Restore the labels store, or delete the "
                    + "checkpoint to intentionally start fresh.")
            }
            FileHandle.standardError.write(Data(("  migrated the classify checkpoint to media-qualified "
                + "keys (\(rebuilt.count) from the labels store, was \(classified.legacyCount))\n").utf8))
            classified.done = rebuilt
            classified.needsMigration = false
        }
        // Opus-confirmed world-knowledge labels (DT-G title-recognition adjudication) that survive the vc gate.
        let wkConfirmed: [Int: [String]] = (try? JSON.read(Layout.wkConfirmed(outDir))) ?? [:]
        var noPrimary = 0, missingVotes = 0, droppedNoWiki = 0

        let labelsHandle = try FileIO.appender(Layout.labelsStore(outDir))
        let vectorsHandle = try FileIO.appender(Layout.vectorsStore(outDir))
        defer { try? labelsHandle.close(); try? vectorsHandle.close() }

        // Embeds are BATCHED (den-embed /embed/batch). The old per-title `embedInt8` was a network round-trip
        // each (~1/s → ~14h for the corpus); a chunked batch does the whole group at once (~20-30min). A title
        // is only marked done once its vector is actually flushed, so a crash never checkpoints an unwritten row.
        let denEmbedChunk = args.int("--chunk") ?? 8   // small: bge-m3 attention is O(batch·seq²) — big batches of
                                                       // long docs spike den-embed RAM (swap-thrash). Keep it low.
        var buffer: [(tmdbId: Int, record: IndexRecord, doc: String)] = []
        func flush() async throws {
            guard !buffer.isEmpty else { return }
            let vectors: [[Int8]] = embedderKind == "fnv"
                ? buffer.map { Quantizer.int8(blockingEmbed(fnv, $0.doc)) }
                : try await denEmbed.embedManyInt8(buffer.map(\.doc))
            guard vectors.count == buffer.count else {
                throw ToolError(message: "den-embed returned \(vectors.count) vectors for \(buffer.count) docs")
            }
            for (item, vector) in zip(buffer, vectors) {
                try labelsHandle.writeLine(JSON.encodeLine(item.record))
                try vectorsHandle.writeLine(JSON.encodeLine(VectorRow(tmdbId: item.tmdbId, v: vector.map(Int.init))))
                classified.done.insert(ClassifyCheckpoint.key(item.record.mediaType, item.tmdbId))
            }
            buffer.removeAll(keepingCapacity: true)
        }

        for dto in enriched where force || !classified.done.contains(ClassifyCheckpoint.key(dto.mediaType, dto.tmdbId)) {
            // One raw-JSON string per pass for this title (re-serialized) → the calibrated aggregation seam.
            let raws: [String] = passes.compactMap { $0[dto.tmdbId] }
            guard !raws.isEmpty else { missingVotes += 1; continue }
            let title = dto.toEnrichedTitle()
            if requireWikiPlot && !title.hasWikiPlot { droppedNoWiki += 1; continue }   // ToS: no TMDB-prose labels
            let confirmedWK = Set(wkConfirmed[dto.tmdbId] ?? [])
            guard let classification = classifier.classify(rawVotes: raws, title: title, confirmedWK: confirmedWK) else {
                noPrimary += 1
                classified.done.insert(ClassifyCheckpoint.key(dto.mediaType, dto.tmdbId))
                continue
            }
            // Compose the embedding doc from FACTS + the just-classified TAGS + the (Wikipedia) plot. A title
            // with no wiki plot composes on facts + tags with an empty Plot — never skipped.
            let tags = (classification.subgenres + classification.moods).map(\.label)
            let plot = title.hasWikiPlot ? Self.cappedPlot(title.overview, maxChars: plotCap) : ""
            let composed = ComposedDoc.build(title: title, tags: tags, plot: plot)
            // Queue for the next batched embed (den-embed returns the FINAL int8[1024]; do NOT re-quantize).
            let record = classification.indexRecord(animated: title.genreIDs.contains(16))   // TMDB genre 16
            buffer.append((dto.tmdbId, record, composed))
            if buffer.count >= denEmbedChunk { try await flush() }
        }
        try await flush()
        classified.totals.merge(noPrimary: noPrimary, missingVotes: missingVotes)
        try JSON.write(classified, to: Layout.classifyCheckpoint(outDir))
        print(JSON.line([
            "batchId": batchId, "classifiedTotal": classified.done.count,
            "noPrimary": noPrimary, "missingVotes": missingVotes, "droppedNoWiki": droppedNoWiki,
        ]))
    }

    /// Expected vector dimension for a known embedding label, or nil (skip the check) for an unrecognized one.
    static func expectedDims(forEmbeddingVersion version: String) -> Int? {
        if version.hasPrefix("bge-m3") { return 1024 }
        if version == "e02" || version.hasPrefix("fnv") { return 384 }
        return nil
    }

    // finalize — the index store → the shipped artifacts. DERIVED labels + quantized vectors ONLY; asserts no
    // raw TMDB text leaked in. Recomputes the run report (coverage + primary-genre dist + confidence buckets)
    // and folds in the former import-dataset.mjs step: dataset.meta.json (the manifest the Rust server reads)
    // + a gzipped copy of the labels blob.
    static func finalize(_ args: Args) throws {
        // FP-2: the shipped embedding is now bge-m3 (den-embed) → vectors-bge-m3.bin, with `dims` taken from
        // the ACTUAL vector length (1024). The earlier lexical builds shipped e02 (FNV, 384-dim); the app
        // re-syncs because FP-1 keys the on-device index on embeddingModel + dims. `--embedding-version`
        // overrides the label (e.g. an offline FNV run), but the default is the bge-m3 artifact name.
        let embeddingVersion = args["--embedding-version"] ?? "bge-m3"

        let outDir = try args.require("--out-dir")
        let allRecords: [IndexRecord] = try FileIO.readLines(Layout.labelsStore(outDir)).map { try JSON.decode($0) }
        let allRows: [VectorRow] = try FileIO.readLines(Layout.vectorsStore(outDir)).map { try JSON.decode($0) }
        guard allRecords.count == allRows.count else {
            throw ToolError(message: "store misaligned: \(allRecords.count) labels vs \(allRows.count) vectors")
        }
        // ...and that line i of each names the same title. The two stores are positionally zipped from here
        // on — nothing downstream carries the vector's own id — so equal counts with a shifted body ships a
        // corpus where every title holds someone else's vector, and looks perfectly healthy doing it.
        if let bad = StoreIntegrity.firstMisalignment(records: allRecords, rows: allRows) {
            throw ToolError(message: "store misaligned at line \(bad + 1): labels say tmdbId "
                + "\(allRecords[bad].tmdbId), vectors say \(allRows[bad].tmdbId) — refusing to ship. "
                + "Re-run embed-corpus, which reconciles the stores before appending.")
        }
        // De-dup by (mediaType, tmdbId) keeping the LAST occurrence — a targeted re-pass (assemble --force)
        // appends superseding records, and finalize keeps the newest while preserving aligned vectors.
        var lastIndex: [String: Int] = [:]
        for (i, r) in allRecords.enumerated() { lastIndex["\(r.mediaType):\(r.tmdbId)"] = i }
        let keep = Set(lastIndex.values)
        let records = allRecords.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let rows = allRows.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let vectors: [[Int8]] = rows.map { $0.v.map { Int8(clamping: $0) } }

        // Guard the mixed-embedder / mislabel footgun: every vector must share ONE length, and it must match the
        // dimension the --embedding-version label implies (bge-m3 = 1024, fnv/e02 = 384). A store assembled with
        // two embedders, or a blob labelled bge-m3 but holding 384-dim FNV content, would otherwise ship a
        // corrupt/lying artifact that the app's `data.count == 8 + count*dim` check silently drops to recipes.
        let dimsSeen = Set(vectors.map(\.count))
        guard dimsSeen.count == 1, let dim = dimsSeen.first, dim > 0 else {
            throw ToolError(message: "vectors have non-uniform length \(dimsSeen.sorted()) — a mixed-embedder "
                + "store; refusing to ship. Re-assemble the batches with a single embedder.")
        }
        if let expected = expectedDims(forEmbeddingVersion: embeddingVersion), expected != dim {
            throw ToolError(message: "embedding-version '\(embeddingVersion)' implies dim \(expected) but the "
                + "vectors are \(dim)-dim — mislabelled artifact; refusing to ship.")
        }

        // Written by whichever command embedded the store. Absent for a corpus built before it was recorded,
        // or by the offline FNV embedder — both legitimate, so this is carried through, not required.
        var embedder: DenEmbedClient.Identity? = nil
        if FileManager.default.fileExists(atPath: Layout.embedderIdentity(outDir)) {
            // Loudly, not `try?`: a malformed identity file silently turned the cross-check below into a
            // no-op AND dropped the identity from the manifest, with nothing said either way.
            do { embedder = try JSON.read(Layout.embedderIdentity(outDir)) } catch {
                throw ToolError(message: "\(Layout.embedderIdentity(outDir)) is unreadable (\(error)) — it "
                    + "records what embedded this store, so shipping without it would misdescribe the corpus")
            }
        }
        if let embedder, embedder.dims > 0, embedder.dims != dim {
            throw ToolError(message: "the store was embedded by \(embedder.label) but its vectors are "
                + "\(dim)-dim — refusing to ship a manifest that would misdescribe them.")
        }

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
        let dims = dim   // validated above: uniform + consistent with the embedding-version label
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
            lastModifiedHttp: DateFmt.rfc1123(now),
            embedderRuntime: embedder?.runtime,
            embedderMaxTokens: embedder?.maxTokens)
        try JSON.writeMeta(meta, to: Layout.datasetMeta(outDir))

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

    // metadata — the on-device METADATA SIDECAR: a light TMDB pass over the finalized records fetching title +
    // poster_path + year, written to metadata-<datasetVersion>.json. Ships as a ≤6-month SYNCED cache (den-atlas
    // serves it beside labels/vectors; the app reads it to render a semantic/ANN neighbour without a detail call).
    // Never bundled — a frozen poster snapshot would break TMDB's 6-month caching allowance.
    /// Below this share of titles returning metadata, the run is a failure rather than a thin result.
    /// Real coverage is ~99% (a title without a poster still returns a row); anything near zero is auth or
    /// rate-limiting.
    static let metadataCoverageFloor = 0.90

    static func metadata(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let skipFetch = args.has("--skip-fetch")   // patch meta from an existing sidecar (no TMDB re-fetch)
        let limit = args.int("--limit")
        let meta: DatasetMeta = try JSON.read(Layout.datasetMeta(outDir))
        let path = Layout.metadataArtifact(outDir, meta.datasetVersion)

        if !skipFetch {
            let labels: LabelsArtifact = try JSON.read(Layout.labelsArtifact(outDir, Taxonomy.current.version))
            var records = labels.records
            if let limit { records = Array(records.prefix(limit)) }
            let client = try TMDB.client()
            var out: [PosterMeta] = []
            let chunk = 200   // the client's semaphore throttles the real fan-out; chunk bounds task spawn count
            for start in stride(from: 0, to: records.count, by: chunk) {
                let slice = Array(records[start..<min(start + chunk, records.count)])
                let batch = await withTaskGroup(of: PosterMeta?.self) { group -> [PosterMeta] in
                    for r in slice {
                        let id = MediaIdentifier(r.tmdbId, MediaType(rawValue: r.mediaType) ?? .movie)
                        group.addTask {
                            do { return try await client.posterMeta(id) } catch {
                                // Discarding these is what made the coverage floor below undiagnosable:
                                // it asserts TMDB is failing without having looked at a single error.
                                Log.append(Layout.enrichLog(outDir),
                                           "metadata-miss \(r.mediaType):\(r.tmdbId) (\(error))")
                                return nil
                            }
                        }
                    }
                    var acc: [PosterMeta] = []
                    for await m in group where m != nil { acc.append(m!) }
                    return acc
                }
                out += batch
                FileHandle.standardError.write(Data("  metadata \(out.count)/\(records.count)…\n".utf8))
            }
            // Every fetch is a `try?`, so an expired TMDB_API_KEY or a rate-limit storm yields an EMPTY
            // sidecar — which was then written over the good one and its sha stamped into the manifest.
            // The app folds that sha into its syncKey, so the device happily re-syncs to a sidecar with no
            // posters in it. A partial result is not a result; refuse it and leave what is there.
            let coverage = records.isEmpty ? 1.0 : Double(out.count) / Double(records.count)
            guard coverage >= Self.metadataCoverageFloor else {
                throw ToolError(message: "only \(out.count) of \(records.count) titles returned metadata "
                    + "(\(Int(coverage * 100))%, floor \(Int(Self.metadataCoverageFloor * 100))%) — that is "
                    + "TMDB failing, not titles without posters. Nothing written; the existing sidecar and "
                    + "manifest are unchanged.")
            }
            // A TOTAL order — (id, mediaType), not id alone. TaskGroup yields in completion order, so two
            // identical runs produced different bytes, a different metadataSha256, and, since the app folds
            // that into its syncKey, a forced 4.6 MB re-download on every device for a file that had not
            // changed. Sorting on the id alone does not fix that: `sort` is unstable, and the corpus
            // contains 940 ids that are BOTH a movie and a series — the very titles the media-qualified
            // checkpoint restores. Measured: 8 shuffles of that corpus produced 8 distinct sha256.
            out.sort { ($0.tmdbId, $0.mediaType) < ($1.tmdbId, $1.mediaType) }
            try JSON.write(out, to: path)
        }

        // Patch dataset.meta.json to reference the sidecar (the server reads meta to know what blobs to serve;
        // the app folds `metadataSha256` into its syncKey so a new/updated sidecar triggers a re-sync).
        let blob = try Data(contentsOf: URL(fileURLWithPath: path))
        let patched = meta.namingSidecar(file: (path as NSString).lastPathComponent,
                                         sha256: sha256Hex(blob), bytes: blob.count)
        try JSON.writeMeta(patched, to: Layout.datasetMeta(outDir))
        let all = (try? JSON.read(path) as [PosterMeta]) ?? []
        print(JSON.line(["metadata": all.count, "withPoster": all.filter { $0.posterPath != nil }.count,
                         "sha": patched.metadataSha256 ?? "", "bytes": patched.metadataBytes ?? 0]))
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
        guard blob.count >= 8 else { throw ToolError(message: "vectors blob too small") }
        let count = Int(blob.withUnsafeBytes { Int32(littleEndian: $0.loadUnaligned(fromByteOffset: 0, as: Int32.self)) })
        let dim = Int(blob.withUnsafeBytes { Int32(littleEndian: $0.loadUnaligned(fromByteOffset: 4, as: Int32.self)) })
        guard count == labels.records.count, dim > 0, blob.count == 8 + count * dim else {
            throw ToolError(message: "vectors blob (\(count)×\(dim)) doesn't match labels (\(labels.records.count))")
        }

        // Unit-normalized Doubles once: k-means runs `iterations × k × count` dot products, so paying the
        // conversion per access would dominate the run.
        var rows = [[Double]](repeating: [], count: count)
        blob.withUnsafeBytes { raw in
            let base = raw.baseAddress!.advanced(by: 8).assumingMemoryBound(to: Int8.self)
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

        // Golden positive support per label + a minimum-support guard: a label with too few golden examples
        // yields an unstable per-label F1 (a 3-title label swings the family mean; a 0-golden label — e.g. the
        // emergent themes / Animation — can ONLY register false positives and never validate recall). Exclude
        // sub-threshold labels from the GATE (still reported) so they can neither fail nor pass the whole run.
        let minSupport = args.int("--min-support") ?? 10
        var support: [String: Int] = [:]
        for labels in goldenLabels.values { for label in labels { support[label, default: 0] += 1 } }

        // Per-family precision AND recall at the current acceptance thresholds — the table used to set the
        // precision knee (DT-C). Recall = tp/(tp+fn); fn is golden labels the index missed at this cutoff.
        // Only labels meeting the min-support floor count toward the gate.
        func familyStats(_ labels: [String]) -> (p: Double, r: Double, tp: Int, fp: Int, fn: Int)? {
            let set = Set(labels.filter { (support[$0] ?? 0) >= minSupport })
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

        // Surface (never silently) the labels the min-support guard drops from the gate.
        let excluded = (tax.subgenres + tax.thematic + tax.moods)
            .filter { (support[$0] ?? 0) < minSupport }.sorted()
        if !excluded.isEmpty {
            print("gate excludes \(excluded.count) label(s) with <\(minSupport) golden examples:")
            print("  " + excluded.map { "\($0)=\(support[$0] ?? 0)" }.joined(separator: ", "))
        }

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
    // FP-2: credits feed the composed embedding doc; `hasWikiPlot` marks `overview` as the live Wikipedia plot
    // (re-grounded at enrich) so `assemble` composes the Plot clause only when a real plot was found.
    let director: String?
    let topCast: [String]
    let hasWikiPlot: Bool

    init(_ t: EnrichedTitle) {
        tmdbId = t.tmdbId; mediaType = t.mediaType.rawValue; title = t.title; year = t.year
        overview = t.overview; genreIDs = t.genreIDs; genres = t.genreNames
        keywordIDs = t.keywords.map(\.id); keywords = t.keywords.map(\.name)
        originCountry = t.originCountry; originalLanguage = t.originalLanguage; voteCount = t.voteCount
        director = t.director; topCast = t.topCast; hasWikiPlot = t.hasWikiPlot
    }

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
        hasWikiPlot = try c.decodeIfPresent(Bool.self, forKey: .hasWikiPlot) ?? false
    }

    func toEnrichedTitle() -> EnrichedTitle {
        EnrichedTitle(tmdbId: tmdbId, mediaType: mediaType == "tv" ? .tv : .movie, title: title, year: year,
                      overview: overview, genreIDs: genreIDs, genreNames: genres,
                      keywords: zip(keywordIDs, keywords).map { Keyword(id: $0, name: $1) },
                      originCountry: originCountry, originalLanguage: originalLanguage, voteCount: voteCount,
                      director: director, topCast: topCast, hasWikiPlot: hasWikiPlot)
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


enum EnrichOutcome {
    case ok(EnrichedTitle)
    case belowFloor(Int)
    case anime(Int)
    case noOverview(Int)
    case failure(Int, String)          // definitive (404/decoding) — a dead id, checkpointed
    case transientFailure(Int, String) // 429/5xx/timeout after retries — deferred, NOT checkpointed
}

struct EnrichCheckpoint: Codable {
    // Keyed "movie:12345" / "tv:12345": TMDB movie and TV id namespaces OVERLAP (both start low), so a bare
    // Set<Int> shared across a movie run then a tv run would skip every TV title whose id matches a processed
    // movie id (e.g. tv 550 skipped because movie 550 was done). Media-qualify the key.
    var processed: Set<String> = []
    var nextBatch: Int = 1
    var totals = Totals()

    static func key(_ media: MediaType, _ id: Int) -> String { "\(media.rawValue):\(id)" }

    init() {}
    // Tolerant decode: a legacy checkpoint stored `processed` as bare [Int] (the movie-only pilot) — migrate
    // those to movie-qualified keys so a resume across this change doesn't reset progress. Malformed/truncated
    // JSON still throws here (the container decode fails), which the caller surfaces loudly.
    init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let keyed = try? c.decode(Set<String>.self, forKey: .processed) {
            processed = keyed
        } else {
            processed = Set((try c.decode(Set<Int>.self, forKey: .processed)).map { "movie:\($0)" })
        }
        nextBatch = try c.decodeIfPresent(Int.self, forKey: .nextBatch) ?? 1
        totals = try c.decodeIfPresent(Totals.self, forKey: .totals) ?? Totals()
    }
    enum CodingKeys: String, CodingKey { case processed, nextBatch, totals }

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

struct ReportExtras: Codable {
    let anime: Int
    let noPrimary: Int
    let report: RunReport
}

// MARK: - Paths

enum Layout {
    static func enrichCheckpoint(_ dir: String) -> String { join(dir, "enrich-checkpoint.json") }
    static func classifyCheckpoint(_ dir: String) -> String { join(dir, "classify-checkpoint.json") }
    static func wkConfirmed(_ dir: String) -> String { join(dir, "wk-confirmed.json") }
    static func metadataArtifact(_ dir: String, _ version: String) -> String { join(dir, "metadata-\(version).json") }
    static func enrichLog(_ dir: String) -> String { join(dir, "enrich-log.txt") }
    static func enrichedDir(_ dir: String) -> String { join(dir, "enriched") }
    static func enrichedBatch(_ dir: String, _ id: Int) -> String { join(dir, "enriched/batch-\(id).json") }
    static func escalateBatch(_ dir: String, _ id: Int) -> String { join(dir, "escalate/batch-\(id).json") }
    static func embedderIdentity(_ dir: String) -> String { join(dir, "index/embedder.json") }
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
    /// Write `dataset.meta.json` through `ManifestMerge`, so keys `DatasetMeta` does not model survive
    /// while every key it DOES model — including ones it deliberately omits — comes from this write.
    static func writeMeta(_ value: DatasetMeta, to path: String) throws {
        let existing = try? Data(contentsOf: URL(fileURLWithPath: path))
        let merged = try ManifestMerge.merge(new: try encodeSorted(value), existing: existing,
                                             owned: DatasetMeta.ownedKeys)
        try FileIO.write(merged, to: path)
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

enum Log {
    /// Serialises writes. `metadata` logs from inside a 200-task group, and this opens its own handle and
    /// seeks to the end with no lock — concurrent first-writers took the `data.write(to:)` fallback below,
    /// which TRUNCATES, so the diagnostics the logging exists to produce were partly lost.
    private static let lock = NSLock()

    static func append(_ path: String, _ message: String) {
        lock.lock()
        defer { lock.unlock() }
        try? FileIO.ensureParent(path)
        // Redacted HERE, at the sink, not at each of the dozen call sites that interpolate an error —
        // TMDB's api_key rides in the query string and `URLError`'s description carries the failing URL.
        if let data = (Redact.secrets(message) + "\n").data(using: .utf8) {
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
