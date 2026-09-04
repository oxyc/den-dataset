#!/usr/bin/env python3
"""Join MovieLens ml-32m links.csv to the shipped Den ids, and report the join rate.

MovieLens is a MOVIE dataset. Its links.csv carries no TV ids at all, so nothing built on
it can say anything about the ~10% of the index that is series — the join is reported
against the movie half, and against the whole shipped set, so neither number can be
mistaken for the other.

Reads nothing but links.csv here; ratings are streamed separately (877 MB).
"""
import csv
import json
import os
import sys

ML = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/movielens/ml-32m'
CORPUS_IDS = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/corpus/corpus-ids.json'
OUT = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/movielens/join.json'


def main():
    with open(CORPUS_IDS, encoding='utf-8') as fh:
        shipped = json.load(fh)['shipped']
    shipped_movies = {int(k.split(':')[1]) for k in shipped if k.startswith('movie:')}
    shipped_series = {int(k.split(':')[1]) for k in shipped if k.startswith('tv:')}

    ml_to_tmdb = {}
    blank = 0
    with open(os.path.join(ML, 'links.csv'), encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ['movieId', 'imdbId', 'tmdbId']:
            sys.exit(f"links.csv columns changed: {reader.fieldnames}")
        for row in reader:
            raw = row['tmdbId'].strip()
            if not raw:
                blank += 1
                continue
            ml_to_tmdb[int(row['movieId'])] = int(raw)

    ml_tmdb_ids = set(ml_to_tmdb.values())
    matched = ml_tmdb_ids & shipped_movies
    # movieIds usable as evaluation items: those whose tmdbId is a shipped Den movie.
    usable_movie_ids = {m: t for m, t in ml_to_tmdb.items() if t in shipped_movies}

    result = {
        'movielensRows': len(ml_to_tmdb) + blank,
        'movielensWithTmdbId': len(ml_to_tmdb),
        'movielensBlankTmdbId': blank,
        'movielensDistinctTmdbIds': len(ml_tmdb_ids),
        'denShippedTotal': len(shipped),
        'denShippedMovies': len(shipped_movies),
        'denShippedSeries': len(shipped_series),
        'matchedDenMovies': len(matched),
        'joinRateOfDenMovies': round(len(matched) / len(shipped_movies), 4),
        'joinRateOfDenShippedTotal': round(len(matched) / len(shipped), 4),
        'joinRateOfMovieLens': round(len(matched) / len(ml_tmdb_ids), 4),
        'movielensMovieIdsUsable': len(usable_movie_ids),
        'coverableSeries': 0,
        'note': 'MovieLens ml-32m has no TV ids; the series half of the index is unmeasurable by this ruler.',
    }
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump({'summary': result, 'movieIdToTmdb': {str(k): v for k, v in sorted(usable_movie_ids.items())}}, fh)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
