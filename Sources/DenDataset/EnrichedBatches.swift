import Foundation

/// Where a new enriched batch's id comes from.
///
/// `enrich` takes its next id from the enrich checkpoint, and an ABSENT checkpoint used to mean "first run,
/// start at 1". That is not safe: a corpus can hold batches and no checkpoint — `out-t02` held 153 batches
/// with no `enrich-checkpoint.json` — and a delta run into it restarted at 1 and overwrote `batch-1.json`
/// and `batch-2.json`, replacing 640 enriched records with 235.
///
/// The damage would not have stopped there. Vote passes are keyed by batch id, so `votes/batch-1-pass1.json`
/// still held the votes for the titles batch 1 used to contain. Assembling it would have labelled the new
/// titles with the old titles' labels — silently, with no count or checksum out of place.
///
/// So the directory, not the checkpoint, is the floor: whatever is on disk has already been written.
public enum EnrichedBatches {
    /// The highest `batch-<n>.json` in `directory`, or 0 when there are none.
    public static func highestID(inDirectory directory: String) -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(atPath: directory)) ?? []
        return highestID(inFileNames: files)
    }

    /// The same rule over a plain list of names — the seam a test can drive without a filesystem.
    public static func highestID(inFileNames names: [String]) -> Int {
        names.compactMap { name -> Int? in
            guard name.hasPrefix("batch-"), name.hasSuffix(".json") else { return nil }
            return Int(name.dropFirst("batch-".count).dropLast(".json".count))
        }.max() ?? 0
    }

    /// Batch file names in BATCH-NUMBER order, oldest first. The order a reader must use, and the reason it
    /// cannot just call `sorted()`.
    ///
    /// `sorted()` is lexicographic, so `batch-99.json` comes AFTER `batch-177.json` and `batch-178.json`
    /// lands between `batch-177` and `batch-18`. That matters because a key can appear in more than one
    /// batch: 1,855 of 59,218 do, and **505 of them disagree about `hasWikiPlot`**. Read in name order,
    /// first-wins, a stale no-plot record beats the later one that found the plot — `tv:37854` (One Piece)
    /// and `tv:65942` (Re:ZERO) are false in `batch-175` and true in `batch-176`; `tv:273240` is false in
    /// `batch-102` and true in `batch-155`. Those titles then embed with `plot = ""`.
    ///
    /// The tie-break is NEWEST-WINS, which is not a new policy: `finalize` already de-dups that way, keeping
    /// the last occurrence. This is what makes the readers agree with it.
    ///
    /// Batch number rather than mtime: mtime order differs from batch order at 155 of 177 positions here,
    /// because batches 1-173 were touched in bulk. Under newest-wins they happen to agree on every key, but
    /// only the batch number says anything about when a record was *written*.
    public static func orderedNames(inFileNames names: [String]) -> [String] {
        names.compactMap { name -> (Int, String)? in
            guard name.hasPrefix("batch-"), name.hasSuffix(".json"),
                  let n = Int(name.dropFirst("batch-".count).dropLast(".json".count)) else { return nil }
            return (n, name)
        }
        .sorted { $0.0 < $1.0 }
        .map(\.1)
    }

    /// The batch files in `directory`, in batch-number order, oldest first.
    public static func orderedNames(inDirectory directory: String) -> [String] {
        orderedNames(inFileNames: (try? FileManager.default.contentsOfDirectory(atPath: directory)) ?? [])
    }
}
