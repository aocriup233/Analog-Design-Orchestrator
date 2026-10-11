import copy
import json
from unittest.mock import patch

from analog_agent.core import read_json, write_json
from analog_agent.layout_constraints import (constraints_draft, detect_constraints, topology,
                                            validate_constraints)
from analog_agent.layout_runs import prepare_layout
from analog_agent.state import status
from test_layout_runs import LayoutRunsFixture


def matching(name="group", source="user"):
    return {"id": name, "kind": "matching", "source": source, "rationale": "reviewed grouping",
            "groups": [["I0", "I1"]], "style": "none", "topology": "user", "tolerance_nm": 0}


class LayoutConstraintsTest(LayoutRunsFixture):
    def setUp(self):
        super().setUp()
        self.verify_source()
        self.run = prepare_layout(self.source)

    def test_schema_all_kinds_and_rejections(self):
        entries = [matching(), {"id": "sym", "kind": "symmetry", "source": "user", "rationale": "axis",
            "pairs": [["I0", "I1"]], "axis": {"direction": "x", "coordinate_nm": -2}, "tolerance_nm": 1},
            {"id": "net", "kind": "net_class", "source": "user", "rationale": "signal",
             "nets": ["OUT"], "class": "critical", "width_nm": 1, "spacing_nm": 0, "shielding": "VSS", "layers": ["metal"]},
            {"id": "ko", "kind": "keep_out", "source": "user", "rationale": "space", "bbox_nm": [[-1, 0], [1, 2]]},
            {"id": "route", "kind": "symmetric_routing", "source": "user", "rationale": "balance", "pairs": [["P", "N"]]},
            {"id": "vias", "kind": "max_vias", "source": "user", "rationale": "limit", "nets": ["OUT"], "max_count": 2}]
        document = {"schema_version": 1, "constraints": entries}
        self.assertEqual(len(validate_constraints(document)["constraints"]), 6)
        for key, value in (("style", "unknown"), ("tolerance_nm", -1), ("source", "candidate"), ("groups", [["I0", "I0"]])):
            bad = matching()
            bad[key] = value
            with self.assertRaises(ValueError):
                validate_constraints({"schema_version": 1, "constraints": [bad]})
        with self.assertRaises(ValueError):
            validate_constraints({"schema_version": 1, "constraints": [matching(), matching()]})

    def test_hierarchy_and_all_detection_patterns(self):
        text = """subckt stage (P N S)
M0 (D0 P S B) dev
M1 (D1 N S B) dev
M2 (G G E B) dev
M3 (Q G E B) dev
M4 (TOP C D0 B) dev
R0 (P N) resistor r=1k
R1 (P S) resistor r=1k
C0 (P N) capacitor c=1p
C1 (P S) capacitor c=1p
ends stage
X0 (INP INN TAIL) stage
"""
        graph = topology(text, {"models": {"dev": {"kind": "mos", "terminals": ["d", "g", "s", "body"]}}})
        kinds = {c["topology"] for c in detect_constraints(graph)}
        self.assertEqual(kinds, {"differential_pair", "current_mirror", "cascode", "resistor", "capacitor"})
        self.assertTrue(all(d["name"].startswith("X0/") for d in graph["devices"]))
        self.assertEqual(graph["devices"][0]["terms"]["p"], "INP")
        self.assertEqual(detect_constraints(graph), detect_constraints(graph))

    def test_parser_does_not_follow_includes_or_guess_models(self):
        graph = topology('include "private-model.scs"\nM0 (A B C) unknown\n')
        self.assertEqual(graph["devices"], [])
        self.assertEqual(len(graph["warnings"]), 2)
        with self.assertRaises(ValueError):
            topology("subckt loop ()\nX0 () loop\nends loop\nX1 () loop")

    def test_draft_is_deterministic_user_wins_and_no_bridge(self):
        user = {"schema_version": 1, "constraints": [matching()]}
        write_json(self.root / "private/layout/constraints.json", user)
        selected = {"schema": 1, "status": "READY", "entries": [{"id": "reviewed", "kind": "layout_constraints",
            "summary": json.dumps({"schema_version": 1, "constraints": [{**matching(), "style": "interdigitated"}]})}]}
        with patch("analog_agent.layout_constraints.memory.retrieve", return_value=selected), \
             patch("analog_agent.layout_bridge.read_placement") as bridge:
            first = constraints_draft(self.run)
            second = constraints_draft(self.run)
        self.assertEqual(first["sha256"], second["sha256"])
        bridge.assert_not_called()
        self.assertEqual(status(self.run), "CONSTRAINTS_DRAFTED")
        item = read_json(self.run / first["path"])["constraints"][0]
        self.assertEqual(item["source"], "user")
        self.assertEqual(item["style"], "none")

    def test_real_memory_review_filter_and_caps(self):
        from analog_agent import memory
        path = self.root / "private/memory.json"
        summary = json.dumps({"schema_version": 1, "constraints": [matching()]}, separators=(",", ":"))
        entries = [{"id": name, "kind": "layout_constraints", "status": state, "version": 1,
                    "scope": {}, "summary": summary, "evidence": [{"path": "evidence.json", "sha256": "a" * 64}]}
                   for name, state in (("a", "reviewed"), ("b", "candidate"), ("c", "rejected"), ("d", "reviewed"))]
        write_json(path, {"schema": 1, "entries": entries})
        cfg = {"_project_root": str(self.root), "memory": {"path": "private/memory.json", "max_items": 1, "max_chars": 500}}
        memory.validate_config(cfg)
        snapshot = memory.retrieve(cfg)
        self.assertEqual([e["id"] for e in snapshot["entries"]], ["a"])
        with patch("analog_agent.layout_constraints.memory.retrieve", return_value=snapshot):
            result = constraints_draft(self.run)
        self.assertEqual(read_json(self.run / result["path"])["constraints"][0]["source"], "memory:a")
