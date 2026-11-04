"""A deliberately limited, auditable set of PCB manufacturing checks.

Distances are nominal design values in millimetres, not finished dimensions.
Clearance covers straight-track pairs only; pads, zones, vias, arcs and same-net
manufacturing spacing need separate review and are always disclosed as gaps.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

DEFAULT_PROFILE = Path(__file__).parent / "profiles" / "jlcpcb-standard-2layer.json"
_TOKEN = re.compile(r'\s+|;[^\n]*|[()]|"(?:\\.|[^"\\])*"|[^\s();"]+')
_REQUIRED = (
    "min_track_width_mm", "min_track_clearance_mm", "min_via_drill_mm",
    "min_plated_drill_mm", "min_nonplated_drill_mm", "min_plated_slot_width_mm",
    "min_nonplated_slot_width_mm", "min_slot_length_width_ratio",
)


def _parse(text: str) -> list:
    """Parse KiCad's S-expressions without evaluating input or expanding symbols."""
    stack: list[list] = []
    roots: list[list] = []
    pos = 0
    for match in _TOKEN.finditer(text):
        if match.start() != pos:
            raise ValueError("invalid S-expression token")
        pos = match.end()
        token = match.group()
        if token.isspace() or token.startswith(";"):
            continue
        if token == "(":
            if len(stack) >= 128:
                raise ValueError("S-expression nesting exceeds 128")
            node: list = []
            if stack:
                stack[-1].append(node)
            else:
                roots.append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("unexpected closing parenthesis")
            stack.pop()
        elif not stack:
            raise ValueError("token outside S-expression")
        else:
            stack[-1].append(token[1:-1] if token.startswith('"') else token)
    if pos != len(text) or stack or len(roots) != 1:
        raise ValueError("incomplete S-expression")
    return roots[0]


def _children(node: list, name: str) -> list[list]:
    return [item for item in node if isinstance(item, list) and item and item[0] == name]


def _walk(node: list):
    for item in node:
        if isinstance(item, list):
            yield item
            yield from _walk(item)


def _number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numeric value")
    return number


def _field(node: list, name: str) -> list:
    matches = _children(node, name)
    if len(matches) != 1:
        raise ValueError(f"missing or repeated {name}")
    return matches[0]


def _xy(node: list, name: str) -> tuple[float, float]:
    values = _field(node, name)
    if len(values) != 3:
        raise ValueError(f"invalid {name}")
    return _number(values[1]), _number(values[2])


def _distance_point_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_squared))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _segment_distance(a, b, c, d) -> float:
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    # Proper crossing, including endpoint contact. Collinear overlap is handled
    # by the point-to-segment distances below.
    if cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0:
        return 0.0
    return min(_distance_point_segment(a, c, d), _distance_point_segment(b, c, d),
               _distance_point_segment(c, a, b), _distance_point_segment(d, a, b))


def check_board(board: Path, profile: Path | None = None) -> dict:
    """Check supported nominal PCB geometry against a JSON manufacturing profile.

    Status is ``failed`` for a violation, ``partial`` when checks have coverage
    gaps, and ``unknown`` when input or configuration cannot be read. A partial
    result must not be interpreted as proof that a board is manufacturable.
    """
    findings: list[dict] = []
    unsupported = [
        "Copper clearance for pads, vias, arcs, zones and other copper objects",
        "Same-net manufacturing clearance and unknown-net clearance",
        "Finished hole dimensions, stack-up assumptions and all other fabrication rules",
    ]
    result = {"status": "unknown", "findings": findings, "supported": [
        "Straight and arc track widths", "Nominal via, plated and non-plated hole and slot dimensions",
        "Straight-track clearance on the same layer for distinct known nets",
    ], "unsupported": unsupported, "profile": None}
    try:
        settings = json.loads((profile or DEFAULT_PROFILE).read_text(encoding="utf-8"))
        for key in _REQUIRED:
            value = _number(settings[key])
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            settings[key] = value
        root = _parse(Path(board).read_text(encoding="utf-8-sig"))
        if not root or root[0] != "kicad_pcb":
            raise ValueError("not a KiCad PCB S-expression")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        findings.append({"code": "dfm_input_error", "severity": "error", "message": str(exc)})
        return result
    result["profile"] = {key: settings.get(key) for key in ("name", "version", "source", "assumptions")}

    def violation(code, message, actual, minimum, **details):
        findings.append({"code": code, "severity": "error", "message": message,
                         "actual_mm": round(actual, 6), "minimum_mm": minimum, **details})

    tracks: list[dict] = []
    for item in _walk(root):
        if not item:
            continue
        try:
            if item[0] in ("segment", "arc"):
                width = _number(_field(item, "width")[1])
                if width <= 0:
                    raise ValueError("track width must be positive")
                if width < settings["min_track_width_mm"]:
                    violation("track_width", "Track width is below profile minimum", width,
                              settings["min_track_width_mm"], position=list(_xy(item, "start")))
                if item[0] == "segment":
                    net = _field(item, "net")[1]
                    layer = _field(item, "layer")[1]
                    tracks.append({"a": _xy(item, "start"), "b": _xy(item, "end"),
                                   "width": width, "net": net, "layer": layer})
            elif item[0] in ("via", "pad"):
                if item[0] == "pad":
                    if len(item) < 3 or item[2] not in ("thru_hole", "np_thru_hole"):
                        continue
                    plated = item[2] == "thru_hole"
                else:
                    if "micro" in item or "blind" in item:
                        unsupported.append("Microvias and blind/buried via drill constraints")
                        continue
                    plated = True
                drill = _field(item, "drill")
                slot = len(drill) > 1 and drill[1] == "oval"
                values = drill[2:] if slot else drill[1:]
                values = [v for v in values if isinstance(v, str)]
                if len(values) < (2 if slot else 1):
                    raise ValueError("missing hole diameter")
                dimensions = [_number(value) for value in values[:2 if slot else 1]]
                if any(value <= 0 for value in dimensions):
                    raise ValueError("drill dimensions must be positive")
                diameter = min(dimensions)
                key = ("min_plated_slot_width_mm" if plated else "min_nonplated_slot_width_mm") if slot else (
                    "min_via_drill_mm" if item[0] == "via" else
                    "min_plated_drill_mm" if plated else "min_nonplated_drill_mm")
                if diameter < settings[key]:
                    violation("slot_width" if slot else "hole_diameter",
                              "Nominal hole or slot width is below profile minimum", diameter,
                              settings[key], hole_type="via" if item[0] == "via" else item[2])
                if slot and plated and max(dimensions) / diameter < settings["min_slot_length_width_ratio"]:
                    findings.append({"code": "slot_ratio", "severity": "error",
                        "message": "Plated slot length-to-width ratio is below profile minimum",
                        "actual_ratio": round(max(dimensions) / diameter, 6),
                        "minimum_ratio": settings["min_slot_length_width_ratio"]})
        except (ValueError, IndexError, TypeError) as exc:
            findings.append({"code": "dfm_unsupported_item", "severity": "warning",
                             "message": f"Could not inspect {item[0]}: {exc}"})

    pairs = len(tracks) * (len(tracks) - 1) // 2
    if pairs > 250000:
        unsupported.append("Straight-track clearance skipped: more than 250,000 candidate pairs")
    else:
        for i, first in enumerate(tracks):
            for second in tracks[i + 1:]:
                if first["layer"] != second["layer"] or first["net"] == second["net"] or "0" in (first["net"], second["net"]):
                    continue
                distance = _segment_distance(first["a"], first["b"], second["a"], second["b"])
                clearance = max(0.0, distance - (first["width"] + second["width"]) / 2)
                if clearance + 1e-9 < settings["min_track_clearance_mm"]:
                    violation("track_clearance", "Copper clearance between distinct-net tracks is below profile minimum",
                              clearance, settings["min_track_clearance_mm"], layer=first["layer"],
                              start_a=list(first["a"]), start_b=list(second["a"]))
    result["status"] = "failed" if any(f["severity"] == "error" for f in findings) else "partial"
    return result
