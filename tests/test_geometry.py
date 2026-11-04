from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from pcbgit.geometry import _hole_findings, compare_files
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
        return {"axis_point_mm": [x, 0, 0], "axis_direction": [0, 0, 1], "diameter_mm": 1.0}
    moved = _hole_findings([hole(0)], [hole(2)])
    assert moved[0]["code"] == "hole_moved"
    assert moved[0]["evidence"]["axis_shift_mm"] == 2
    ambiguous = _hole_findings([hole(0)], [hole(1), hole(2)])
    assert any(f["code"] == "hole_match_ambiguous" for f in ambiguous)
    assert not any(f["code"] == "hole_moved" for f in ambiguous)
    assert _hole_findings([hole(0), hole(1)], [hole(0), hole(1)]) == []
    assert _hole_findings([hole(0), hole(1)], [hole(0), hole(2)])[0]["code"] == "hole_moved"


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
    assert any(f["code"] == "hole_moved" for f in result["findings"]), result
    assert any(f["code"] == "step_tolerances_unknown" for f in result["findings"])
    viewer = Path(result["artifacts"][0]).read_text(encoding="utf-8")
    assert "removed" in viewer and "added" in viewer
    assert "https://" not in viewer
