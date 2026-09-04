import Foundation

/// Opus-confirmed world-knowledge labels (Cult / Anime / Art House / Epic), which survive the 100-vote
/// gate that otherwise strips them from the obscure tail.
///
/// Keyed `"movie:123"` / `"tv:123"`, because one `wk-confirmed.json` serves an out-dir that holds both
/// media and TMDB's id namespaces overlap. A legacy bare-Int file applies its entries to BOTH media.
///
/// Reading legacy ids as movies — which an earlier version of this did, on the stated premise that movies
/// were what produced them — was wrong, and measurably so. Of the 2,950 entries in the shipped file, 2,833
/// are movie-only, 30 are ids that exist as both, and **87 are series-only**: Berlin Alexanderplatz, The
/// Prisoner, Gormenghast, Heimat. Every one of those 87 sits under the vote floor, so its label survives
/// only through this override; filing them under `movie:` would have silently discarded paid Opus
/// adjudication for 87 titles in order to disambiguate 30.
///
/// Applying to both leaves those 30 exactly as ambiguous as they already were — the file does not record
/// which medium was adjudicated, and inventing an answer is what the movie-only reading did. Files written
/// from now on are keyed and carry no such ambiguity.
public enum WorldKnowledge {
    public static func load(at path: String) -> [String: [String]] {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let raw = try? JSONDecoder().decode([String: [String]].self, from: data)
        else { return [:] }
        return expand(raw)
    }

    /// Split out from the file read so the mapping is testable without touching disk.
    public static func expand(_ raw: [String: [String]]) -> [String: [String]] {
        var out: [String: [String]] = [:]
        for (key, labels) in raw {
            if key.contains(":") {
                out[key] = labels
            } else if let id = Int(key) {
                for media in ["movie", "tv"] { out[ClassifyCheckpoint.key(media, id)] = labels }
            }
        }
        return out
    }

    /// How many bare ids a file mixes in alongside keyed ones. A mixed file is a hand-edit in progress;
    /// the previous version returned NOTHING for it, silently stripping every confirmed label, because it
    /// required every key to be qualified and then fell through to an Int-keyed decode that could not
    /// parse `"tv:95"`.
    public static func unkeyedCount(_ raw: [String: [String]]) -> Int {
        raw.keys.filter { !$0.contains(":") && Int($0) != nil }.count
    }
}
