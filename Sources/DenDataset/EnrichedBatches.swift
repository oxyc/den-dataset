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
}
