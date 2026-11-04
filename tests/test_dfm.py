import json
import itertools
from pathlib import Path
import random
import subprocess
from unittest.mock import patch

import pytest

from pcbgit.dfm import DEFAULT_PROFILE, _segment_distance, check_board
from pcbgit.review import review


def board(tmp_path, content):
    target = tmp_path / "test.kicad_pcb"
    target.write_text("(kicad_pcb " + content + ")", encoding="utf-8")
    return target


def test_manufacturer_profile_and_valid_board_are_explicitly_partial(tmp_path):
    profile = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    assert profile["source"].startswith("https://jlcpcb.com/")
    result = check_board(board(tmp_path, """
      (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))
      (segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (net 2))
      (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
      (footprint "X" (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5)))
    """))
    assert result["status"] == "partial"
    assert not result["findings"]
    assert result["unsupported"]


def test_detects_width_clearance_and_holes(tmp_path):
    result = check_board(board(tmp_path, """
      (segment (start 0 0) (end 1 0) (width 0.09) (layer "F.Cu") (net 1))
      (segment (start 0 0.14) (end 1 0.14) (width 0.1) (layer "F.Cu") (net 2))
      (via (at 1 1) (size 0.3) (drill 0.1) (layers "F.Cu" "B.Cu") (net 1))
      (footprint "X" (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 0.3)))
    """))
    assert result["status"] == "failed"
    assert {entry["code"] for entry in result["findings"]} == {
        "track_width", "track_clearance", "hole_diameter"}
    assert sum(entry["code"] == "hole_diameter" for entry in result["findings"]) == 2


def test_slots_and_custom_profile(tmp_path):
    profile = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    profile["min_track_width_mm"] = 0.25
    custom = tmp_path / "rules.json"
    custom.write_text(json.dumps(profile), encoding="utf-8")
    result = check_board(board(tmp_path, """
      (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (layer "F.Cu") (net 1))
      (footprint "X" (pad "1" thru_hole oval (at 0 0) (size 2 2) (drill oval 0.4 0.6))
        (pad "" np_thru_hole oval (at 0 0) (size 2 2) (drill oval 0.8 1.7)))
    """), custom)
    assert {f["code"] for f in result["findings"]} == {"track_width", "slot_width", "slot_ratio"}
    assert sum(f["code"] == "slot_width" for f in result["findings"]) == 2


def test_clearance_crossing_and_layer_and_net_exclusions(tmp_path):
    result = check_board(board(tmp_path, """
      (segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 1))
      (segment (start 1 -1) (end 1 1) (width 0.2) (layer "F.Cu") (net 2))
      (segment (start 0 0) (end 2 0) (width 0.2) (layer "B.Cu") (net 3))
      (segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 1))
    """))
    clearances = [f for f in result["findings"] if f["code"] == "track_clearance"]
    assert len(clearances) == 2
    assert all(f["actual_mm"] == 0 for f in clearances)


def test_invalid_inputs_are_unknown(tmp_path):
    target = tmp_path / "bad.kicad_pcb"
    target.write_text("(kicad_pcb (segment", encoding="utf-8")
    assert check_board(target)["status"] == "unknown"
    target.write_text("(kicad_pcb)", encoding="utf-8")
    profile = tmp_path / "bad.json"
    profile.write_text('{"min_track_width_mm": -1}', encoding="utf-8")
    assert check_board(target, profile)["status"] == "unknown"


@pytest.mark.parametrize("invalid", [True, False, "0.1", None, [], {}])
def test_profile_requires_json_numbers_but_board_numbers_are_strings(tmp_path, invalid):
    settings = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    settings["min_slot_length_width_ratio"] = invalid
    profile = tmp_path / "rules.json"
    profile.write_text(json.dumps(settings), encoding="utf-8")
    target = board(tmp_path, '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))')
    result = check_board(target, profile)
    assert result["status"] == "unknown"
    assert result["findings"][0]["code"] == "dfm_input_error"
    assert "must be a JSON number" in result["findings"][0]["message"]
    assert check_board(target)["status"] == "partial"


def test_overflowing_profile_number_is_unknown(tmp_path):
    settings = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    settings["min_track_width_mm"] = 10 ** 400
    profile = tmp_path / "overflow.json"
    profile.write_text(json.dumps(settings), encoding="utf-8")
    result = check_board(board(tmp_path, ""), profile)
    assert result["status"] == "unknown"
    assert result["findings"][0]["code"] == "dfm_input_error"


def _segment(x1, y1, x2, y2, width, layer, net):
    return f'(segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) (layer "{layer}") (net {net}))'


def test_large_board_finds_violation_after_pruning(tmp_path):
    # All tracks overlap in X but are separated in Y; the old total-pair cap
    # skipped every clearance check. The final pair is deliberately too close.
    tracks = [_segment(0, i, 20, i, 0.2, "F.Cu", i + 1) for i in range(1100)]
    tracks.append(_segment(0, 1099.15, 20, 1099.15, 0.2, "F.Cu", 2000))
    tracks.append(_segment(0, 1099.15, 20, 1099.15, 0.2, "B.Cu", 2001))
    result = check_board(board(tmp_path, "\n".join(tracks)))
    violations = [finding for finding in result["findings"] if finding["code"] == "track_clearance"]
    assert len(violations) == 1
    assert result["status"] == "failed"
    assert not any("clearance incomplete" in item for item in result["unsupported"])


def test_dense_candidate_limit_discloses_incomplete_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr("pcbgit.dfm._MAX_CLEARANCE_CANDIDATES", 3)
    # Same-net pairs are pruned after bounding boxes, but still count toward
    # the bound, preventing quadratic scans even when all exact checks skip.
    tracks = [_segment(0, 0, 1, 0, 0.2, "F.Cu", 1) for _ in range(10)]
    result = check_board(board(tmp_path, "\n".join(tracks)))
    assert result["status"] == "partial"
    assert any("clearance incomplete" in item for item in result["unsupported"])


def test_sweep_matches_brute_force_on_randomized_boards(tmp_path):
    rng = random.Random(61728)
    for _ in range(6):
        tracks = []
        for index in range(80):
            # Distinct start coordinates make the reported pair identity unique.
            x1 = round(rng.uniform(0, 5) + index * 0.00001, 5)
            y1 = round(rng.uniform(0, 5), 5)
            x2, y2 = round(x1 + rng.uniform(-1, 1), 5), round(y1 + rng.uniform(-1, 1), 5)
            tracks.append((x1, y1, x2, y2, rng.choice((0.1, 0.2, 0.4)), rng.choice(("F.Cu", "B.Cu")), rng.randint(0, 6)))
        result = check_board(board(tmp_path, "\n".join(_segment(*track) for track in tracks)))
        expected = set()
        for first, second in itertools.combinations(tracks, 2):
            if first[5] != second[5] or first[6] == second[6] or 0 in (first[6], second[6]):
                continue
            distance = _segment_distance(first[:2], first[2:4], second[:2], second[2:4])
            clearance = max(0, distance - (first[4] + second[4]) / 2)
            if clearance + 1e-9 < 0.1:
                expected.add(tuple(sorted((first[:2], second[:2]))))
        actual = {tuple(sorted((tuple(f["start_a"]), tuple(f["start_b"]))))
                  for f in result["findings"] if f["code"] == "track_clearance"}
        assert actual == expected
        assert not any("clearance incomplete" in item for item in result["unsupported"])


def test_unrecognized_geometry_is_not_silently_passed(tmp_path):
    result = check_board(board(tmp_path, """
      (via micro (at 0 0) (size 0.3) (drill 0.1))
      (segment (start 0 0) (end 1 0) (layer "F.Cu") (net 1))
    """))
    assert result["status"] == "partial"
    assert any("Microvias" in gap for gap in result["unsupported"])
    assert any(f["code"] == "dfm_unsupported_item" for f in result["findings"])


def test_review_fails_on_dfm_violation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    target = repo / "board.kicad_pcb"
    target.write_text('(kicad_pcb (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1)))')
    git("add", ".")
    git("commit", "-m", "before")
    base = git("rev-parse", "HEAD")
    target.write_text('(kicad_pcb (segment (start 0 0) (end 1 0) (width 0.05) (layer "F.Cu") (net 1)) '
                      '(segment (start 0 1) (end 1 1) (layer "F.Cu") (net 2)))')
    git("commit", "-am", "after")
    with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), patch("pcbgit.review.kicad_file", return_value={"side": "head", "artifacts": [], "findings": []}):
        result = review(repo, base, "HEAD", tmp_path / "report")
    assert result["status"] == "fail"
    findings = result["files"][0]["sides"][-1]["findings"]
    assert findings[0]["status"] == "info"
    assert next(f for f in findings if f.get("code") == "track_width")["status"] == "fail"
    assert next(f for f in findings if f.get("code") == "dfm_unsupported_item")["status"] == "info"
