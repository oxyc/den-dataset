import Foundation

/// CC0 facts per title, for den-atlas `/recommend` — the ranking signals that must NOT come from TMDB.
///
/// This is a different artifact from `ComposedDoc`'s inputs: the doc wants prose, so it takes labels; this
/// wants to be joined and filtered, so entity-valued fields are **Q-ids** with names in a shared `entities`
/// map, and only ISO-coded fields (country, language) are plain strings.
///
/// Its first and most valuable use is the DELTA: titles atlas has never seen. Measured against a real
/// library, 42 of 73 library titles and 158 of ~220 billboard candidates had no labels and no facets at all —
/// mostly new arrivals, which the >=50-vote worklist floor cannot reach because a new release has no votes
/// yet. A facts record needs no plot, no LLM pass and no embedding, so it can be built for those titles in
/// minutes and refreshed daily.
public enum WikidataFacts {
    /// How a property's value is carried in the output.
    public enum Kind: Sendable {
        case entity      // Q-id + a name in `entities`
        case iso         // resolved to an ISO code via a second property (P297 country, P218 language)
        case literal     // a bare value (runtime, episode counts, external ids)
        case dateP       // a date, carrying its precision
    }

    public struct Spec: Sendable {
        public let key: String       // the field name in the record
        public let prop: String      // the Wikidata property
        public let kind: Kind
        public let isoVia: String?   // for .iso: the property on the VALUE holding the code
        public let tvOnly: Bool

        public init(_ key: String, _ prop: String, _ kind: Kind, isoVia: String? = nil, tvOnly: Bool = false) {
            self.key = key; self.prop = prop; self.kind = kind; self.isoVia = isoVia; self.tvOnly = tvOnly
        }
    }

    /// The fields `/recommend` ranks, filters or explains by. Coverage figures are measured on this corpus,
    /// films/series, and are the reason several obvious-looking properties are absent: P1080 narrative
    /// universe scored 0%, P155/P156 sequel order 8%, P166 awards 11%.
    public static let specs: [Spec] = [
        .init("instanceOf", "P31", .entity),                 // film / series / miniseries / documentary
        .init("imdbId", "P345", .literal),                   // 100%
        .init("released", "P577", .dateP),                   // films: 71% day-precision
        .init("started", "P580", .dateP, tvOnly: true),      // series: 98% day-precision
        .init("ended", "P582", .dateP, tvOnly: true),
        .init("directors", "P57", .entity),                  // films 97%, series 37% (TMDB: 25%)
        .init("creators", "P170", .entity),                  // series 32-40%
        .init("screenwriters", "P58", .entity),              // films 76%
        .init("cast", "P161", .entity),                      // films 86% (median 10 vs TMDB's cap of 4)
        .init("genres", "P136", .entity),                    // finer than TMDB's 19; see genreMap
        .init("countries", "P495", .iso, isoVia: "P297"),
        .init("languages", "P364", .iso, isoVia: "P218"),
        .init("broadcaster", "P449", .entity, tvOnly: true), // 91% on series; TMDB carries nothing like it
        .init("productionCompanies", "P272", .entity),       // 41-51%
        .init("distributors", "P750", .entity),              // 81% — better covered than production company
        .init("composers", "P86", .entity),                  // 71% on films
        .init("cinematographers", "P344", .entity),          // 61% on films
        .init("runtimeMinutes", "P2047", .literal),
        .init("franchise", "P179", .entity),                 // sparse (41% top films, 5% tail) — tiebreak only
        .init("mainSubjects", "P921", .entity),              // 29%
        .init("basedOn", "P144", .entity),                   // 18% — links adaptations of one source
        .init("narrativeLocations", "P840", .entity),        // 47%
        .init("seasons", "P2437", .literal, tvOnly: true),
        .init("episodes", "P1113", .literal, tvOnly: true),
    ]

    /// One record. Everything is optional: an ABSENT key means Wikidata states nothing, which is not the same
    /// as "none" and must never be ranked as a penalty. Explicit `novalue` is vanishingly rare — measured zero
    /// across 240 sampled titles — so an empty array is not written at all.
    public struct Record: Codable, Sendable {
        public var mediaType: String
        public var tmdbId: Int
        public var hasVector: Bool
        public var fields: [String: FieldValue]

        public init(mediaType: String, tmdbId: Int, hasVector: Bool, fields: [String: FieldValue]) {
            self.mediaType = mediaType; self.tmdbId = tmdbId; self.hasVector = hasVector; self.fields = fields
        }
    }

    /// A field is a list of strings (Q-ids, ISO codes or literals) or a date carrying its precision. A
    /// year-only date must stay distinguishable: collapsing it to a day pins every undated 2026 title to the
    /// same instant and makes "newest first" meaningless.
    public enum FieldValue: Codable, Sendable, Equatable {
        case list([String])
        case date(String, precision: String)

        public func encode(to encoder: Encoder) throws {
            var c = encoder.singleValueContainer()
            switch self {
            case .list(let v): try c.encode(v)
            case .date(let d, let p): try c.encode(["date": d, "precision": p])
            }
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let v = try? c.decode([String].self) { self = .list(v); return }
            let m = try c.decode([String: String].self)
            self = .date(m["date"] ?? "", precision: m["precision"] ?? "day")
        }
    }

    /// TMDB's genre vocabulary, which is what the app's hide-genre setting stores. Movie and TV ids differ for
    /// the same word, and TV folds several together ("Action & Adventure", "Sci-Fi & Fantasy"), so a Wikidata
    /// genre maps to a DIFFERENT id per media type — hence the pair.
    static let tmdbGenres: [String: (movie: Int?, tv: Int?)] = [
        "action": (28, 10759), "adventure": (12, 10759), "animation": (16, 16), "comedy": (35, 35),
        "crime": (80, 80), "documentary": (99, 99), "drama": (18, 18), "family": (10751, 10751),
        "fantasy": (14, 10765), "history": (36, nil), "horror": (27, nil), "music": (10402, nil),
        "mystery": (9648, 9648), "romance": (10749, nil), "science fiction": (878, 10765),
        "thriller": (53, nil), "war": (10752, 10768), "western": (37, 37), "kids": (nil, 10762),
        "reality": (nil, 10764), "soap opera": (nil, 10766), "talk show": (nil, 10767), "news": (nil, 10763),
    ]

    /// `Q-id → {"movie": 18, "tv": 18}` for the genres a build actually references. Only EXACT matches after
    /// the media suffix is stripped: Wikidata is far finer than TMDB's 19 (sitcom, telenovela, neo-noir,
    /// cyberpunk), and guessing a parent for those would quietly file a cyberpunk film under Science Fiction
    /// on the strength of a substring. Unmapped genres are still useful — they are what makes the vocabulary
    /// richer than TMDB's — they just cannot answer a TMDB-id hide filter.
    public static func genreMap(entities: [String: [String: String]],
                                strip: (String) -> String) -> [String: [String: Int]] {
        var out: [String: [String: Int]] = [:]
        for (qid, names) in entities {
            guard let label = names["en"] else { continue }
            guard let hit = tmdbGenres[strip(label)] else { continue }
            var entry: [String: Int] = [:]
            if let m = hit.movie { entry["movie"] = m }
            if let t = hit.tv { entry["tv"] = t }
            if !entry.isEmpty { out[qid] = entry }
        }
        return out
    }

    /// Wikidata's `wikibase:timePrecision` integer → the string the sidecar carries. 11 is a day, 10 a month,
    /// 9 a year; anything coarser is a decade or worse and is not useful for ordering releases.
    public static func precisionName(_ raw: Int) -> String? {
        switch raw {
        case 11: return "day"
        case 10: return "month"
        case 9:  return "year"
        default: return nil
        }
    }

    /// `+1999-03-31T00:00:00Z` → `1999-03-31`, trimmed to what the precision actually asserts so a
    /// year-precision value cannot be read as the 1st of January.
    public static func trimDate(_ raw: String, precision: String) -> String? {
        var s = raw
        if s.hasPrefix("+") { s.removeFirst() }
        guard s.count >= 10 else { return nil }
        let date = String(s.prefix(10))
        switch precision {
        case "day": return date
        case "month": return String(date.prefix(7))
        case "year": return String(date.prefix(4))
        default: return nil
        }
    }
}
