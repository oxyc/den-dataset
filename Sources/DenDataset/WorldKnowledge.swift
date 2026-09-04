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
    /// Absent is a real answer — most out-dirs have no adjudication. CORRUPT is not: it used to read the
    /// same way, so a file truncated mid-edit silently stripped every Cult/Anime/Art House/Epic label from
    /// the whole sub-floor tail, with nothing said. `enrich` and `assemble` both refuse loudly on an
    /// unreadable checkpoint for exactly this reason.
    public static func load(at path: String) throws -> [String: [String]] {
        guard FileManager.default.fileExists(atPath: path) else { return [:] }
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard !data.isEmpty else {
            throw WorldKnowledgeError.unreadable(path, "the file is empty")
        }
        do {
            return expand(try JSONDecoder().decode([String: [String]].self, from: data))
        } catch {
            throw WorldKnowledgeError.unreadable(path, "\(error)")
        }
    }

    /// Split out from the file read so the mapping is testable without touching disk.
    ///
    /// TWO passes, legacy first. A single pass assigned in `Dictionary` iteration order, which Swift
    /// randomises per process — so a file holding both `"95"` and `"tv:95"` (exactly the mid-hand-edit
    /// state this loader exists to accept) resolved to whichever came out first, and the explicit keyed
    /// entry lost roughly one run in six. Same inputs, different shipped labels: the nondeterminism the
    /// sidecar order and the classifier tie-breaks were both hardened against.
    ///
    /// A keyed entry states which medium was adjudicated. A legacy one cannot, so it fills in only where
    /// nothing more specific exists.
    public static func expand(_ raw: [String: [String]]) -> [String: [String]] {
        var out: [String: [String]] = [:]
        for (key, labels) in raw where !key.contains(":") {
            guard let id = strictID(key) else { continue }
            for media in ["movie", "tv"] { out[ClassifyCheckpoint.key(media, id)] = labels }
        }
        for (key, labels) in raw where key.contains(":") {
            out[key] = labels
        }
        return out
    }

    /// `Int("007")` is 7 and `Int("+95")` is 95, so a typo'd key silently attached labels to a DIFFERENT
    /// title. An id is its digits, exactly.
    static func strictID(_ key: String) -> Int? {
        guard !key.isEmpty, key.allSatisfy(\.isNumber), let id = Int(key), String(id) == key else {
            return nil
        }
        return id
    }

    /// How many bare ids a file mixes in alongside keyed ones. A mixed file is a hand-edit in progress;
    /// the previous version returned NOTHING for it, silently stripping every confirmed label, because it
    /// required every key to be qualified and then fell through to an Int-keyed decode that could not
    /// parse `"tv:95"`.
    public static func unkeyedCount(_ raw: [String: [String]]) -> Int {
        raw.keys.filter { !$0.contains(":") && strictID($0) != nil }.count
    }

    /// Keys that are neither qualified nor a plain id — `" 95"`, `"95.0"`, `"007"`, a typo. They are
    /// dropped, and dropping an override silently is how a title loses a label nobody notices.
    public static func unusableKeys(_ raw: [String: [String]]) -> [String] {
        raw.keys.filter { !$0.contains(":") && strictID($0) == nil }.sorted()
    }
}

/// A present-but-unreadable adjudication file. Distinguished from an absent one because they mean opposite
/// things: absent is "nothing was adjudicated", unreadable is "the adjudication is there and I cannot see
/// it", and treating the second as the first discards it silently.
public enum WorldKnowledgeError: Error, CustomStringConvertible {
    case unreadable(String, String)

    public var description: String {
        switch self {
        case .unreadable(let path, let why):
            return "\(path) is unreadable (\(why)); it carries the confirmed world-knowledge labels that "
                + "survive the vote gate, so classifying without it would silently drop them. Fix or "
                + "remove the file."
        }
    }
}
