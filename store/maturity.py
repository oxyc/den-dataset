"""One conservative numeric maturity age per title (`minimum_age`).

The corpus retains authority observations and matching provenance. The mmap store needs only the highest
verified age, as u8; 255 is unknown so a missing classification can never become all-ages by zero fill.
"""
import sys

from .format import AGE_NONE


class Maturity:
    def __init__(self):
        self.values = []
        self.known = 0

    def add(self, key, row):
        value = (row.get("maturity") or {}).get("minimumAge")
        if value is None:
            self.values.append(AGE_NONE)
            return
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 18:
            sys.exit(f"{key}: maturity.minimumAge is {value!r}, expected an integer from 0 through 18")
        self.values.append(value)
        self.known += 1

    def put(self, sec, rows):
        sec.put("minimum_age", "B", self.values, 1, expect=rows)
