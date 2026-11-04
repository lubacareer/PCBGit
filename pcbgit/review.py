"""Git snapshots, KiCad checks and report generation."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tempfile

from .geometry import GERBER_EXTENSIONS, STEP_EXTENSIONS, compare_files
from .dfm import check_board
from .sexpr import _parse


class ReviewError(Exception):
    pass


def git_environment() -> dict[str, str]:
    """Prevent inherited Git location overrides from changing the target repo."""
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
        environment.pop(key, None)
    return environment


def git(repo: Path, *args: str) -> bytes:
    """Run Git against the selected repository with a bounded execution time."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                              env=git_environment()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError(f"Git command failed: {exc}") from exc


def snapshot(repo: Path, ref: str, destination: Path) -> str:
    """Extract committed regular blobs, rejecting paths outside the destination."""
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    # git archive honors untrusted export-ignore attributes; enumerating the
    # committed tree prevents a change from hiding a design from its review.
    entries = git(repo, "ls-tree", "-r", "-z", "--long", "--full-tree", commit).split(b"\0")
    files: list[tuple[str, str, int]] = []
    total = 0
    for entry in entries:
        if not entry:
            continue
        try:
            header, name = entry.split(b"\t", 1)
            mode, kind, oid, size = header.split()
            path = name.decode("utf-8", "surrogateescape")
        except (ValueError, UnicodeError) as exc:
            raise ReviewError("Invalid Git tree entry") from exc
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ReviewError(f"Unsafe or unsupported repository entry: {path}")
        try:
            size_int = int(size)
        except ValueError as exc:
            raise ReviewError(f"Invalid Git object size: {path}") from exc
        relative = PurePosixPath(path)
        if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                or any(part in ("", ".") for part in path.split("/"))
                or "\\" in path or ":" in path):
            raise ReviewError(f"Unsafe repository path: {path}")
        if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ReviewError("Invalid Git object identifier")
        if size_int < 0 or size_int > 64 * 1024 * 1024:
            raise ReviewError(f"Repository file exceeds 64 MiB snapshot limit: {path}")
        total += size_int
        if total > 256 * 1024 * 1024 or len(files) >= 100000:
            raise ReviewError("Repository exceeds snapshot size or file count limit")
        files.append((path, oid.decode("ascii"), size_int))

    try:
        batch = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                               input="".join(oid + "\n" for _, oid, _ in files).encode(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=True, timeout=120, env=git_environment()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError(f"Git object extraction failed: {exc}") from exc
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ReviewError("Snapshot destination must not be a symlink")
    resolved_destination = destination.resolve()
    position = 0
    for name, oid, size in files:
        end_header = batch.find(b"\n", position)
        if end_header < 0 or batch[position:end_header] != f"{oid} blob {size}".encode():
            raise ReviewError(f"Unexpected Git object header for {name}")
        start = end_header + 1
        end = start + size
        if end >= len(batch) or batch[end:end + 1] != b"\n":
            raise ReviewError(f"Incomplete Git object for {name}")
        target = destination.joinpath(*PurePosixPath(name).parts)
        # Never follow pre-existing symlinks in a reused destination, even if
        # they happen to point inside it. Check resolved containment before and
        # after creating directories so a path cannot escape the snapshot root.
        current = destination
        for component in PurePosixPath(name).parts:
            current = current / component
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ReviewError(f"Symlink in snapshot destination: {name}")
        try:
            target.resolve().relative_to(resolved_destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.resolve().relative_to(resolved_destination)
        except (ValueError, OSError) as exc:
            raise ReviewError(f"Snapshot path escapes destination: {name}") from exc
        target.write_bytes(batch[start:end])
        position = end + 1
    if position != len(batch):
        raise ReviewError("Unexpected trailing Git object data")
    return commit


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    """List changed repository paths without relying on rename detection."""
    raw = git(repo, "diff", "--name-only", "-z", "--no-renames", base, head, "--")
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def run_tool(args: list[str]) -> tuple[int | None, str]:
    """Run a CAD tool without inherited distributor secrets; bound its output."""
    try:
        environment = os.environ.copy()
        for key in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "DIGIKEY_ACCESS_TOKEN",
                    "DIGIKEY_ACCOUNT_ID", "MOUSER_API_KEY"):
            environment.pop(key, None)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=180, env=environment)
        return result.returncode, result.stdout[-8000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)



def inner_copper_layers(path: Path) -> list[str]:
    """Return enabled canonical inner-copper layers from a KiCad board."""
    board = _parse(path.read_text(encoding="utf-8-sig"))
    if not board or board[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    sections = [node for node in board if isinstance(node, list) and node and node[0] == "layers"]
    if len(sections) != 1:
        raise ValueError("board has missing or ambiguous layers")
    inner = []
    for entry in sections[0][1:]:
        if not isinstance(entry, list) or len(entry) < 2:
            raise ValueError("invalid board layer entry")
        if len(entry) < 3 or not isinstance(entry[1], str):
            raise ValueError("invalid board layer definition")
        try:
            int(entry[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid board layer ordinal") from exc
        # The ordinal is stack ordering, not the copper index. KiCad's
        # canonical name is the second item; a custom display name is optional
        # and follows the layer type.
        name = entry[1]
        if re.fullmatch(r"In(?:[1-9]|[12][0-9]|30)\.Cu", name):
            inner.append(name)
        elif entry[2] in ("signal", "power", "mixed", "jumper") and name not in ("F.Cu", "B.Cu"):
            raise ValueError(f"unsupported copper layer name: {name}")
    if len(inner) != len(set(inner)):
        raise ValueError("duplicate inner copper layer")
    return sorted(inner, key=lambda name: int(name[2:-3]))


def kicad_file(path: Path, relative: str, output: Path, side: str, check: bool) -> dict:
    """Render one snapshot and optionally run its KiCad design rule check."""
    kind = "pcb" if path.suffix.lower() == ".kicad_pcb" else "sch"
    digest = hashlib.sha256(relative.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    output.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents stale SVG or check reports from being reused.
    dest = Path(tempfile.mkdtemp(prefix=f"pcbgit-{digest}-{side}-", dir=output))
    artifacts, findings = [], []
    if kind == "pcb":
        args = ["kicad-cli", "pcb", "export", "svg", "--layers", "F.Cu,B.Cu,Edge.Cuts,F.Silkscreen,B.Silkscreen", "--mode-single", "--output", str(dest / "board.svg"), str(path)]
    else:
        args = ["kicad-cli", "sch", "export", "svg", "--output", str(dest), str(path)]
    code, log = run_tool(args)
    if code == 0:
        artifacts.extend(p.relative_to(output).as_posix() for p in sorted(dest.glob("*.svg")))
        if not artifacts:
            findings.append({"status": "unknown", "message": "KiCad produced no SVG output."})
    else:
        findings.append({"status": "unknown", "message": f"SVG export failed: {log}"})
    if kind == "pcb":
        try:
            inner = inner_copper_layers(path)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            inner = []
            findings.append({"status": "unknown", "message": f"Cannot determine inner copper layers: {exc}"})
        for layer in inner:
            layer_svg = dest / f"layer-{layer}.svg"
            code, log = run_tool(["kicad-cli", "pcb", "export", "svg", "--layers",
                                  f"{layer},Edge.Cuts", "--mode-single", "--output",
                                  str(layer_svg), str(path)])
            if code == 0 and layer_svg.is_file():
                artifacts.append(layer_svg.relative_to(output).as_posix())
            else:
                findings.append({"status": "unknown", "message": f"SVG export failed for {layer}: {log}"})
    if check:
        name = "DRC" if kind == "pcb" else "ERC"
        report = dest / f"{name.lower()}.json"
        args = ["kicad-cli", kind, name.lower(), "--format", "json", "--exit-code-violations", "--output", str(report), str(path)]
        code, log = run_tool(args)
        status = "pass" if code == 0 else "fail" if code == 5 else "unknown"
        finding = {"status": status, "message": f"KiCad {name}: {status}"}
        if report.exists():
            artifacts.append(report.relative_to(output).as_posix())
            try:
                finding["details"] = json.loads(report.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                finding.update(status="unknown", message=f"KiCad {name} returned an unreadable report")
        elif status == "pass":
            finding.update(status="unknown", message=f"KiCad {name} produced no report")
        if code not in (0, 5):
            finding["message"] += f": {log}"
        findings.append(finding)
    return {"side": side, "artifacts": artifacts, "findings": findings}


def write_report(result: dict, output: Path) -> None:
    """Write machine-readable and HTML reports, escaping untrusted labels."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><title>PCBGit review</title>",
             "<style>body{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#172033}section{border-top:1px solid #ccd;padding:1rem 0}.images{display:flex;gap:1rem;flex-wrap:wrap}.image{flex:1;min-width:280px}img{width:100%;background:white;border:1px solid #bbb}pre{white-space:pre-wrap;overflow-wrap:anywhere}.fail,.unknown{color:#a22}</style>",
             f"<h1>PCBGit review</h1><p>Status: <strong class='{esc(result['status'])}'>{esc(result['status'])}</strong></p>",
             f"<p>Base: {esc(result['base'])}<br>Head: {esc(result['head'])}</p>"]
    for finding in result.get("findings", []):
        parts.append(f"<p class='{esc(finding.get('status', ''))}'>{esc(finding.get('message', ''))}</p>")
    for item in result.get("files", []):
        parts.append(f"<section><h2>{esc(item['path'])}</h2><div class='images'>")
        for side in item.get("sides", []):
            parts.append(f"<div class='image'><h3>{esc(side['side'])}</h3>")
            for artifact in side.get("artifacts", []):
                if artifact.endswith(".svg"):
                    parts.append(f"<p>{esc(Path(artifact).name)}</p><img src='{esc(artifact)}' alt='{esc(side['side'])} rendering'>")
                else:
                    parts.append(f"<p><a href='{esc(artifact)}'>Download details</a></p>")
            for finding in side.get("findings", []):
                parts.append(f"<p class='{esc(finding.get('status', ''))}'>{esc(finding.get('message', ''))}</p>")
                if finding.get("evidence"):
                    parts.append(f"<pre>{esc(json.dumps(finding['evidence'], indent=2, ensure_ascii=True))}</pre>")
            parts.append("</div>")
        parts.append("</div></section>")
    parts.append("<p><a href='report.json'>Machine-readable report</a></p></html>")
    (output / "index.html").write_text("".join(parts), encoding="utf-8")


def snapshot_path(root: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value or ":" in value:
        raise ReviewError("Configured paths must be relative paths within the repository")
    return root.joinpath(*path.parts)


def schematic_roots(root: Path) -> tuple[set[str], list[dict]]:
    """Identify root sheets; never infer roots from an incomplete hierarchy."""
    paths = {path.relative_to(root).as_posix(): path for path in root.rglob("*")
             if path.is_file() and path.suffix.lower() == ".kicad_sch"}
    children = {name: set() for name in paths}
    errors = []
    for name, path in paths.items():
        try:
            node = _parse(path.read_text(encoding="utf-8-sig"))
            if not node or node[0] != "kicad_sch":
                raise ValueError("not a KiCad schematic")
            for sheet in (item for item in node if isinstance(item, list) and item and item[0] == "sheet"):
                properties = [item for item in sheet if isinstance(item, list) and len(item) >= 3
                              and item[0] == "property" and str(item[1]).casefold() in ("sheetfile", "sheet file")]
                if len(properties) != 1 or not isinstance(properties[0][2], str):
                    raise ValueError("sheet has missing or ambiguous Sheetfile property")
                child = properties[0][2]
                if not child or "\\" in child or ":" in child or "$" in child or child.startswith("/"):
                    raise ValueError("sheet path is unsupported or ambiguous")
                child = posixpath.normpath(posixpath.join(posixpath.dirname(name), child))
                if child == ".." or child.startswith("../") or child not in paths:
                    raise ValueError(f"sheet file is missing or outside the snapshot: {child}")
                children[name].add(child)
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
            errors.append({"status": "unknown", "message": f"Schematic hierarchy could not be resolved for {name}: {exc}"})
    if errors:
        return set(), errors
    indegree = {name: 0 for name in paths}
    for references in children.values():
        for child in references:
            indegree[child] += 1
    roots = {name for name, count in indegree.items() if count == 0}
    pending = list(roots)
    visited = 0
    while pending:
        name = pending.pop()
        visited += 1
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                pending.append(child)
    if visited != len(paths):
        return set(), [{"status": "unknown", "message": "Schematic hierarchy contains a cycle; ERC roots cannot be determined."}]
    return roots, []


def review(repo: Path, base_ref: str, head_ref: str, output: Path, profile: str | None = None) -> dict:
    """Compare merge-base and head snapshots and check all head designs."""
    repo, output = repo.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pcbgit-") as temp:
        root = Path(temp)
        base_tip = git(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}").decode().strip()
        head_commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{head_ref}^{{commit}}").decode().strip()
        base_commit = git(repo, "merge-base", base_tip, head_commit).decode().strip()
        snapshot(repo, base_commit, root / "base")
        snapshot(repo, head_commit, root / "head")
        profile_path = snapshot_path(root / "head", profile) if profile else None
        version_code, version = run_tool(["kicad-cli", "version"])
        supported = version_code == 0 and version.strip().split(".")[0] == "9"
        result = {"schema_version": 1, "base": base_commit, "head": head_commit,
                  "status": "pass", "files": [], "findings": []}
        roots, hierarchy_findings = schematic_roots(root / "head")
        result["findings"].extend(hierarchy_findings)
        if not supported:
            result["findings"].append({"status": "unknown", "message": f"KiCad 9 is required: {version.strip()}"})
        changed = changed_files(repo, base_commit, head_commit)
        for name in changed:
            relative = PurePosixPath(name)
            if relative.suffix.lower() in STEP_EXTENSIONS | GERBER_EXTENSIONS:
                old = root / "base" / Path(*relative.parts)
                new = root / "head" / Path(*relative.parts)
                digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:16]
                comparison = compare_files(old if old.is_file() else None, new if new.is_file() else None,
                                           output / "assets" / digest / "comparison")
                status = "pass" if comparison["status"] == "ok" else "unknown"
                findings = [{**f, "status": f.get("status", status if f.get("severity") == "error" else "info")}
                            for f in comparison["findings"]]
                if status == "unknown":
                    findings.append({"status": "unknown", "message": "Geometry comparison could not be completed."})
                artifacts = [Path(p).relative_to(output).as_posix() for p in comparison["artifacts"]]
                result["files"].append({"path": name, "kind": comparison["kind"], "change": comparison["change"],
                                        "sides": [{"side": "comparison", "artifacts": artifacts, "findings": findings}]})
                continue
            if relative.suffix.lower() not in (".kicad_pcb", ".kicad_sch"):
                continue
            item = {"path": name, "sides": []}
            for side in ("base", "head"):
                path = root / side / Path(*relative.parts)
                if path.is_file():
                    check = side == "head" and (relative.suffix.lower() == ".kicad_pcb" or name in roots)
                    item["sides"].append(kicad_file(path, name, output, side, check=check))
            result["files"].append(item)
        # Run checks even when a project file did not itself change.
        changed_set = set(changed)
        for path in sorted((root / "head").rglob("*")):
            if path.is_file() and path.suffix.lower() in (".kicad_pcb", ".kicad_sch"):
                name = path.relative_to(root / "head").as_posix()
                if name not in changed_set:
                    check = path.suffix.lower() == ".kicad_pcb" or name in roots
                    result["files"].append({"path": name, "sides": [kicad_file(path, name, output, "head", check=check)]})
        for item in result["files"]:
            if PurePosixPath(item["path"]).suffix.lower() != ".kicad_pcb":
                continue
            board = root / "head" / Path(*PurePosixPath(item["path"]).parts)
            if not board.is_file():
                continue
            dfm = check_board(board, profile_path)
            status = {"failed": "fail", "unknown": "unknown", "partial": "info"}[dfm["status"]]
            findings = [{"status": "info", "message": "DFM coverage is partial; unsupported checks require independent review.",
                         "evidence": {"profile": dfm["profile"], "supported": dfm["supported"], "unsupported": dfm["unsupported"]}}]
            findings.extend({**f, "status": f.get("status", status if f.get("severity") == "error" else "info")}
                            for f in dfm["findings"])
            item["sides"].append({"side": "DFM", "artifacts": [], "findings": findings})
        statuses = [f.get("status") for f in result["findings"]]
        statuses.extend(f.get("status") for item in result["files"] for side in item["sides"] for f in side["findings"])
        result["status"] = "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
        write_report(result, output)
        return result
