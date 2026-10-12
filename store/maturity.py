"""Conservative filtering ages plus sparse public-authority observations.

`minimum_age` stays the one-byte hot path. The `mat_*` columns are a parallel sparse table used only by
title details: row i owns observations `mat_o[i]..mat_o[i + 1]`. An observation may have no normalized
age even though its official reported label remains useful to display.
"""
import sys

from .format import AGE_NONE


STRING_FIELDS = {
    "authority": "mat_auth",
    "authorityId": "mat_authid",
    "raw": "mat_raw",
    "assessed": "mat_assess",
    "format": "mat_format",
}


class Maturity:
    def __init__(self):
        self.values = []
        self.observations = []
        self.known = 0
        self.evidence = 0

    def intern(self, strings, key, row):
        for observation in self._validated(key, row)[1]:
            for field in STRING_FIELDS:
                strings.add(observation.get(field))

    def add(self, strings, key, row):
        value, observations = self._validated(key, row)
        self.values.append(AGE_NONE if value is None else value)
        self.known += value is not None
        encoded = []
        for observation in observations:
            encoded.append({
                **{section: strings.id(observation.get(field))
                   for field, section in STRING_FIELDS.items()},
                "mat_age": AGE_NONE if observation.get("age") is None else observation["age"],
                "mat_runtime": observation.get("runtimeMinutes") or 0,
            })
        self.observations.append(encoded)
        self.evidence += len(encoded)

    def put(self, sec, rows):
        sec.put("minimum_age", "B", self.values, 1, expect=rows)
        offsets = [0]
        flat = []
        for observations in self.observations:
            flat.extend(observations)
            offsets.append(len(flat))
        sec.put("mat_o", "I", offsets, 4, expect=rows + 1)
        for section in STRING_FIELDS.values():
            sec.put(section, "I", [observation[section] for observation in flat], 4,
                    expect=len(flat))
        sec.put("mat_age", "B", [observation["mat_age"] for observation in flat], 1,
                expect=len(flat))
        sec.put("mat_runtime", "H", [observation["mat_runtime"] for observation in flat], 2,
                expect=len(flat))

    @staticmethod
    def _validated(key, row):
        maturity = row.get("maturity")
        if maturity is None:
            return None, []
        if not isinstance(maturity, dict):
            sys.exit(f"{key}: maturity is {maturity!r}, expected an object or null")
        value = maturity.get("minimumAge")
        if (isinstance(value, bool) or value is not None
                and (not isinstance(value, int) or not 0 <= value <= 18)):
            sys.exit(f"{key}: maturity.minimumAge is {value!r}, expected null or an integer from 0 through 18")
        observations = maturity.get("observations")
        if not isinstance(observations, list) or not observations:
            sys.exit(f"{key}: maturity.observations must be a non-empty list")
        ages = []
        for at, observation in enumerate(observations):
            if not isinstance(observation, dict):
                sys.exit(f"{key}: maturity.observations[{at}] is not an object")
            for field in ("authority", "authorityId", "raw"):
                field_value = observation.get(field)
                if not isinstance(field_value, str) or not field_value:
                    sys.exit(f"{key}: maturity.observations[{at}].{field} is not a non-empty string")
            age = observation.get("age")
            if isinstance(age, bool) or age is not None and (not isinstance(age, int) or not 0 <= age <= 18):
                sys.exit(f"{key}: maturity.observations[{at}].age is {age!r}, expected null or 0 through 18")
            if age is not None:
                ages.append(age)
            for field in ("assessed", "format"):
                optional = observation.get(field)
                if optional is not None and (not isinstance(optional, str) or not optional):
                    sys.exit(f"{key}: maturity.observations[{at}].{field} is not a non-empty string")
            runtime = observation.get("runtimeMinutes")
            if (isinstance(runtime, bool) or runtime is not None
                    and (not isinstance(runtime, int) or not 1 <= runtime <= 0xFFFF)):
                sys.exit(f"{key}: maturity.observations[{at}].runtimeMinutes is invalid")
        if value != (max(ages) if ages else None):
            sys.exit(f"{key}: maturity.minimumAge is not the highest normalized observation")
        return value, observations
