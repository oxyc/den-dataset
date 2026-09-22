import CryptoKit
import DenDataset
import Foundation

// taxonomy-backfill — the fetch and artifact half of the producer, structured as discrete, resumable phases.
// Labelling is no longer here: the decision-only pass (`scripts/v2/run_combined.py`, the `classify` stage)
// produces the labels and facets the corpus join reads, so this tool gathers the inputs that pass needs and
// turns already-decided labels into the shipped artifacts.
//
//   worklist      — build the universe (TMDB /discover sorted vote_count.desc for the pilot; daily-export
//                   parse for the full run) → out/worklist-<media>.json
//   enrich        — next N un-enriched ids → ONE TMDB call each (append_to_response=keywords), drop below the
//                   vote floor / anime / fetch failures (logged) → out/enriched/batch-<id>.json (+ checkpoint).
//                   The enriched batch is SCRATCH (holds raw TMDB text) and is never shipped.
//   dump-articles — each grounded title's whole Wikipedia article as prose → the classify pass's input.
//   embed-corpus  — compose(facts + already-decided tags + plot) → den-embed → append to the index store.
//   finalize      — index store → labels-<taxonomy>.json + vectors-<embed>.bin + report.json +
//                   dataset.meta.json (DERIVED only). Folds in the former import-dataset.mjs job.
//
// Env: TMDB_API_KEY (enrichment only). No LLM key — this tool does not classify.

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
        // acceptable, because a periodic full pass covers drift, whereas paying per-id daily does not scale.
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
    // Writes one scratch batch file the article dump and the embed pass read + advances the resumable
    // checkpoint.
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
        // An ABSENT checkpoint is not proof of a first run. `out-t02` has 153 enriched batches and no enrich
        // checkpoint, so a delta into it started numbering at 1 and overwrote batch-1 and batch-2 — 640
        // records replaced by 235, with the old votes/batch-1-pass1.json still on disk, which would have
        // labelled the new titles with the OLD titles' votes. Trust the directory over the missing file.
        let onDisk = EnrichedBatches.highestID(inDirectory: Layout.enrichedDir(outDir))
        if checkpoint.nextBatch <= onDisk { checkpoint.nextBatch = onDisk + 1 }
        let pending = worklist.filter { !checkpoint.processed.contains(EnrichCheckpoint.key($0.media, $0.tmdbId)) }.prefix(limit)
        guard !pending.isEmpty else {
            print(JSON.line(["remaining": 0, "count": 0])); return
        }
        // A BATCH HOLDS ONE MEDIA TYPE. Enrichment itself handles both (its Wikidata mapping is keyed by
        // MediaKey), but the corpus holds 1,756 ids that exist as BOTH a movie and a series, and any reader
        // that keys a batch row by a bare TMDB id would hand Armageddon and Buffy the same record with
        // nothing in the output to show it. Worklists are written per media, so this only fires on a
        // hand-assembled one — where failing is far better than mislabelling silently.
        let mediaTypes = Set(pending.map(\.media))
        guard mediaTypes.count == 1 else {
            throw ToolError(message: "worklist mixes \(mediaTypes.map(\.rawValue).sorted().joined(separator: " + ")) "
                + "in one batch. The vote files written from it carry no media type, so a TMDB id present as "
                + "both a movie and a series would be labelled once and applied to both. Split the worklist "
                + "by media and enrich each separately.")
        }

        let tmdb = try TMDB.client()
        let batchId = checkpoint.nextBatch
        var titles: [EnrichedTitle] = []
        // Opt-IN now, not opt-out: excluding anime silently cost the corpus 1,498 titles including the entire
        // Ghibli catalogue, and a default that loses well-known titles should have to be asked for.
        let excludeAnime = args.has("--exclude-anime")
        var belowFloor = 0, anime = 0, failures = 0, noOverview = 0
        // Ids whose failure was TRANSIENT (429/5xx/timeout, retries already exhausted in transport). These are
        // NOT checkpointed, so the next run retries them — rather than permanently dropping a title on a blip.
        var deferred = Set<MediaKey>()
        // Nor are titles rejected for being BELOW THE VOTE FLOOR. A vote count is the one input here that
        // moves on its own, and it only ever moves up — so "below the floor" is a verdict about today, not
        // about the title. Checkpointing it made the rejection permanent: a title at 40 votes when it was
        // first seen would never be reconsidered at 62, and a detail response served from cache (up to its
        // TTL old) widened that window to weeks. They stay pending and are re-judged next run, which the
        // response cache makes nearly free.
        var belowFloorKeys = Set<MediaKey>()

        try await withThrowingTaskGroup(of: EnrichOutcome.self) { group in
            for entry in pending {
                group.addTask {
                    do {
                        let key = MediaKey(entry.media, entry.tmdbId)
                        let title = try await tmdb.classificationRecord(MediaIdentifier(entry.tmdbId, entry.media))
                        if title.voteCount < floor { return .belowFloor(key) }
                        if excludeAnime, isAnime(title) { return .anime(key) }
                        // Can't classify a stub — drop titles with no / very-short overview (DT-C region-aware
                        // floor). Judged on the LENGTH of TMDB's overview, the only part of it that crosses
                        // the client boundary; `title.overview` is empty until a Wikipedia plot fills it.
                        if title.overviewChars < 20 { return .noOverview(key) }
                        return .ok(title)
                    } catch {
                        // Transient → defer (retry next run); definitive (404/decoding) → a real dead id, drop.
                        let key = MediaKey(entry.media, entry.tmdbId)
                        return Transport.isRetryable(error)
                            ? .transientFailure(key, "\(error)")
                            : .failure(key, "\(error)")
                    }
                }
            }
            for try await outcome in group {
                switch outcome {
                case .ok(let title): titles.append(title)
                case .belowFloor(let key):
                    belowFloor += 1
                    belowFloorKeys.insert(key)
                case .anime: anime += 1
                case .noOverview: noOverview += 1
                case .failure(let key, let reason):
                    failures += 1
                    Log.append(Layout.enrichLog(outDir), "fetch-failure id=\(key.logLabel) \(reason)")
                case .transientFailure(let key, let reason):
                    deferred.insert(key)
                    Log.append(Layout.enrichLog(outDir), "fetch-deferred id=\(key.logLabel) (transient: \(reason))")
                }
            }
        }

        // FP-2 — re-ground on Wikipedia: ONE Wikidata SPARQL maps the surviving ids to their enwiki articles,
        // then each title's plot is fetched live. Where a plot exists it REPLACES the TMDB overview (ToS-clean
        // grounding for the labelling pass); titles keep the TMDB overview only where Wikipedia has no plot.
        // ONE query PER MEDIA TYPE, and the result keyed by both. TMDB's movie and series id spaces overlap
        // (movie 95 is Armageddon, series 95 is Buffy), so a batch holding both cannot share a lookup: taking
        // the whole batch's media from its first entry looked series 91545 up as a MOVIE and grounded Young
        // Wallander on the plot of "Sunday Drive (film)" — a confident, completely wrong plot, with nothing in
        // the output to mark it as such. Worklists are normally per-media, which is why this stayed hidden.
        let mapping: [MediaKey: WikipediaSource.Mapping]
        do {
            var merged: [MediaKey: WikipediaSource.Mapping] = [:]
            for media in Set(titles.map(\.mediaType)) {
                let ids = titles.filter { $0.mediaType == media }.map(\.tmdbId)
                for (id, value) in try await WikipediaSource().wikidata(forTMDBIds: ids, mediaType: media) {
                    merged[MediaKey(media, id)] = value
                }
            }
            mapping = merged
        } catch {
            throw ToolError(message: "Wikidata mapping failed for batch \(batchId) after retries (\(error)); "
                + "nothing written — re-run to retry this batch")
        }

        var withPlot = 0
        var grounded: [EnrichedTitle] = []
        for outcome in try await regroundOnWikipedia(titles, mapping: mapping, log: Layout.enrichLog(outDir)) {
            switch outcome {
            case .grounded(let title): grounded.append(title); withPlot += 1
            // The reason rides along on the record. A later pass re-runs the subset a fix reaches —
            // `noSection` for a heading rule, `noArticle` for a non-English sitelink — instead of the corpus.
            case .noPlot(let title, let reason): grounded.append(title.notingNoPlot(reason.rawValue))
            case .deferred(let id): deferred.insert(id)   // transient plot fetch — retry next run, don't checkpoint
            }
        }
        var survivors = grounded.map(EnrichedDTO.init)

        survivors.sort { $0.tmdbId < $1.tmdbId }
        // Never write over an existing batch: the plots already dumped from it belong to the titles it USED
        // to hold, so a clobbered batch strands them silently rather than failing.
        let batchPath = Layout.enrichedBatch(outDir, batchId)
        guard !FileManager.default.fileExists(atPath: batchPath) else {
            throw ToolError(message: "refusing to overwrite \(batchPath): it already holds an enriched batch, "
                + "and anything derived from batch \(batchId) belongs to those titles. The enrich "
                + "checkpoint's nextBatch is out of step with the batches on disk — fix it rather than "
                + "clobbering.")
        }
        // And never let a NEW batch silently re-cover a key an existing batch already holds. The guard above
        // protects the batch FILE; nothing protected the keys inside it, so 1,855 of 59,218 keys ended up in
        // more than one batch and 505 of those disagree with themselves about `hasWikiPlot`. Which record
        // wins then depends on the reader's traversal order — the defect `EnrichedBatches.orderedNames`
        // exists to make deterministic, and better not to create at all.
        //
        // A warning rather than a refusal: re-covering is legitimate when a title is deliberately
        // re-enriched, and failing here would block exactly the pass that fixes a stale record. But silence
        // is how 505 of them accumulated.
        var seen: [String: Int] = [:]
        let enrichedDir = Layout.enrichedDir(outDir)
        for name in EnrichedBatches.orderedNames(inDirectory: enrichedDir) {
            guard let id = Int(name.dropFirst("batch-".count).dropLast(".json".count)), id != batchId,
                  let existing: [EnrichedDTO] = try? JSON.read(
                      (enrichedDir as NSString).appendingPathComponent(name)) else { continue }
            for dto in existing { seen["\(dto.mediaType):\(dto.tmdbId)"] = id }
        }
        let recovered = survivors.compactMap { dto -> String? in
            seen["\(dto.mediaType):\(dto.tmdbId)"].map { "\(dto.mediaType):\(dto.tmdbId) (batch \($0))" }
        }
        if !recovered.isEmpty {
            let sample: String = recovered.prefix(5).joined(separator: ", ")
                + (recovered.count > 5 ? " …" : "")
            Log.append(Layout.enrichLog(outDir),
                       "re-covered \(recovered.count) key(s) already in earlier batches: \(sample)")
            let warning = "  warning: \(recovered.count) key(s) here already exist in earlier batches — "
                + "the newest wins on read, but the older records remain. \(sample)\n"
            FileHandle.standardError.write(Data(warning.utf8))
        }
        try JSON.writePretty(survivors, to: batchPath)
        // Checkpoint every pending id EXCEPT those still owed another look: transient failures (a blip must
        // not drop a title) and below-floor rejections (a vote count only climbs, so today's verdict is not
        // the title's).
        for entry in pending {
            let key = MediaKey(entry.media, entry.tmdbId)
            guard !deferred.contains(key), !belowFloorKeys.contains(key) else { continue }
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
        case noPlot(EnrichedTitle, NoPlotReason)
        /// MediaKey, not a bare id: deferring "95" would hold back a movie and a series together.
        case deferred(MediaKey)
    }

    /// WHY a title has no plot — recorded per title, because `hasWikiPlot: false` on its own is the thing
    /// that forces a full re-scrape every time anything improves.
    ///
    /// The four causes want four different fixes and are not interchangeable: `noArticle` needs a
    /// non-English sitelink or nothing at all, `noSection` is who a heading-rule change or a lead fallback
    /// would reach, `belowFloor` is a threshold decision, and `fetchFailed` is simply worth retrying. Folded
    /// into one boolean, the only safe answer to "who should I re-run?" is "all 19,542", which is how this
    /// corpus came to be re-scraped repeatedly.
    enum NoPlotReason: String {
        /// No English Wikipedia article to read — the Wikidata item has no enwiki sitelink.
        case noArticle
        /// The article exists and carries no section `plotRank` recognises.
        case noSection
        /// A plot section exists but its prose is under `wikiPlotFloor`.
        case belowFloor
        /// A definitive fetch failure, e.g. a 404 on a stale sitelink. Transient failures defer instead.
        case fetchFailed
    }

    /// Minimum plot length to re-ground on (chars).
    ///
    /// Was 200, justified as "a bare one-line logline adds little grounding over the TMDB overview it would
    /// replace". That comparison no longer exists: `overview` holds a Wikipedia plot or NOTHING — TMDB's
    /// text was removed from the record entirely on ToS grounds — so the trade is not "this versus the
    /// overview" but "this versus nothing", and 200 was rejecting real premises:
    ///
    ///   165  Would You Marry Me?  a romantic comedy about a 90-day fake marriage between a man and a woman
    ///                             trying to win the grand prize of a luxury home for newlyweds
    ///   179  Disclaimer           Catherine Ravenscroft, a documentary-journalist, discovers she is a
    ///                             character in a novel that purports to reveal a secret she has hidden
    ///   189  Silo                 a dystopian future where a community exists in a giant silo extending
    ///                             144 levels underground
    ///
    /// Each names genre, premise and stakes, which is what the vector wants. 120 admits those and still
    /// rejects the actual loglines: "The film explores the life and career of John le Carré" (55) and its
    /// 93- and 101-character neighbours.
    static let wikiPlotFloor = 120

    /// Long enough that a title's OWN article is clearly its best source, so the P144 source work is not
    /// worth a second fetch.
    ///
    /// The floor cannot also do this job. It used to be both the accept threshold AND the fall-through
    /// trigger — first candidate over the line wins — so lowering it would have stopped Silo, Dark Matter
    /// and Defending Jacob falling through to the novels that carry their real plots, trading 12,415
    /// characters for 189. Separating them means a thin own-article still tries the source work, and the
    /// longer answer wins.
    static let ownArticleSufficient = 1000

    /// Fetch each title's live Wikipedia plot (bounded concurrency) and classify the outcome. A missing mapping
    /// or a plot section that is absent / below the floor is a definitive `noPlot`; a transient fetch failure
    /// (429/5xx/timeout, retries exhausted) is `deferred` so the id is retried on the next run.
    static func regroundOnWikipedia(_ titles: [EnrichedTitle], mapping: [MediaKey: WikipediaSource.Mapping],
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
                        // Runtime + creators come from the SAME hop that resolved the article, so they are
                        // folded in for EVERY title — including the ones with no plot, which keep no other
                        // trace of this call.
                        let facts = mapping[MediaKey(title.mediaType, title.tmdbId)]
                        let title = title.mergingWikidata(runtimeMinutes: facts?.runtimeMinutes,
                                                          creators: facts?.creators ?? [])
                        // The title's OWN article first; the source work only if that yields no plot. An
                        // adaptation's article is often production-and-episodes with no story in it at all —
                        // "Attack on Titan (TV series)" is Series overview / Seasons / Cast, while the plot
                        // lives on the franchise page. Measured: this recovers 87% of plotless anime series,
                        // 21% of general TV, 5% of films.
                        //
                        // The source article describes the BOOK or franchise, not this adaptation, so it can
                        // cover unadapted material or diverge. Accepted for premise and thematic similarity,
                        // where the story engine is what matters; it would be wrong for anything claiming to
                        // describe this cut specifically.
                        //
                        // Each candidate carries its ROLE, so the winner still knows which of the two it
                        // was. Reading it off the position afterwards would be wrong for the 4% of titles
                        // with no English article, where the source work is the only candidate and sits at
                        // index 0.
                        var candidates: [(article: String, role: PlotArticleRole)] = []
                        if let own = facts?.article { candidates.append((own, .own)) }
                        if let source = facts?.sourceArticle { candidates.append((source, .sourceWork)) }
                        guard !candidates.isEmpty else { return .noPlot(title, .noArticle) }
                        do {
                            var found: (article: String, role: PlotArticleRole,
                                        plot: WikipediaSource.PlotFetch)?
                            // Whether ANY candidate had a plot-ranked section at all, even a short one. That
                            // is the difference between "a heading rule would reach this" and "the floor
                            // rejected it", and without it both look the same afterwards.
                            var sawSection = false
                            for candidate in candidates {
                                guard let plot = try await wiki.plot(articleTitle: candidate.article)
                                else { continue }
                                sawSection = true
                                // Keep the LONGEST, rather than the first over the line. The own article is
                                // tried first, so a thin one no longer blocks the source work: Silo's own
                                // article gives 189 characters of premise and the novel gives 12,415.
                                if plot.text.count > (found?.plot.text.count ?? 0) {
                                    found = (candidate.article, candidate.role, plot)
                                }
                                // …but stop once the title's own article is clearly enough, so a well
                                // covered adaptation does not pay for a second fetch it cannot use.
                                if plot.text.count >= ownArticleSufficient { break }
                            }
                            // NO ENGLISH ARTICLE, or a thin one. Two thirds of the films with no plot have
                            // no enwiki article at all, and half of THOSE have one in another language —
                            // 12 of 30 sampled carried a real plot under the local heading. bge-m3 is
                            // multilingual, so the prose embeds directly with no translation step.
                            //
                            // The title's own language first, as the likeliest to have it, then the rest.
                            // Measured, that first guess is right 8 times in 15 — good but not sufficient,
                            // and the misses are the interesting half: four were ENGLISH-language films
                            // with no English article, covered by the German or Italian Wikipedia instead.
                            if (found?.plot.text.count ?? 0) < ownArticleSufficient,
                               let byLang = facts?.articlesByLang, !byLang.isEmpty {
                                let preferred = [title.originalLanguage].compactMap { $0 }
                                let order = preferred + byLang.keys.sorted().filter { !preferred.contains($0) }
                                for lang in order {
                                    guard let article = byLang[lang],
                                          let plot = try await wiki.plot(articleTitle: article,
                                                                         language: lang) else { continue }
                                    sawSection = true
                                    if plot.text.count > (found?.plot.text.count ?? 0) {
                                        // Still this title's OWN article, just on another Wikipedia —
                                        // `articlesByLang` is built from its sitelinks, never the source
                                        // work's.
                                        found = (article, .ownOtherLanguage, plot)
                                    }
                                    if plot.text.count >= ownArticleSufficient { break }
                                }
                            }
                            guard let hit = found, hit.plot.text.count >= wikiPlotFloor else {
                                return .noPlot(title, sawSection ? .belowFloor : .noSection)
                            }
                            // Which article won and at which revision — recorded so a refresh can ask for
                            // current revids in bulk and re-read only the articles that moved.
                            // The RESOLVED article, not the one asked for: a redirect returns the target's
                            // content and revid, so storing the redirect's name would make the refresh
                            // compare revisions of two different pages.
                            //
                            // …and WHICH candidate that was, plus whether a redirect moved it. Both are
                            // known only here, and the article name alone recovers neither: a novel's page
                            // and an adaptation's are both just names, and a redirect leaves no trace at
                            // all. Discarded, every later census has to replay this decision out of the
                            // Wikidata cache to ask "is this text about this title?".
                            return .grounded(title.groundedOnWikiPlot(
                                hit.plot.text,
                                article: hit.plot.resolvedArticle ?? hit.article,
                                revId: hit.plot.revId,
                                sections: hit.plot.sections,
                                language: hit.plot.language,
                                provenance: PlotProvenance(role: hit.role, requested: hit.article,
                                                           resolved: hit.plot.resolvedArticle)))
                        } catch {
                            let key = MediaKey(title.mediaType, title.tmdbId)
                            if Transport.isRetryable(error) {
                                Log.append(log, "plot-deferred id=\(key.logLabel) (transient: \(error))")
                                return .deferred(key)
                            }
                            // Definitive (e.g. 404 on a stale sitelink) — keep the title on its TMDB overview.
                            Log.append(log, "plot-miss id=\(key.logLabel) (\(error))")
                            return .noPlot(title, .fetchFailed)
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

    // doc-facts — scrape the two embedding-doc clauses that still came from TMDB (director, genre) from
    // Wikidata, so the vectors can be built with no TMDB Content in them at all. Writes `doc-facts.json` keyed
    // "mediaType:tmdbId". Kept SEPARATE from embed-corpus because the scrape is ~770 SPARQL requests and the
    // embed is hours: pay each once, and let a failure in one not cost the other.
    static func docFacts(_ args: Args) async throws {
        let outDir = try args.require("--out-dir")
        let labelsPath = try args.require("--labels")
        let batchSize = args.int("--batch") ?? 100
        let path = (outDir as NSString).appendingPathComponent("doc-facts.json")

        struct Row: Codable { let directors: [String]; let genres: [String] }
        // RESUME: an id already present is not re-queried. A 38.5k-title scrape WILL be interrupted, and
        // re-running from zero each time is how a polite scrape turns into an impolite one.
        var facts: [String: Row] = (try? JSON.read(path)) ?? [:]
        let before = facts.count

        let labels: LabelsArtifact = try JSON.read(labelsPath)
        var byType: [String: [Int]] = [:]
        for record in labels.records where facts["\(record.mediaType):\(record.tmdbId)"] == nil {
            byType[record.mediaType, default: []].append(record.tmdbId)
        }
        let todo = byType.values.reduce(0) { $0 + $1.count }
        FileHandle.standardError.write(Data("  doc-facts: \(before) cached, \(todo) to fetch\n".utf8))

        let source = WikipediaSource()
        var done = 0
        for (type, ids) in byType {
            let mediaType: MediaType = type == "tv" ? .tv : .movie
            for start in stride(from: 0, to: ids.count, by: batchSize) {
                let slice = Array(ids[start..<min(start + batchSize, ids.count)])
                let got = try await source.docFacts(forTMDBIds: slice, mediaType: mediaType)
                for id in slice {
                    // Absent from the result means Wikidata states neither — record the empty row so the
                    // resume does not re-query it forever. The COMPOSER treats both as "no clause"; the
                    // unknown-vs-none distinction matters for the facts sidecar, not for prose.
                    let f = got[id]
                    facts["\(type):\(id)"] = Row(directors: f?.directors ?? [], genres: f?.genres ?? [])
                }
                done += slice.count
                // Write every batch, not at the end: an interrupted scrape keeps everything it paid for.
                try JSON.write(facts, to: path)
                if done % 1000 < batchSize {
                    FileHandle.standardError.write(Data("  doc-facts \(done)/\(todo)…\n".utf8))
                }
            }
        }
        let withDirector = facts.values.filter { !$0.directors.isEmpty }.count
        let withGenre = facts.values.filter { !$0.genres.isEmpty }.count
        print(JSON.line(["docFacts": facts.count, "fetched": todo, "withDirector": withDirector,
                         "withGenre": withGenre, "path": path]))
    }

    // embed-corpus — build bge-m3 vectors for the EXISTING (already-shipped) labels from the Wikipedia-plot
    // enrichment, WITHOUT re-classifying. Composes facts + the existing tags + the wiki plot, batch-embeds via
    // den-embed, and writes a FRESH index store (labels = the existing records verbatim, aligned to new
    // vectors) into a dedicated out-dir. This is the "semantic vectors now" path: it upgrades the app's ANN
    // from lexical FNV to bge-m3 immediately, reusing the labels we already ship, while the fresh plot-grounded
    // reclassification (which improves the LABELS) is run later. `finalize --out-dir <same>` emits the artifact.
    /// Write each grounded title's WHOLE Wikipedia article as prose, one JSON object per line.
    ///
    /// The classifier reads the article; the embedder reads the extracted plot. Two consumers with genuinely
    /// different needs — see `WikipediaSource.articleProse` — and this is what feeds the first. Keeping them
    /// apart also means a future extractor bug degrades similarity without silently corrupting labels.
    ///
    /// Every article was already fetched once to find its plot, and `plotArticle` + `plotLanguage` were
    /// recorded per title, so this re-requests the same URL and the response cache answers nearly all of it.
    /// That is why it goes through `WikipediaSource` rather than reading the cache directly: the cache is
    /// keyed by a hash of the request, so reconstructing keys by hand would be a second implementation of
    /// something that already works, and wrong the first time a parameter moves.
    ///
    /// Resumable by re-reading its own output — a kill costs at most the titles in flight.
    static func dumpArticles(_ args: Args) async throws {
        let outPath = try args.require("--out")
        let enrichedDir = try args.require("--enriched-dir")
        let limit = args.int("--limit")

        var done: Set<String> = []
        if FileManager.default.fileExists(atPath: outPath) {
            for line in try FileIO.readLines(outPath) {
                struct Row: Decodable { let mediaType: String; let tmdbId: Int }
                if let r: Row = try? JSON.decode(line) { done.insert("\(r.mediaType):\(r.tmdbId)") }
            }
            FileHandle.standardError.write(Data("  resuming: \(done.count) already dumped\n".utf8))
        }

        // Newest batch wins, matching how every other reader folds this directory.
        var wanted: [(key: String, dto: EnrichedDTO)] = []
        var seen: Set<String> = []
        for file in EnrichedBatches.orderedNames(inDirectory: enrichedDir).reversed() {
            let dtos: [EnrichedDTO] = try JSON.read((enrichedDir as NSString).appendingPathComponent(file))
            for dto in dtos {
                let key = "\(dto.mediaType):\(dto.tmdbId)"
                guard !seen.contains(key) else { continue }
                seen.insert(key)
                // ToS: only a title with a Wikipedia plot may reach an LLM, and only such a title has a
                // recorded article to fetch.
                guard dto.hasWikiPlot, let article = dto.plotArticle, !article.isEmpty else { continue }
                guard !done.contains(key) else { continue }
                wanted.append((key, dto))
            }
        }
        if let limit { wanted = Array(wanted.prefix(limit)) }
        FileHandle.standardError.write(Data("  \(wanted.count) articles to fetch\n".utf8))

        let wiki = WikipediaSource()
        let handle = try FileIO.appender(outPath)
        defer { try? handle.close() }
        struct ArticleRow: Encodable {
            let mediaType: String, tmdbId: Int, title: String?, year: Int?, article: String, language: String
            let resolvedArticle: String?, revId: Int?, extractorArticleRevId: Int?
            let sections: [String], plotSections: [String]
            let chars: Int, text: String
        }

        let gate = 4   // gentle on the public Wikipedia API, same as the plot pass
        var written = 0, missing = 0
        var index = 0
        while index < wanted.count {
            let slice = Array(wanted[index..<min(index + gate, wanted.count)])
            let rows = try await withThrowingTaskGroup(of: ArticleRow?.self) { group -> [ArticleRow] in
                for item in slice {
                    group.addTask {
                        let lang = item.dto.plotLanguage ?? "en"
                        guard let article = item.dto.plotArticle,
                              let found = try? await wiki.articleProse(articleTitle: article, language: lang)
                        else { return nil }
                        return ArticleRow(
                            mediaType: item.dto.mediaType, tmdbId: item.dto.tmdbId, title: item.dto.title,
                            year: item.dto.year,
                            article: article, language: lang, resolvedArticle: found.resolvedArticle,
                            revId: found.revId, extractorArticleRevId: item.dto.plotRevId,
                            sections: found.sections,
                            plotSections: item.dto.plotSections, chars: found.text.count,
                            text: found.text)
                    }
                }
                var out: [ArticleRow] = []
                for try await row in group { if let row { out.append(row) } else { missing += 1 } }
                return out
            }
            for row in rows {
                try handle.writeLine(JSON.encodeLine(row))
                written += 1
            }
            index += gate
            if written % 500 == 0 && !rows.isEmpty {
                FileHandle.standardError.write(Data("  dumped \(written) (no article \(missing))…\n".utf8))
            }
        }
        print(JSON.line(["written": written, "noArticle": missing, "out": outPath]))
    }

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
                    // already Wikidata (P170) on the enrichment, so nothing here is TMDB-sourced.
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
        // De-dup by (mediaType, tmdbId) keeping the LAST occurrence — a re-embed appends superseding
        // records, and finalize keeps the newest while preserving aligned vectors.
        var lastIndex: [String: Int] = [:]
        for (i, r) in allRecords.enumerated() { lastIndex["\(r.mediaType):\(r.tmdbId)"] = i }
        let keep = Set(lastIndex.values)
        let records = allRecords.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let rows = allRows.enumerated().filter { keep.contains($0.offset) }.map(\.element)
        let vectors: [[Int8]] = rows.map { $0.v.map { Int8(clamping: $0) } }

        // Guard the mixed-embedder / mislabel footgun: every vector must share ONE length, and it must match the
        // dimension the --embedding-version label implies (bge-m3 = 1024, fnv/e02 = 384). A store assembled with
        // two embedders, or a blob labelled bge-m3 but holding 384-dim FNV content, would otherwise ship a
        // corrupt/lying artifact, which a reader's length check can only reject wholesale, never explain.
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

        // Written by whichever embed path verified the known-answer canary — `embed-corpus` here, or
        // `scripts/v2/embed_docs.py` on the box by way of `import_box_vectors.py --embed-space`. Absent
        // for a store built before the canary existed, which is carried as an absent manifest key rather
        // than a guess: naming a space this run did not verify is the failure the canary exists to stop.
        var embeddingSpace: String? = nil
        if FileManager.default.fileExists(atPath: Layout.embeddingSpace(outDir)) {
            // Loudly, like the identity above: a malformed file would otherwise drop the space from the
            // manifest and say nothing, which looks exactly like a store that never had one.
            do {
                let stamp: EmbedSpaceCanary.Stamp = try JSON.read(Layout.embeddingSpace(outDir))
                embeddingSpace = stamp.spaceId
            } catch {
                throw ToolError(message: "\(Layout.embeddingSpace(outDir)) is unreadable (\(error)) — it "
                    + "records the embedding space this store's vectors are in, so shipping without it "
                    + "would publish a corpus that cannot say which space it belongs to")
            }
        }

        let taxonomyVersion = Taxonomy.current.version
        let labels = LabelsArtifact(taxonomyVersion: taxonomyVersion, records: records)
        let labelsBlob = try JSON.encodeSorted(labels)
        let prose = ShipGuard.prohibited(in: labelsBlob)
        guard prose.isEmpty else {
            throw ToolError(message: "REFUSING to ship: the labels artifact carries prose field(s) "
                + "\(prose.joined(separator: ", ")). A published artifact holds labels, ids and numbers — "
                + "TMDB's terms bar shipping their text, and a CC0 plot belongs in the corpus and the "
                + "embedding, not in an artifact served to devices.")
        }
        let labelsPath = Layout.labelsArtifact(outDir, taxonomyVersion)
        let vectorsPath = Layout.vectorsArtifact(outDir, embeddingVersion)
        // Each row named by its title, from the record it was zipped with. The blob used to be the matrix
        // alone, leaving the labels artifact as the only record of which row was whose — so the artifact
        // had to keep being built after it stopped being published, purely as an order oracle, and a
        // regenerated labels file with a different record order would have moved every vector onto the
        // wrong title with nothing able to notice.
        let vectorsData = try VectorBlob.encode(
            keys: records.map { VectorBlob.key(mediaType: $0.mediaType, tmdbId: $0.tmdbId) },
            vectors: vectors)
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
            embedderMaxTokens: embedder?.maxTokens,
            embeddingSpace: embeddingSpace)
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
        // --limit is only consulted inside the fetch path, so pairing it with --skip-fetch skipped the
        // probe and went straight to patching the manifest — which usage() promises it never does.
        if limit != nil && skipFetch {
            throw ToolError(message: "--limit is a probe and --skip-fetch patches the manifest from an "
                + "existing sidecar; together they would do the second without the first. Pick one.")
        }
        let meta: DatasetMeta = try JSON.read(Layout.datasetMeta(outDir))
        let path = Layout.metadataArtifact(outDir, meta.datasetVersion)

        if !skipFetch {
            let labels: LabelsArtifact = try JSON.read(Layout.labelsArtifact(outDir, Taxonomy.current.version))
            var records = labels.records
            // --limit is a PROBE: fetch a handful, report, write nothing. It used to truncate `records`
            // here — before the coverage floor below is computed against `records.count` — so coverage was
            // always ~100% and the floor could never fire, and the N rows were then written over the
            // shipped 37.5k-row sidecar with their sha stamped into the manifest. The app folds that sha
            // into its syncKey, so `metadata --limit 50`, the obvious cheap credential smoke-test, would
            // have re-synced every device to a sidecar missing 37,483 titles.
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
            out = SidecarOrder.sorted(out)
            if limit != nil {
                print(JSON.line(["probe": out.count, "of": records.count,
                                 "withPoster": out.filter { $0.posterPath != nil }.count,
                                 "wrote": "nothing (--limit is a probe)"]))
                return
            }
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

// MARK: - Anime filter (single authority; both worklist modes funnel through enrich)

/// TMDB keyword 210024 = "anime"; Japanese-language Animation is the catch-all. DT-taxonomy.md: **no anime**.
/// Anime, by TMDB's `anime` keyword or by "animated AND originally Japanese".
///
/// `enrich` used to DROP everything this matched, which is why the corpus held 595 Japanese films and 2,469
/// animated titles and precisely ZERO in the intersection — Spirited Away, Totoro, Akira and 1,495 others were
/// never fetched at all. Nothing recorded why. The likely reason is that t02's subgenres are built for Western
/// film and TV and fit anime badly, which is true but is an argument for better labels, not for the titles
/// being absent from search, similarity, facets and facts as well.
///
/// The predicate is kept because the flag is still worth carrying: `enrich --exclude-anime` restores the old
/// behaviour, and a caller that wants an anime-only pass can invert it.
func isAnime(_ title: EnrichedTitle) -> Bool {
    if title.keywords.contains(where: { $0.id == 210024 }) { return true }
    if title.genreIDs.contains(16) && title.originalLanguage == "ja" { return true }
    return false
}

func confidenceBucket(_ confidence: Double) -> String {
    let low = (confidence * 10).rounded(.down) / 10
    return String(format: "%.1f-%.1f", low, low + 0.1)
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

/// A TMDB id together with its media type — the only safe key for anything holding both. The two id spaces
/// overlap, so a bare `Int` silently conflates movie 95 (Armageddon) with series 95 (Buffy).
struct MediaKey: Hashable {
    let mediaType: MediaType
    let tmdbId: Int

    init(_ mediaType: MediaType, _ tmdbId: Int) {
        self.mediaType = mediaType
        self.tmdbId = tmdbId
    }

    /// `"movie:95"` — log lines printed a bare id, which is ambiguous in exactly the way this type exists to
    /// prevent: "id=95" could be Armageddon or Buffy.
    var logLabel: String { "\(mediaType.rawValue):\(tmdbId)" }
}

/// The scratch enriched record (holds raw TMDB text → never shipped; gitignored). Captures the full
/// EnrichedTitle so the article dump and the embed pass can rebuild it for grounding.
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

    init(_ t: EnrichedTitle) {
        tmdbId = t.tmdbId; mediaType = t.mediaType.rawValue; title = t.title; year = t.year
        overview = t.overview; genreIDs = t.genreIDs; genres = t.genreNames
        keywordIDs = t.keywords.map(\.id); keywords = t.keywords.map(\.name)
        originCountry = t.originCountry; originalLanguage = t.originalLanguage; voteCount = t.voteCount
        director = t.director; topCast = t.topCast; createdBy = t.createdBy
        runtimeMinutes = t.runtimeMinutes
        hasWikiPlot = t.hasWikiPlot
        plotArticle = t.plotArticle; plotRevId = t.plotRevId; noPlotReason = t.noPlotReason
        plotSections = t.plotSections; plotLanguage = t.plotLanguage
        plotArticleRole = t.plotProvenance?.role.rawValue
        plotArticleRedirected = t.plotProvenance?.redirected
        overviewChars = t.overviewChars
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

/// Ids here are `MediaKey`, never a bare Int: a batch can hold both media types, and TMDB's id spaces
/// overlap, so deferring "95" would otherwise defer a movie and a series together.
enum EnrichOutcome {
    case ok(EnrichedTitle)
    case belowFloor(MediaKey)
    case anime(MediaKey)
    case noOverview(MediaKey)
    case failure(MediaKey, String)          // definitive (404/decoding) — a dead id, checkpointed
    case transientFailure(MediaKey, String) // 429/5xx/timeout after retries — deferred, NOT checkpointed
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
    static func metadataArtifact(_ dir: String, _ version: String) -> String { join(dir, "metadata-\(version).json") }
    static func enrichLog(_ dir: String) -> String { join(dir, "enrich-log.txt") }
    static func enrichedDir(_ dir: String) -> String { join(dir, "enriched") }
    static func enrichedBatch(_ dir: String, _ id: Int) -> String { join(dir, "enriched/batch-\(id).json") }
    static func embedderIdentity(_ dir: String) -> String { join(dir, "index/embedder.json") }
    static func embeddingSpace(_ dir: String) -> String { join(dir, "index/embedding-space.json") }
    static func compositionIdentity(_ dir: String) -> String { join(dir, "index/composition.json") }
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
        // Detail responses are served from disk when already fetched (DEN_CACHE=0 or TMDB_CACHE=0
        // disables, DEN_CACHE_DIR / TMDB_CACHE_TTL_DAYS tune it).
        return TMDBClient(apiKey: key, maxConcurrent: 8, cache: TMDBCachePolicy.cache())
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
            name: "worklist",
            summary: "Build the universe of ids to classify → worklist-<media>.json.",
            flags: [
                .value("--out", "<path>", "where to write the worklist JSON", required: true),
                .value("--mode", "<discover|export|delta>",
                       "discover (default): TMDB /discover, vote_count.desc. export: parse TMDB's daily id "
                       + "export. delta: the DT-F daily freshness pass over titles released since --since"),
                .value("--media", "<movie|tv>", "movie (default) or tv — a worklist holds ONE media type"),
                .value("--count", "<n>", "how many ids to collect in discover mode (default 500)"),
                .value("--vote-floor", "<n>", "minimum TMDB vote count (default 50); re-checked at enrich"),
                .value("--origins", "<cc,cc>",
                       "discover: comma-separated origin countries, one fully-paged vote_count.gte slice each "
                       + "— the foreign-depth expansion. Additive; --count is not applied"),
                .value("--year-max", "<yyyy>",
                       "discover past 10k results: newest release year to partition from (default 2026)"),
                .value("--year-min", "<yyyy>", "discover past 10k results: oldest release year (default 1920)"),
                .value("--since", "<yyyy-mm-dd>", "delta: only titles released on or after this date (required in delta)"),
                .value("--known", "<labels-tNN.json>",
                       "delta: already-published labels whose titles are skipped — a delta classifies what is NEW"),
                .value("--file", "<export.json>", "export: the TMDB daily-export file to parse (required in export)"),
            ],
            run: { try await Commands.worklist($0) }),
        Subcommand(
            name: "enrich",
            summary: "Next N un-enriched worklist ids → one TMDB call each → a scratch batch.",
            flags: [
                .value("--worklist", "<path>", "the worklist JSON to draw ids from", required: true),
                .value("--out-dir", "<dir>", "the run directory (enriched batches + the resumable checkpoint)",
                       required: true),
                .value("--vote-floor", "<n>", "minimum TMDB vote count (default 50); below it a title stays pending"),
                .value("--limit", "<n>", "how many un-enriched ids to take this run (default 150)"),
                .bare("--exclude-anime",
                      "drop anime. Opt-IN: excluding it by default silently cost the corpus 1,498 titles"),
            ],
            run: { try await Commands.enrich($0) }),
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
            name: "dump-articles",
            summary: "Write each grounded title's WHOLE Wikipedia article as prose, one JSON object per line.",
            flags: [
                .value("--out", "<path>", "the JSONL to append to; re-running resumes from it", required: true),
                .value("--enriched-dir", "<dir>", "the enriched batches naming each title's article", required: true),
                .value("--limit", "<n>", "stop after N articles (smoke tests)"),
            ],
            run: { try await Commands.dumpArticles($0) }),
        Subcommand(
            name: "doc-facts",
            summary: "Scrape Wikidata P57 director + P136 genre for the shipped corpus into doc-facts.json.",
            flags: [
                .value("--out-dir", "<dir>", "doc-facts.json is written here", required: true),
                .value("--labels", "<labels-t02.json>", "the corpus to scrape facts for", required: true),
                .value("--batch", "<n>", "ids per SPARQL request (default 100). Resumable: a re-run skips "
                       + "ids already in the file"),
            ],
            run: { try await Commands.docFacts($0) }),
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
            name: "finalize",
            summary: "Index store → labels-<taxonomy>.json + vectors-<embed>.bin + dataset.meta.json + report.",
            flags: [
                .value("--out-dir", "<dir>", "the run directory holding the index store", required: true),
                .value("--embedding-version", "<name>",
                       "the artifact label (default bge-m3). Its implied dimension is checked against the "
                       + "vectors, so a mislabelled blob is refused rather than shipped"),
            ],
            run: { try Commands.finalize($0) }),
        Subcommand(
            name: "metadata",
            summary: "The poster sidecar. Its filename carries the datasetVersion — run it after EVERY finalize.",
            flags: [
                .value("--out-dir", "<dir>", "the run directory holding dataset.meta.json", required: true),
                .bare("--skip-fetch", "patch the manifest from an existing sidecar, with no TMDB re-fetch"),
                .value("--limit", "<n>",
                       "a PROBE: fetch N and report, writing no sidecar and touching no manifest — a partial "
                       + "sidecar would re-sync every device onto a gutted one"),
            ],
            run: { try await Commands.metadata($0) }),
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
