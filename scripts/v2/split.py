#!/usr/bin/env python3
"""The DEV/TEST split, defined once, for every ruler.

The split is a pure function of the title key, so a title lands in the same half in the
co-rating ruler and in the premise ruler. Splitting each ruler independently would leak:
a title tuned on via its co-rating case could then be graded via its premise triplet.

TEST is sealed. Nothing in the tuning path may read a TEST case.
"""
import hashlib

SALT = 'den-v2-2026-09-04'


def half(key: str) -> str:
    """'dev' or 'test' for a "movie:123" / "tv:123" key."""
    digest = hashlib.sha256((SALT + '|' + key).encode('utf-8')).digest()
    return 'test' if digest[0] & 1 else 'dev'


def is_dev(key: str) -> bool:
    return half(key) == 'dev'


def is_test(key: str) -> bool:
    return half(key) == 'test'
