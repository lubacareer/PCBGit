import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from pcbgit.dfm import DEFAULT_PROFILE, check_board
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
    target.write_text(target.read_text().replace("0.2", "0.05"))
    git("commit", "-am", "after")
    with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), patch("pcbgit.review.kicad_file", return_value={"side": "head", "artifacts": [], "findings": []}):
        result = review(repo, base, "HEAD", tmp_path / "report", skip_inventory=True)
    assert result["status"] == "fail"
    assert any(f.get("code") == "track_width" for f in result["files"][0]["sides"][-1]["findings"])
