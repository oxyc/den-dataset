import Foundation
import XCTest
@testable import DenDataset

final class PosterMetaTests: XCTestCase {
    func testPosterMetaCarriesRatingAndLegacyRowsStillDecode() throws {
        let body = Data(#"{"id":550,"title":"Fight Club","poster_path":"/p.jpg","release_date":"1999-10-15","vote_average":8.4}"#.utf8)
        let row = try TMDBClient.posterMetaForTesting(body, identifier: MediaIdentifier(550, .movie))
        XCTAssertEqual(row.voteAverage, 8.4)

        let encoded = try JSONEncoder().encode(row)
        XCTAssertEqual(try JSONDecoder().decode(PosterMeta.self, from: encoded).voteAverage, 8.4)

        let legacy = Data(#"{"tmdbId":1,"mediaType":"movie","title":"Legacy","posterPath":"/old.jpg","year":1995}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(PosterMeta.self, from: legacy).voteAverage)
    }
}
