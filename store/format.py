"""The wire primitives every section group writes through.

The layout is den-spec `wire/store-v1.md` § "Layout": a 64-byte header, a table of 32-byte entries,
then the section bodies, each 8-byte aligned. Nothing here knows what a section MEANS — that is the
group modules' half — and nothing there knows how a section reaches the file.
"""
import hashlib
import struct
import sys

FORMAT_VERSION = 1
MAGIC = b"DENSTOR1"
ENDIAN_CHECK = 0x01020304
HEADER_BYTES = 64
ENTRY_BYTES = 32
ALIGN = 8

U32_NONE = 0xFFFFFFFF
I16_NONE = -0x8000
I32_NONE = -0x80000000
SCORE_NONE = 0xFFFF   # distinct from a genuine 0.00


class Strings:
    """The string dictionary. Ids are assigned AFTER sorting, so the output is byte-identical across runs."""

    def __init__(self):
        self._seen = set()
        self._ids = None

    def add(self, value):
        if value is not None:
            self._seen.add(value)

    def freeze(self):
        ordered = sorted(self._seen)
        self._ids = {s: i for i, s in enumerate(ordered)}
        return ordered

    def id(self, value, default=U32_NONE):
        if value is None:
            return default
        return self._ids[value]


def hundredths(value, what, key):
    """A probability as u8 hundredths, refusing anything the format cannot hold exactly."""
    if value is None:
        return 0
    f = float(value)
    if f != f or f < 0.0 or f > 1.0:
        sys.exit(f"{key}: {what} is {value} — not a probability")
    scaled = round(f * 100)
    if abs(f * 100 - scaled) > 1e-9:
        sys.exit(f"{key}: {what} is {value}, which has more than two decimals — the store stores "
                 f"hundredths, and rounding it here would be a silent precision loss. Fix the producer.")
    return scaled


def score_hundredths(value, what, key):
    """A 0..4 score as u16 hundredths.

    This was u8 twentieths, reasoning that hundredths overflow a u8 at 2.56. True, and the wrong
    conclusion: the corpus scores ARE hundredths — 401 distinct values, 0.00 to 4.00 in steps of 0.01 — so
    twentieths silently rounded 94,498 of 190,116 values, 49.7% of them, Game of Thrones losing all four
    axes. The answer to "does not fit in u8" is a wider integer, not a coarser unit. u16 costs 381 KB of a
    124 MB file.

    It also refuses more than two decimals, which the twentieths version did not — and that omission is
    precisely why the loss shipped: the same check existed six lines away and was not applied here. It
    still sits beside `hundredths` for that reason, though the score axes it encodes belong to
    `scores.py`: the two are one decision about how a fixed-point number reaches the file.
    """
    if value is None:
        return SCORE_NONE
    f = float(value)
    if f != f or f < 0.0 or f > 4.0:
        sys.exit(f"{key}: {what} is {value} — outside the 0..4 score range")
    scaled = round(f * 100)
    if abs(f * 100 - scaled) > 1e-9:
        sys.exit(f"{key}: {what} is {value}, which has more than two decimals — storing it would round "
                 f"silently. Fix the producer, or widen the field deliberately.")
    return scaled


class Sections:
    """Named byte blocks, written in insertion order, each 8-byte aligned."""

    def __init__(self, rows):
        self.rows = rows
        self.order = []
        self.blocks = {}
        self.widths = {}

    def put(self, name, fmt, values, width, expect=None):
        if expect is not None and len(values) != expect:
            sys.exit(f"section {name}: {len(values)} values, expected {expect}")
        if len(name.encode()) > 16:
            sys.exit(f"section name too long: {name}")
        self.blocks[name] = struct.pack(f"<{len(values)}{fmt}", *values)
        self.widths[name] = width
        self.order.append(name)

    def put_raw(self, name, blob, width, expect=None):
        """`expect` is the element count, not bytes — a dense R x K section must say what K is."""
        if expect is not None and len(blob) != expect * width:
            sys.exit(f"section {name}: {len(blob)} bytes, expected {expect} x {width}")
        if not blob and expect != 0:
            sys.exit(f"section {name} is empty — an empty section is well formed and says nothing, "
                     f"which is how genres shipped at zero bytes past every check")
        self.blocks[name] = bytes(blob)
        self.widths[name] = width
        self.order.append(name)

    def put_list(self, name, fmt, width, per_row, allow_empty=False, expect_rows=None):
        """A values array plus offsets of len(rows)+1 — an empty list is a zero-width span.

        The offsets array being the right length proves nothing about the values array: `genres_v` was
        0 bytes beside a perfectly correct 47,619-entry offsets array, and that combination is a valid,
        entirely empty section. A values array of zero is fatal unless the caller says it may be.

        `expect_rows` for a list that is not per TITLE — the entity table has its own length, and
        checking it against the title count would refuse a correct section.
        """
        rows = self.rows if expect_rows is None else expect_rows
        flat, offsets = [], [0]
        for row in per_row:
            flat.extend(row)
            offsets.append(len(flat))
        if len(offsets) != rows + 1:
            sys.exit(f"section {name}: {len(offsets)} offsets, expected {rows + 1}")
        if not flat and not allow_empty:
            sys.exit(f"section {name}_v holds no values for {rows} rows — the field is missing from "
                     f"the source or its type is not what this writer expects")
        self.put(f"{name}_v", fmt, flat, width)
        self.put(f"{name}_o", "I", offsets, 4)

    def put_labelled_list(self, name, per_row):
        """A label list that carries the model's confidence per entry: ids and confidences share one
        offsets array, so row i owns both spans. `subgenres` and `moods` are scored, not bare strings —
        flattening the confidence away would lose the only signal separating a 0.7 label from a 0.3 one."""
        ids, confs, offsets = [], [], [0]
        for row in per_row:
            for label_id, conf in row:
                ids.append(label_id)
                confs.append(conf)
            offsets.append(len(ids))
        if len(offsets) != self.rows + 1:
            sys.exit(f"section {name}: {len(offsets)} offsets, expected {self.rows + 1}")
        if not ids:
            sys.exit(f"section {name}_v holds no values for {self.rows} rows")
        self.put(f"{name}_v", "I", ids, 4)
        self.put(f"{name}_c", "B", confs, 1)
        self.put(f"{name}_o", "I", offsets, 4)


def write(path, sec, rows, dataset_version):
    """Lay the sections out and write the file. Returns the payload's length, header excluded.

    The header carries a blake2b of everything after it, so the whole artifact is self-checking, and
    the alignment assert below is what keeps a `u32` section readable as one in a mmap.
    """
    table_bytes = ENTRY_BYTES * len(sec.order)
    at = HEADER_BYTES + table_bytes
    entries, body = [], []
    for name in sec.order:
        pad = (-at) % ALIGN
        if pad:
            body.append(b"\0" * pad)
            at += pad
        blk = sec.blocks[name]
        entries.append((name, at, len(blk), sec.widths[name]))
        body.append(blk)
        at += len(blk)

    table = b"".join(struct.pack("<16sQII", name.encode(), off, length, width)
                     for name, off, length, width in entries)
    payload = table + b"".join(body)
    digest = int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little")
    header = struct.pack("<8sIIQII16s16s", MAGIC, FORMAT_VERSION, ENDIAN_CHECK, digest,
                         len(sec.order), rows, dataset_version.encode()[:16], b"")
    if len(header) != HEADER_BYTES:
        sys.exit(f"header is {len(header)} bytes, expected {HEADER_BYTES}")

    for name, off, _, width in entries:
        if width > 1 and off % ALIGN:
            sys.exit(f"section {name} at {off} is not {ALIGN}-byte aligned")

    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(payload)
    return len(payload)
