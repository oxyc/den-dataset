"""The curated primary franchises, eras and release-ordered members from `franchises.json`.

Raw P179 facts remain in `franchise_v`; these optional sections are the viewer-facing judgement from the
franchises stage.  A missing input writes no sections, preserving stores built before the addition.
"""
import json
import sys

from .format import U32_NONE, hundredths


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        sys.exit(f"{where}: expected a non-empty string, got {value!r}")
    return value.strip()


class Franchises:
    """Validated schema-2 franchises, resolved to corpus rows before strings are frozen."""

    def __init__(self, path, keys, dataset_version):
        self.present = path is not None
        self.rows = {key: row for row, key in enumerate(keys)}
        self.franchises = []
        self.primary = [U32_NONE] * len(keys)
        self.umbrella_ids = [None] * len(keys)
        self.umbrella_names = [None] * len(keys)
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        if blob.get("schema") != 2:
            sys.exit(f"{path}: franchise schema is {blob.get('schema')!r}, expected 2")
        if blob.get("datasetVersion") != dataset_version:
            sys.exit(f"{path}: datasetVersion {blob.get('datasetVersion')!r}, expected {dataset_version!r}")
        raw, title_map = blob.get("franchises"), blob.get("titles")
        if not isinstance(raw, dict) or not isinstance(title_map, dict):
            sys.exit(f"{path}: franchises and titles must be objects")
        claimed, era_ids = {}, set()
        for index, fid in enumerate(sorted(raw)):
            entry = raw[fid]
            where = f"{path} franchise {fid!r}"
            if not isinstance(entry, dict) or _text(entry.get("id"), where) != fid:
                sys.exit(f"{where}: id must equal its object key")
            name = _text(entry.get("name"), where)
            source = _text(entry.get("source"), where)
            confidence = hundredths(entry.get("confidence"), "franchise confidence", fid)
            eras = entry.get("eras")
            members = entry.get("members")
            if not isinstance(eras, list) or not eras:
                sys.exit(f"{where}: needs at least one era")
            if not isinstance(members, list) or len(members) < 2:
                sys.exit(f"{where}: needs at least two members")
            parsed_eras, era_index = [], {}
            for order, era in enumerate(eras):
                ewhere = f"{where} era {order}"
                if not isinstance(era, dict) or era.get("order") != order:
                    sys.exit(f"{ewhere}: order must be {order}")
                eid = _text(era.get("id"), ewhere)
                ename = _text(era.get("name"), ewhere)
                if eid in era_ids:
                    sys.exit(f"{ewhere}: era id {eid!r} is used twice")
                era_ids.add(eid)
                listed = era.get("members")
                if not isinstance(listed, list) or not listed or any(not isinstance(k, str) for k in listed):
                    sys.exit(f"{ewhere}: members must be a non-empty key list")
                era_index[eid] = order
                parsed_eras.append((eid, ename, listed))

            parsed_members = []
            by_era = {eid: [] for eid in era_index}
            for order, member in enumerate(members):
                mwhere = f"{where} member {order}"
                if not isinstance(member, dict) or member.get("order") != order:
                    sys.exit(f"{mwhere}: order must be {order}")
                key = _text(member.get("key"), mwhere)
                eid = _text(member.get("eraId"), mwhere)
                if key not in self.rows:
                    sys.exit(f"{mwhere}: {key} is not a corpus title")
                if key in claimed:
                    sys.exit(f"{mwhere}: {key} is already in franchise {claimed[key]}")
                if eid not in era_index:
                    sys.exit(f"{mwhere}: eraId {eid!r} is not one of this franchise's eras")
                claimed[key] = fid
                by_era[eid].append(key)
                parsed_members.append((self.rows[key], order, era_index[eid]))
            for eid, _, listed in parsed_eras:
                if listed != by_era[eid]:
                    sys.exit(f"{where}: era {eid!r} members differ from the release-ordered member table")
            self.franchises.append((fid, name, confidence, source, parsed_eras, parsed_members))
            for row, _, _ in parsed_members:
                self.primary[row] = index

        if set(title_map) != set(claimed):
            missing, extra = sorted(set(claimed) - set(title_map)), sorted(set(title_map) - set(claimed))
            sys.exit(f"{path}: titles index differs from members; missing {missing[:3]}, extra {extra[:3]}")
        for key, fid in claimed.items():
            value = title_map[key]
            if not isinstance(value, dict) or value.get("primary") != fid or set(value) - {"primary", "umbrella"}:
                sys.exit(f"{path}: titles[{key!r}] must name primary {fid!r} and an optional umbrella")
            umbrella = value.get("umbrella")
            if umbrella is not None:
                if not isinstance(umbrella, dict):
                    sys.exit(f"{path}: titles[{key!r}].umbrella must be an object")
                row = self.rows[key]
                self.umbrella_ids[row] = _text(umbrella.get("id"), f"{path} title {key} umbrella")
                self.umbrella_names[row] = _text(umbrella.get("name"), f"{path} title {key} umbrella")

    def intern(self, strings):
        for fid, name, _, source, eras, _ in self.franchises:
            strings.add(fid)
            strings.add(name)
            strings.add(source)
            for eid, ename, _ in eras:
                strings.add(eid)
                strings.add(ename)
        for value in self.umbrella_ids + self.umbrella_names:
            strings.add(value)

    def put(self, sec, strings):
        if not self.present:
            return
        count = len(self.franchises)
        sec.put("fr_primary", "I", self.primary, 4, expect=sec.rows)
        sec.put("fr_id", "I", [strings.id(f[0]) for f in self.franchises], 4, expect=count)
        sec.put("fr_name", "I", [strings.id(f[1]) for f in self.franchises], 4, expect=count)
        sec.put("fr_conf", "B", [f[2] for f in self.franchises], 1, expect=count)
        sec.put("fr_source", "I", [strings.id(f[3]) for f in self.franchises], 4, expect=count)
        sec.put("fr_umb_id", "I", [strings.id(value) for value in self.umbrella_ids], 4, expect=sec.rows)
        sec.put("fr_umb_name", "I", [strings.id(value) for value in self.umbrella_names], 4,
                expect=sec.rows)

        era_ids, era_names, era_orders, era_offsets = [], [], [], [0]
        member_rows, member_eras, member_orders, member_offsets = [], [], [], [0]
        era_base = 0
        for _, _, _, _, eras, members in self.franchises:
            for order, (eid, name, _) in enumerate(eras):
                era_ids.append(strings.id(eid))
                era_names.append(strings.id(name))
                era_orders.append(order)
            for row, order, era in members:
                member_rows.append(row)
                member_eras.append(era_base + era)
                member_orders.append(order)
            era_base += len(eras)
            era_offsets.append(len(era_ids))
            member_offsets.append(len(member_rows))
        sec.put("fr_era_id", "I", era_ids, 4)
        sec.put("fr_era_name", "I", era_names, 4)
        sec.put("fr_era_order", "I", era_orders, 4)
        sec.put("fr_era_o", "I", era_offsets, 4, expect=count + 1)
        sec.put("fr_mem_row", "I", member_rows, 4)
        sec.put("fr_mem_era", "I", member_eras, 4)
        sec.put("fr_mem_order", "I", member_orders, 4)
        sec.put("fr_mem_o", "I", member_offsets, 4, expect=count + 1)
