"""A deliberately limited, auditable set of PCB manufacturing checks.

Distances are nominal design values in millimetres, not finished dimensions.
Clearance covers straight-track pairs only; pads, zones, vias, arcs and same-net
manufacturing spacing need separate review and are always disclosed as gaps.
"""
from __future__ import annotations

import json
import heapq
import math
from pathlib import Path
import secrets
from typing import Any

from .sexpr import _parse

DEFAULT_PROFILE = Path(__file__).parent / "profiles" / "jlcpcb-standard-2layer.json"
_REQUIRED = (
    "min_track_width_mm", "min_track_clearance_mm", "min_via_drill_mm",
    "min_plated_drill_mm", "min_nonplated_drill_mm", "min_plated_slot_width_mm",
    "min_nonplated_slot_width_mm", "min_slot_length_width_ratio",
)
_MAX_CLEARANCE_CANDIDATES = 250_000


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


def _finite(value: float) -> float:
    """Reject overflow instead of reporting an unreliable geometric result."""
    if not math.isfinite(value):
        raise ValueError("non-finite clearance calculation")
    return value


def _mul(first: float, second: float) -> float:
    value = _finite(first * second)
    if value == 0 and first != 0 and second != 0:
        raise ValueError("clearance calculation underflow")
    return value


def _distance_point_segment(p, a, b) -> float:
    dx, dy = _finite(b[0] - a[0]), _finite(b[1] - a[1])
    length_squared = _finite(_mul(dx, dx) + _mul(dy, dy))
    if length_squared == 0:
        return _finite(math.dist(p, a))
    px, py = _finite(p[0] - a[0]), _finite(p[1] - a[1])
    numerator = _finite(_mul(px, dx) + _mul(py, dy))
    t = max(0.0, min(1.0, _finite(numerator / length_squared)))
    closest = (_finite(a[0] + _mul(t, dx)), _finite(a[1] + _mul(t, dy)))
    return _finite(math.dist(p, closest))


def _segment_distance(a, b, c, d) -> float:
    def cross(p, q, r):
        x1, y1 = _finite(q[0] - p[0]), _finite(q[1] - p[1])
        x2, y2 = _finite(r[0] - p[0]), _finite(r[1] - p[1])
        return _finite(_mul(x1, y2) - _mul(y1, x2))

    def opposite(first, second):
        # Avoid overflowing the product of two otherwise finite cross products.
        return (first < 0 < second) or (second < 0 < first)

    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    if opposite(ab_c, ab_d) and opposite(cd_a, cd_b):
        return 0.0
    return min(_distance_point_segment(a, c, d), _distance_point_segment(b, c, d),
               _distance_point_segment(c, a, b), _distance_point_segment(d, a, b))


class _IntervalNode:
    """Randomized interval tree node storing its subtree's maximum end."""
    __slots__ = ("key", "end", "box", "priority", "left", "right", "max_end")

    def __init__(self, key, end, box):
        self.key, self.end, self.box = key, end, box
        self.priority = secrets.randbits(64)
        self.left = self.right = None
        self.max_end = end


def _refresh(node):
    if node:
        node.max_end = max(node.end, node.left.max_end if node.left else -math.inf,
                           node.right.max_end if node.right else -math.inf)
    return node


def _insert(node, new):
    if node is None:
        return new
    if new.key < node.key:
        node.left = _insert(node.left, new)
        if node.left.priority < node.priority:
            child = node.left
            node.left = child.right
            child.right = _refresh(node)
            return _refresh(child)
    else:
        node.right = _insert(node.right, new)
        if node.right.priority < node.priority:
            child = node.right
            node.right = child.left
            child.left = _refresh(node)
            return _refresh(child)
    return _refresh(node)


def _merge_intervals(left, right):
    if not left:
        return right
    if not right:
        return left
    if left.priority < right.priority:
        left.right = _merge_intervals(left.right, right)
        return _refresh(left)
    right.left = _merge_intervals(left, right.left)
    return _refresh(right)


def _remove(node, key):
    if node is None:
        return None
    if node.key == key:
        return _merge_intervals(node.left, node.right)
    if key < node.key:
        node.left = _remove(node.left, key)
    else:
        node.right = _remove(node.right, key)
    return _refresh(node)


def _overlapping(node, low, high):
    """Yield intervals intersecting [low, high], pruning entire subtrees."""
    pending = [node] if node else []
    while pending:
        node = pending.pop()
        if node.max_end < low:
            continue
        if node.left:
            pending.append(node.left)
        if node.key[0] <= high:
            if node.end >= low:
                yield node.box
            if node.right:
                pending.append(node.right)


def check_board(board: Path, profile: Path | None = None) -> dict:
    """Check supported nominal PCB geometry against a JSON manufacturing profile.

    Status is ``failed`` for a violation, ``partial`` when checks have coverage
    gaps, and ``unknown`` when an evaluated check cannot be verified. A partial
    result must not be interpreted as proof that a board is manufacturable.
    """
    findings: list[dict] = []
    unsupported = [
        "Copper clearance for pads, vias, arcs, zones and other copper objects",
        "Same-net manufacturing clearance",
        "Finished hole dimensions, stack-up assumptions and all other fabrication rules",
    ]
    result = {"status": "unknown", "findings": findings, "supported": [
        "Straight and arc track widths", "Nominal via, plated and non-plated hole and slot dimensions",
        "Straight-track clearance on the same layer for distinct known nets; unknown-net proximity is flagged for review",
    ], "unsupported": unsupported, "profile": None}
    try:
        settings = json.loads((profile or DEFAULT_PROFILE).read_text(encoding="utf-8"))
        for key in _REQUIRED:
            if isinstance(settings[key], bool) or not isinstance(settings[key], (int, float)):
                raise ValueError(f"{key} must be a JSON number")
            value = _number(settings[key])
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            settings[key] = value
        optional_ratio = "min_nonplated_slot_length_width_ratio"
        if optional_ratio in settings:
            raw = settings[optional_ratio]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"{optional_ratio} must be a JSON number")
            ratio = _number(raw)
            if ratio <= 0:
                raise ValueError(f"{optional_ratio} must be positive")
            settings[optional_ratio] = ratio
        root = _parse(Path(board).read_text(encoding="utf-8-sig"))
        if not root or root[0] != "kicad_pcb":
            raise ValueError("not a KiCad PCB S-expression")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        findings.append({"code": "dfm_input_error", "severity": "error", "message": str(exc)})
        return result
    result["profile"] = {key: settings.get(key) for key in ("name", "version", "source", "assumptions")}

    def violation(code, message, actual, minimum, **details):
        findings.append({"code": code, "severity": "error", "message": message,
                         "actual_mm": round(actual, 6), "minimum_mm": minimum, **details})

    tracks: list[dict] = []
    item_unknown = False
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
                    net_field, layer_field = _field(item, "net"), _field(item, "layer")
                    if (len(net_field) != 2 or not isinstance(net_field[1], str)
                            or not net_field[1].isascii() or not net_field[1].isdigit()):
                        raise ValueError("track net must be a non-negative integer")
                    if (len(layer_field) != 2 or not isinstance(layer_field[1], str)
                            or not layer_field[1]):
                        raise ValueError("track layer must be a non-empty string")
                    net, layer = net_field[1], layer_field[1]
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
                ratio_limit = (settings["min_slot_length_width_ratio"] if plated else
                               settings.get("min_nonplated_slot_length_width_ratio"))
                if slot and ratio_limit is not None and max(dimensions) / diameter < ratio_limit:
                    findings.append({"code": "slot_ratio", "severity": "error",
                        "message": f"{'Plated' if plated else 'Non-plated'} slot length-to-width ratio is below profile minimum",
                        "actual_ratio": round(max(dimensions) / diameter, 6),
                        "minimum_ratio": ratio_limit})
        except (ValueError, IndexError, TypeError, OverflowError) as exc:
            item_unknown = True
            findings.append({"code": "dfm_unsupported_item", "severity": "warning", "status": "unknown",
                             "message": f"Could not inspect {item[0]}: {exc}"})

    # Expanding each centerline box by half its width plus half the required
    # clearance cannot discard any pair that violates the distance check.
    # Sweep each layer independently, pruning boxes on both axes before the
    # exact segment calculation. Bound overlapping candidates for dense boards
    # and disclose any unfinished coverage.
    layers: dict[str, list[tuple]] = {}
    numeric_unknown = False
    unknown_net = False
    for track in tracks:
        try:
            radius = _finite(_finite(track["width"] + settings["min_track_clearance_mm"]) / 2)
            a, b = track["a"], track["b"]
            box = (_finite(min(a[0], b[0]) - radius), _finite(max(a[0], b[0]) + radius),
                   _finite(min(a[1], b[1]) - radius), _finite(max(a[1], b[1]) + radius), track)
        except ValueError:
            numeric_unknown = True
            continue
        layers.setdefault(track["layer"], []).append(box)
    candidates = 0
    limited = False
    for boxes in layers.values():
        boxes.sort(key=lambda box: box[0])
        tree = None
        expiry = []
        for index, current in enumerate(boxes):
            while expiry and expiry[0][0] < current[0]:
                _, key = heapq.heappop(expiry)
                tree = _remove(tree, key)
            for old in _overlapping(tree, current[2], current[3]):
                candidates += 1
                if candidates > _MAX_CLEARANCE_CANDIDATES:
                    limited = True
                    break
                first, second = old[4], current[4]
                if first["net"] == second["net"] and first["net"] != "0":
                    continue
                try:
                    distance = _segment_distance(first["a"], first["b"], second["a"], second["b"])
                    widths = _finite(first["width"] + second["width"])
                    clearance = max(0.0, _finite(distance - widths / 2))
                except ValueError:
                    numeric_unknown = True
                    continue
                if clearance + 1e-9 < settings["min_track_clearance_mm"]:
                    if "0" in (first["net"], second["net"]):
                        unknown_net = True
                        findings.append({"code": "track_clearance_unknown_net", "severity": "warning", "status": "unknown",
                                         "message": "Track clearance is below profile minimum but an unassigned net makes connectivity uncertain",
                                         "actual_mm": round(clearance, 6), "minimum_mm": settings["min_track_clearance_mm"],
                                         "layer": first["layer"], "start_a": list(first["a"]), "start_b": list(second["a"])})
                    else:
                        violation("track_clearance", "Copper clearance between distinct-net tracks is below profile minimum",
                                  clearance, settings["min_track_clearance_mm"], layer=first["layer"],
                                  start_a=list(first["a"]), start_b=list(second["a"]))
            if limited:
                break
            key = (current[2], index)
            tree = _insert(tree, _IntervalNode(key, current[3], current))
            heapq.heappush(expiry, (current[1], key))
        if limited:
            break
    if limited:
        unsupported.append("Straight-track clearance incomplete: candidate limit exceeded")
        findings.append({"code": "dfm_clearance_candidate_limit", "severity": "warning", "status": "unknown",
                         "message": "Straight-track clearance could not be fully verified because the candidate limit was exceeded"})
    if numeric_unknown:
        unsupported.append("Straight-track clearance incomplete: non-finite geometry calculation")
        findings.append({"code": "dfm_clearance_numeric_unknown", "severity": "error", "status": "unknown",
                         "message": "Straight-track clearance could not be verified because a geometry calculation overflowed or underflowed"})
    has_violation = any(f["severity"] == "error" and f["code"] != "dfm_clearance_numeric_unknown" for f in findings)
    result["status"] = "failed" if has_violation else "unknown" if numeric_unknown or limited or unknown_net or item_unknown else "partial"
    return result
