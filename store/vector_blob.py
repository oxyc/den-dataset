"""The int8 vector blob — `vectors-bge-m3.bin`, `vectors-premise.bin` and every experiment beside them.

    0    8         magic b"DENVEC02"
    8    4         u32 count   (little-endian)
    12   4         u32 dim     (little-endian)
    16   8*count   u64 keys    (little-endian), ONE PER ROW, in row order
    …    count*dim int8 rows, quantized int8-symmetric-x127 from L2-normalized floats

## Why the keys are in the file

v1 was `[i32 count][i32 dim][rows…]` — the matrix and nothing else. Which title row *n* described was
recorded only in the order some separate `labels-*.json` happened to list its records. Nothing in the
`.bin` could detect a mismatch: regenerate the labels file with a different record order and every vector
silently moves onto the wrong title, in a file that loads cleanly and returns real numbers for everything.
That is why `labels-t02.json` and `labels-premise.json` had to keep being BUILT after they stopped being
PUBLISHED — not for their labels, which the corpus carries, but purely as an order oracle. An oracle is
not a check; it cannot fail.

The key is the same one the store uses (`build_store.py`, section `keys`): `(media << 32) | tmdbId`, media
0 = movie, 1 = tv. Never a bare tmdbId — 1,097 ids in this corpus are both a film and a series. 8 bytes a
row is 380 KB on a 48 MB file, and it turns a positional assumption into a join that can be asserted.

## Why the magic, and why it is `DENVEC02`

Eight ASCII bytes with a trailing version number, matching `DENSTOR1` — the convention this repo already
uses — so `head -c8` names the file and a future bump is one byte.

The discrimination against v1 is total in both directions. A v1 header is two small little-endian ints, so
its bytes 4..8 are the dim: `00 04 00 00` (1024) or `80 01 00 00` (384), never `45 43 30 32`. Read the
other way, a v2 file's first eight bytes are count = 1,448,232,772 and dim = 842,018,117, which overflows
every length check in this repo. So neither format can be misread AS the other — but only a reader that
looks for the magic refuses BY NAME, pointing at the migration, instead of dying on a length assert.
"""
import struct
import sys

MAGIC = b"DENVEC02"
HEADER_BYTES = 16
KEY_BYTES = 8

#: What a v1 file is, for the error message and for readers that still have to open one.
LEGACY_HEADER_BYTES = 8


def pack_key(key):
    """`"movie:238"` → the u64 the store and the blob agree on."""
    media, _, tmdb = key.partition(":")
    if not tmdb.isdigit():
        sys.exit(f"{key!r} is not a media:tmdbId key")
    return ((0 if media == "movie" else 1) << 32) | int(tmdb)


def unpack_key(value):
    """The u64 back to `"movie:238"`."""
    return f"{'movie' if value >> 32 == 0 else 'tv'}:{value & 0xFFFFFFFF}"


def header(keys, dim):
    """The magic, the counts and the key column — everything before the first row."""
    return (MAGIC + struct.pack("<II", len(keys), dim)
            + struct.pack(f"<{len(keys)}Q", *[pack_key(k) if isinstance(k, str) else k for k in keys]))


def write(path, keys, rows, dim):
    """`rows` is the raw int8 payload, already in `keys` order."""
    if len(rows) != len(keys) * dim:
        sys.exit(f"{path}: {len(rows)} payload bytes for {len(keys)} x {dim}")
    with open(path, "wb") as fh:
        fh.write(header(keys, dim))
        fh.write(rows)


def read(path, allow_legacy=False):
    """`(count, dim, keys, blob, base)` — `keys` is None only for a v1 file under `allow_legacy`.

    `blob` is the whole file and `base` the offset of row 0, so a caller can slice rows out of it without
    copying 48 MB. A file that is neither format, or whose length disagrees with its own header, is fatal
    here rather than a plausible-looking shift later.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[:len(MAGIC)] != MAGIC:
        if not allow_legacy:
            sys.exit(f"{path}: not a {MAGIC.decode()} vector blob (first bytes {blob[:8]!r}). A v1 blob "
                     f"carries no keys, so joining it would rest on a labels file's record order — the "
                     f"exact failure the format bump removes. Convert it: "
                     f"pipeline/migrate_vector_blob.py --blob {path} --labels <labels-*.json> --out <new>")
        if len(blob) < LEGACY_HEADER_BYTES:
            sys.exit(f"{path}: {len(blob)} bytes, too short to be a vector blob")
        count, dim = struct.unpack("<II", blob[:LEGACY_HEADER_BYTES])
        if len(blob) != LEGACY_HEADER_BYTES + count * dim:
            sys.exit(f"{path}: {len(blob)} bytes for {count} x {dim} (v1)")
        return count, dim, None, blob, LEGACY_HEADER_BYTES
    if len(blob) < HEADER_BYTES:
        sys.exit(f"{path}: {len(blob)} bytes, too short to be a vector blob")
    count, dim = struct.unpack("<II", blob[len(MAGIC):HEADER_BYTES])
    base = HEADER_BYTES + count * KEY_BYTES
    if len(blob) != base + count * dim:
        sys.exit(f"{path}: {len(blob)} bytes for {count} x {dim} plus {count} keys, "
                 f"expected {base + count * dim}")
    keys = [unpack_key(k) for k in struct.unpack_from(f"<{count}Q", blob, HEADER_BYTES)]
    if len(set(keys)) != count:
        dupes = sorted({k for k in keys if keys.count(k) > 1})[:3]
        sys.exit(f"{path}: the key column repeats {count - len(set(keys))} keys ({dupes}) — two rows "
                 f"claiming one title is not a join this can resolve")
    return count, dim, keys, blob, base
