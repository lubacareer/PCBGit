from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from pcbgit.geometry import _hole_findings, compare_files
from pcbgit.cli import main
from pcbgit.review import review


GERBER = """%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.300*%
D10*
X000000Y000000D02*
X010000Y000000D01*
M02*
"""


def test_review_includes_changed_gerber(tmp_path):
    pytest.importorskip("gerbonara")
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "layer.gbr").write_text(GERBER)
    git("add", ".")
    git("commit", "-m", "before")
    base = git("rev-parse", "HEAD")
    (repo / "layer.gbr").write_text(GERBER.replace("X010000", "X020000"))
    git("commit", "-am", "after")
    with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")):
        result = review(repo, base, "HEAD", tmp_path / "report", skip_inventory=True)
    assert result["status"] == "pass"
    assert result["files"][0]["kind"] == "gerber"
    assert (tmp_path / "report" / result["files"][0]["sides"][0]["artifacts"][0]).is_file()


def test_unknown_extension_and_missing_paths(tmp_path):
    assert compare_files(None, tmp_path / "sample.xyz", tmp_path)["status"] == "unknown"
    with pytest.raises(ValueError):
        compare_files(None, None, tmp_path)


def test_unique_hole_move_and_ambiguous_matches():
    def hole(x):
        return {"axis_point_mm": [x, 0, 0], "axis_direction": [0, 0, 1], "diameter_mm": 1.0, "axis_interval_mm": [0, 2]}
    moved = _hole_findings([hole(0)], [hole(2)])
    assert moved[0]["code"] == "hole_moved"
    assert moved[0]["status"] == "unknown"
    assert moved[0]["evidence"]["axis_shift_mm"] == 2
    ambiguous = _hole_findings([hole(0)], [hole(1), hole(2)])
    assert any(f["code"] == "hole_match_ambiguous" for f in ambiguous)
    assert next(f for f in ambiguous if f["code"] == "hole_match_ambiguous")["status"] == "unknown"
    assert not any(f["code"] == "hole_moved" for f in ambiguous)
    assert _hole_findings([hole(0), hole(1)], [hole(0), hole(1)]) == []
    assert _hole_findings([hole(0), hole(1)], [hole(0), hole(2)])[0]["code"] == "hole_moved"


def test_axial_extent_matching_never_guesses_among_coaxial_candidates():
    def hole(interval):
        return {"axis_point_mm": [0, 0, 0], "axis_direction": [0, 0, 1], "diameter_mm": 1.0, "axis_interval_mm": interval}
    before = [hole([0, 2]), hole([8, 10])]
    removed = _hole_findings(before, [hole([0, 2])])
    assert len(removed) == 1 and removed[0]["code"] == "hole_unmatched_before"
    assert removed[0]["evidence"]["before"]["axis_interval_mm"] == [8, 10]
    ambiguity = _hole_findings([hole([0, 2]), hole([3, 5])], [hole([0, 3]), hole([3, 6])])
    assert any(f["code"] == "hole_match_ambiguous" and f["status"] == "unknown" for f in ambiguity)
    assert not any(f["code"] == "hole_depth_changed" for f in ambiguity)
    disjoint = _hole_findings([hole([0, 2])], [hole([5, 7])])
    assert disjoint[0]["code"] == "hole_match_ambiguous" and disjoint[0]["status"] == "unknown"
    moved = _hole_findings([hole([0, 2])], [hole([1, 3])])
    assert moved[0]["code"] == "hole_axial_moved" and moved[0]["status"] == "unknown"


def test_review_propagates_step_uncertainty_without_escalating_other_warnings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "part.step").write_text("before")
    git("add", ".")
    git("commit", "-m", "before")
    base = git("rev-parse", "HEAD")
    (repo / "part.step").write_text("after")
    git("commit", "-am", "after")
    warning = {"code": "hole_moved", "severity": "warning", "message": "Possible move"}
    unknown = {"code": "step_tolerances_unknown", "severity": "warning", "status": "unknown", "message": "Tolerances unknown"}
    comparison = {"kind": "step", "change": "changed", "status": "ok", "findings": [warning, unknown], "artifacts": []}
    with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), patch("pcbgit.review.compare_files", return_value=comparison):
        result = review(repo, base, "HEAD", tmp_path / "report", skip_inventory=True)
        assert main(["review", "--repo", str(repo), "--base", base, "--head", "HEAD", "--output", str(tmp_path / "cli-report"), "--skip-inventory"]) == 2
    findings = result["files"][0]["sides"][0]["findings"]
    assert result["status"] == "unknown"
    assert [finding["status"] for finding in findings] == ["info", "unknown"]
    assert "Tolerances unknown" in (tmp_path / "report" / "index.html").read_text(encoding="utf-8")
    comparison["findings"] = [warning]
    with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), patch("pcbgit.review.compare_files", return_value=comparison):
        assert review(repo, base, "HEAD", tmp_path / "other-report", skip_inventory=True)["status"] == "pass"


def test_gerber_render_and_absent_revision(tmp_path):
    pytest.importorskip("gerbonara")
    old, new = tmp_path / "old.gbr", tmp_path / "new.gbr"
    old.write_text(GERBER)
    new.write_text(GERBER.replace("X010000", "X020000"))
    result = compare_files(old, new, tmp_path / "render")
    assert result["status"] == "ok", result
    assert result["change"] == "changed"
    page = Path(result["artifacts"][0]).read_text(encoding="utf-8")
    assert "data:image/svg+xml;base64," in page
    assert "Overlay" in page
    added = compare_files(None, new, tmp_path / "added")
    assert added["status"] == "ok", added
    assert added["change"] == "added"
    assert "File absent" in Path(added["artifacts"][0]).read_text(encoding="utf-8")


def test_invalid_gerber_does_not_claim_success(tmp_path):
    pytest.importorskip("gerbonara")
    bad = tmp_path / "bad.gbr"
    bad.write_text("this is not gerber")
    result = compare_files(None, bad, tmp_path / "out")
    assert result["status"] == "error"
    assert result["findings"][-1]["code"] == "comparison_error"


def test_real_step_hole_move_and_viewer(tmp_path):
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.IFSelect import IFSelect_RetDone

    def make_step(path, x):
        block = BRepPrimAPI_MakeBox(10, 10, 2).Shape()
        drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 5, -1), gp_Dir(0, 0, 1)), 1, 4).Shape()
        body = BRepAlgoAPI_Cut(block, drill).Shape()
        writer = STEPControl_Writer()
        assert writer.Transfer(body, STEPControl_AsIs) == IFSelect_RetDone
        assert writer.Write(str(path)) == IFSelect_RetDone
    old, new = tmp_path / "old.step", tmp_path / "new.step"
    make_step(old, 3)
    make_step(new, 5)
    result = compare_files(old, new, tmp_path / "out")
    assert result["status"] == "ok", result
    moved = next(f for f in result["findings"] if f["code"] == "hole_moved")
    assert moved["status"] == "unknown"
    assert moved["evidence"]["before"]["axis_interval_mm"] == [0.0, 2.0]
    assert any(f["code"] == "step_tolerances_unknown" for f in result["findings"])
    viewer = Path(result["artifacts"][0]).read_text(encoding="utf-8")
    assert "removed" in viewer and "added" in viewer
    assert "https://" not in viewer


def test_real_step_coaxial_cavity_removal_and_blind_depth_change(tmp_path):
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.IFSelect import IFSelect_RetDone

    def make_step(path, bottom_depth, top_depth=None):
        body = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
        bottom = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(5, 5, -1), gp_Dir(0, 0, 1)), 1, bottom_depth + 1).Shape()
        body = BRepAlgoAPI_Cut(body, bottom).Shape()
        if top_depth is not None:
            # The opposite direction exercises canonicalized axis intervals.
            top = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(5, 5, 11), gp_Dir(0, 0, -1)), 1, top_depth + 1).Shape()
            body = BRepAlgoAPI_Cut(body, top).Shape()
        writer = STEPControl_Writer()
        assert writer.Transfer(body, STEPControl_AsIs) == IFSelect_RetDone
        assert writer.Write(str(path)) == IFSelect_RetDone

    two, one, deeper = (tmp_path / name for name in ("two.step", "one.step", "deeper.step"))
    make_step(two, 2, 2)
    make_step(one, 2)
    make_step(deeper, 3)
    removed = compare_files(two, one, tmp_path / "removed")
    assert removed["status"] == "ok", removed
    missing = [f for f in removed["findings"] if f["code"] == "hole_unmatched_before"]
    assert len(missing) == 1, removed
    assert missing[0]["status"] == "unknown"
    assert missing[0]["evidence"]["before"]["axis_interval_mm"] == [8.0, 10.0]
    depth = compare_files(one, deeper, tmp_path / "depth")
    assert depth["status"] == "ok", depth
    change = next(f for f in depth["findings"] if f["code"] == "hole_depth_changed")
    assert change["status"] == "unknown"
    assert change["evidence"]["before_extent_mm"] == 2.0
    assert change["evidence"]["after_extent_mm"] == 3.0


def test_split_cylindrical_faces_are_reported_unknown():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from pcbgit.geometry import _holes
    import math

    body = BRepPrimAPI_MakeBox(10, 10, 4).Shape()
    half_cylinder = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(5, 5, -1), gp_Dir(0, 0, 1)), 1, 6, math.pi).Shape()
    body = BRepAlgoAPI_Cut(body, half_cylinder).Shape()
    holes, uncertain = _holes(body)
    assert not holes
    assert any(f["code"] == "hole_face_unsupported" and f["status"] == "unknown" for f in uncertain)

    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    face = TopExp_Explorer(body, TopAbs_FACE).Current()
    _, open_shell = _holes(face)
    assert open_shell[0]["code"] == "hole_face_unsupported"
    assert open_shell[0]["status"] == "unknown"
