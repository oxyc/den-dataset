import Foundation

/// The enriched batch files, in the order a reader must take them. (Where a NEW batch's number comes from
/// is `pipeline/enrich.py`'s now.)
public enum EnrichedBatches {
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
