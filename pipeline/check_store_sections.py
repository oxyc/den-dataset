#!/usr/bin/env python3
"""Refuse a store that drops a section carried by the published store (oxyc/den-dataset#172).

The store format deliberately permits optional sections, so every reader can open an older store. That
compatibility is not permission for a rebuild of the same live dataset to silently turn a feature off.
The store writer stamps its complete section set into the manifest. For the first publish after this guard
lands, the live manifest has no such record, so the publisher downloads that store and passes it here.
"""
import argparse
import json
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not sys.path or sys.path[0] != REPO:
    sys.path.insert(0, REPO)

from store import format as store_format


def sections(path):
    """The section names in table order, after checking enough of the layout to trust the table."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        header = handle.read(store_format.HEADER_BYTES)
        if len(header) != store_format.HEADER_BYTES:
            raise ValueError(f"{path}: truncated store header")
        magic, _version, endian, _digest, count, _rows, _dataset, _reserved = struct.unpack(
            "<8sIIQII16s16s", header)
        if magic != store_format.MAGIC or endian != store_format.ENDIAN_CHECK:
            raise ValueError(f"{path}: not a Den store")
        table = handle.read(count * store_format.ENTRY_BYTES)
    if len(table) != count * store_format.ENTRY_BYTES:
        raise ValueError(f"{path}: truncated section table")
    names = []
    for at in range(0, len(table), store_format.ENTRY_BYTES):
        raw, offset, length, width = struct.unpack("<16sQII", table[at:at + store_format.ENTRY_BYTES])
        try:
            name = raw.split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError(f"{path}: non-ASCII section name") from error
        if not name or name in names:
            raise ValueError(f"{path}: empty or repeated section name {name!r}")
        if offset + length > size or (width > 1 and offset % store_format.ALIGN):
            raise ValueError(f"{path}: invalid extent for section {name}")
        names.append(name)
    return names


def recorded(meta_path):
    with open(meta_path, encoding="utf-8") as handle:
        value = json.load(handle).get("storeSections")
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"{meta_path}: storeSections is not a non-empty list of names")
    if len(value) != len(set(value)):
        raise ValueError(f"{meta_path}: storeSections repeats a name")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("published_meta")
    parser.add_argument("candidate_store")
    parser.add_argument("--published-store",
                        help="required only while the published manifest predates storeSections")
    args = parser.parse_args(argv)
    try:
        before = recorded(args.published_meta)
        if before is None:
            if not args.published_store:
                raise ValueError("the published manifest predates storeSections and no published store was given")
            before = sections(args.published_store)
        after = sections(args.candidate_store)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: store section guard could not compare the stores: {error}", file=sys.stderr)
        return 2
    missing = [name for name in before if name not in set(after)]
    if missing:
        print("\n".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
