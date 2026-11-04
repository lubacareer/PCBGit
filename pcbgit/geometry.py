"""Optional STEP and Gerber visual comparison.

These comparisons are evidence for a human reviewer, not a manufacturing
approval. STEP feature recognition is intentionally conservative.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

STEP_EXTENSIONS = {".step", ".stp"}
GERBER_EXTENSIONS = {".gbr", ".ger", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gko", ".gm1", ".g1", ".g2", ".g3", ".g4"}
COMPARISON_TOLERANCE_MM = 0.01


def _finding(code, message, severity="info", *, status=None, **evidence):
    finding = {"code": code, "severity": severity, "message": message, "evidence": evidence}
    if status is not None:
        finding["status"] = status
    return finding


def _changed(base, head):
    if base is None:
        return "added"
    if head is None:
        return "removed"
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        return h.digest()
    return "unchanged" if digest(base) == digest(head) else "changed"


def compare_files(base: Path | None, head: Path | None, output_dir: Path) -> dict:
    """Compare one file pair. Inputs must be local files; output is JSON-safe.

    Status is ``ok``, ``unknown`` (an optional parser is unavailable) or
    ``error`` (input could not be processed). Output files are self-contained.
    """
    base = Path(base) if base is not None else None
    head = Path(head) if head is not None else None
    path = head or base
    if path is None:
        raise ValueError("At least one input file is required")
    suffix = path.suffix.lower()
    kind = "step" if suffix in STEP_EXTENSIONS else "gerber" if suffix in GERBER_EXTENSIONS else "unsupported"
    result = {"kind": kind, "status": "ok", "findings": [], "artifacts": [], "change": None}
    if kind == "unsupported":
        result.update(status="unknown", findings=[_finding("unsupported_format", f"Unsupported file extension: {suffix}", "warning")])
        return result
    try:
        result["change"] = _changed(base, head)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if kind == "step":
            _compare_step(base, head, output_dir, result)
        else:
            _compare_gerber(base, head, output_dir, result)
    except ImportError as exc:
        result["status"] = "unknown"
        result["findings"].append(_finding("dependency_missing", f"Cannot compare {kind}: optional dependency unavailable ({exc.name}).", "warning"))
    except Exception as exc:
        result["status"] = "error"
        result["findings"].append(_finding("comparison_error", f"Could not process {kind}: {type(exc).__name__}: {exc}", "error"))
    return result


def _compare_gerber(base, head, output_dir, result):
    from gerbonara import GerberFile
    old = GerberFile.open(base, enable_includes=False) if base else None
    new = GerberFile.open(head, enable_includes=False) if head else None
    boxes = [f.bounding_box() for f in (old, new) if f is not None]
    boxes = [box for box in boxes if box and all(p is not None for pair in box for p in pair)]
    if not boxes:
        raise ValueError("Gerber contains no drawable geometry")
    bounds = ((min(b[0][0] for b in boxes), min(b[0][1] for b in boxes)),
              (max(b[1][0] for b in boxes), max(b[1][1] for b in boxes)))
    images = []
    for label, gerber, color in (("Before", old, "#d62235"), ("After", new, "#15803d")):
        if gerber is None:
            images.append((label, ""))
            continue
        svg = str(gerber.to_svg(force_bounds=bounds, fg=color, bg="transparent"))
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
        images.append((label, uri))
    panels = "".join(f'<section><h2>{label}</h2>' + (f'<img alt="{label} Gerber" src="{uri}">' if uri else '<p>File absent</p>') + '</section>' for label, uri in images)
    overlay = "".join(f'<img alt="{label} overlay" src="{uri}">' for label, uri in images if uri)
    report = output_dir / "gerber-view.html"
    report.write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Gerber comparison</title><style>body{font:16px system-ui;background:#f4f5f7;color:#17202a;margin:2rem}.panels{display:flex;gap:1rem}section{flex:1;min-width:0;background:white;padding:1rem}img{width:100%;max-height:65vh;object-fit:contain}.overlay{position:relative;display:grid;background:white}.overlay img{grid-area:1/1;opacity:.65}</style><h1>Gerber comparison</h1><p>Before: red; after: green. Overlay is a visual aid and is not a Boolean geometry diff.</p><div class="panels">' + panels + '</div><h2>Overlay</h2><div class="overlay">' + overlay + '</div></html>', encoding="utf-8")
    result["artifacts"].append(str(report))
    result["findings"].append(_finding("gerber_visual_review", "Rendered both layers at the same scale. Overlay colors indicate revision, not necessarily added or removed copper.", bounds_mm=bounds))


def _read_step(path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError("STEP reader rejected the file")
    if reader.TransferRoots() < 1:
        raise ValueError("STEP contains no transferable shape")
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError("STEP contains no geometry")
    return shape


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return None
    values = box.Get()
    return [round(float(v), 5) for v in values]


def _holes(shape):
    """Collect bounded full cylindrical interior faces as provisional holes.

    Axial intervals use a canonical axis direction and world origin, so two
    coaxial cavities at different depths remain distinguishable. This is a
    face-level heuristic: even reversed cylindrical faces are not proof of a
    design-intent hole, and split/open faces are reported as uncertain.
    """
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Cylinder
    holes, uncertain = [], []
    solid_explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    if not solid_explorer.More():
        return [], [_finding("hole_face_unsupported", "The STEP model has no solid; cylindrical face classification is unsupported.", "warning", status="unknown")]

    def solid_faces():
        while solid_explorer.More():
            faces = TopExp_Explorer(solid_explorer.Current(), TopAbs_FACE)
            while faces.More():
                yield TopoDS.Face_s(faces.Current())
                faces.Next()
            solid_explorer.Next()

    for face in solid_faces():
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() == GeomAbs_Cylinder and face.Orientation() == TopAbs_REVERSED:
            u_min, u_max, v_min, v_max = BRepTools.UVBounds_s(face)
            if (not all(math.isfinite(x) for x in (u_min, u_max, v_min, v_max))
                    or abs((u_max - u_min) - 2 * math.pi) > 1e-5 or v_max <= v_min):
                uncertain.append(_finding(
                    "hole_face_unsupported", "A cylindrical interior face is open, split, or unbounded; its hole geometry cannot be matched reliably.",
                    "warning", status="unknown", u_bounds=[x if math.isfinite(x) else None for x in (u_min, u_max)],
                    v_bounds=[x if math.isfinite(x) else None for x in (v_min, v_max)]))
                continue
            cylinder = surface.Cylinder()
            axis = cylinder.Axis()
            loc, direction = axis.Location(), axis.Direction()
            point = [float(loc.X()), float(loc.Y()), float(loc.Z())]
            original_vector = [float(direction.X()), float(direction.Y()), float(direction.Z())]
            radius = float(cylinder.Radius())
            if not all(math.isfinite(x) for x in (*point, *original_vector, radius)) or radius <= 0:
                uncertain.append(_finding("hole_face_unsupported", "A cylindrical face has invalid axis or radius; its geometry cannot be matched reliably.", "warning", status="unknown"))
                continue
            vector = original_vector[:]
            # Canonical point on the infinite axis, independent of where its
            # STEP surface origin happens to lie. Flip equivalent directions.
            if next((v for v in vector if abs(v) > 1e-8), 1) < 0:
                vector = [-v for v in vector]
            projection = sum(a * b for a, b in zip(point, vector))
            point = [round(a - projection * b, 5) for a, b in zip(point, vector)]
            direction_sign = sum(a * b for a, b in zip(vector, original_vector))
            axial = sorted([projection + direction_sign * v_min, projection + direction_sign * v_max])
            if not all(math.isfinite(x) for x in (*point, *axial, 2 * radius)):
                uncertain.append(_finding("hole_face_unsupported", "A cylindrical face has non-finite axis bounds; its geometry cannot be matched reliably.", "warning", status="unknown"))
                continue
            holes.append({"axis_point_mm": point, "axis_direction": [round(x, 6) for x in vector],
                          "diameter_mm": round(2 * radius, 5),
                          "axis_interval_mm": [round(x, 5) for x in axial]})
    return holes, uncertain


def _hole_findings(old, new):
    """Match unchanged extents first, then report only unique plausible pairs."""
    def compatible(a, b):
        return abs(a["diameter_mm"] - b["diameter_mm"]) <= COMPARISON_TOLERANCE_MM and abs(sum(x*y for x,y in zip(a["axis_direction"], b["axis_direction"]))) > 0.99999
    def same_axis(a, b):
        return compatible(a, b) and math.dist(a["axis_point_mm"], b["axis_point_mm"]) <= COMPARISON_TOLERANCE_MM
    def same_extent(a, b):
        return all(abs(x-y) <= COMPARISON_TOLERANCE_MM for x, y in zip(a["axis_interval_mm"], b["axis_interval_mm"]))

    # Removing exact matches first prevents unchanged repeated holes from
    # masking a separate removed or moved coaxial cavity.
    exact_old = [[j for j, b in enumerate(new) if same_axis(a, b) and same_extent(a, b)] for a in old]
    exact_new = [[i for i, a in enumerate(old) if same_axis(a, b) and same_extent(a, b)] for b in new]
    matched_old = {i for i, matches in enumerate(exact_old) if len(matches) == 1 and len(exact_new[matches[0]]) == 1}
    matched_new = {exact_old[i][0] for i in matched_old}
    findings = []
    # On the same axis, distinguish an extent change from a transverse move.
    # Even unique matches are heuristic, so report them as unknown.
    axial_old = [[j for j, b in enumerate(new) if j not in matched_new and same_axis(a, b)] for a in old]
    axial_new = [[i for i, a in enumerate(old) if i not in matched_old and same_axis(a, b)] for b in new]
    for i, a in enumerate(old):
        if i in matched_old or len(axial_old[i]) != 1:
            continue
        j = axial_old[i][0]
        if len(axial_new[j]) != 1:
            continue
        b = new[j]
        old_start, old_end = a["axis_interval_mm"]
        new_start, new_end = b["axis_interval_mm"]
        if min(old_end, new_end) + COMPARISON_TOLERANCE_MM < max(old_start, new_start):
            findings.append(_finding("hole_match_ambiguous", "Coaxial cylindrical faces occupy disjoint axial intervals; their relationship is unknown.", "warning", status="unknown", before=a, after=b))
        elif abs((old_end-old_start) - (new_end-new_start)) > COMPARISON_TOLERANCE_MM:
            findings.append(_finding("hole_depth_changed", "A uniquely matched cylindrical interior face has a changed axial extent; verify the cavity depth and design intent.", "warning", status="unknown", before=a, after=b, before_extent_mm=round(old_end-old_start, 5), after_extent_mm=round(new_end-new_start, 5)))
        else:
            findings.append(_finding("hole_axial_moved", "A uniquely matched cylindrical interior face appears to have moved along its axis; verify its identity.", "warning", status="unknown", before=a, after=b))
        matched_old.add(i)
        matched_new.add(j)
    candidates = [[j for j, b in enumerate(new) if j not in matched_new and compatible(a, b)] for a in old]
    reverse = [[i for i, a in enumerate(old) if i not in matched_old and compatible(a, b)] for b in new]
    for i, a in enumerate(old):
        if i in matched_old:
            continue
        if len(candidates[i]) == 1 and len(reverse[candidates[i][0]]) == 1:
            b = new[candidates[i][0]]
            distance = math.dist(a["axis_point_mm"], b["axis_point_mm"])
            if distance > COMPARISON_TOLERANCE_MM:
                findings.append(_finding("hole_moved", "A uniquely matched cylindrical interior face appears to have moved; verify its identity and highlighted geometry.", "warning", status="unknown", before=a, after=b, axis_shift_mm=round(distance, 5)))
        elif candidates[i]:
            findings.append(_finding("hole_match_ambiguous", "Multiple cylindrical faces could match; a moved hole cannot be identified reliably.", "warning", status="unknown", before=a, candidate_count=len(candidates[i])))
        else:
            findings.append(_finding("hole_unmatched_before", "A cylindrical interior face has no diameter-and-axis match in the new model.", "warning", status="unknown", before=a))
    for j, b in enumerate(new):
        if j not in matched_new and not reverse[j]:
            findings.append(_finding("hole_unmatched_after", "A cylindrical interior face has no diameter-and-axis match in the old model.", "warning", status="unknown", after=b))
    return findings


def _mesh(shape):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5, True).Perform()
    triangles = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh:
            transform = location.Transformation()
            for i in range(1, mesh.NbTriangles() + 1):
                indices = mesh.Triangle(i).Get()
                coords = []
                for index in indices:
                    p = mesh.Node(index).Transformed(transform)
                    coords.extend([round(p.X(), 4), round(p.Y(), 4), round(p.Z(), 4)])
                triangles.append(coords)
                if len(triangles) > 300000:
                    raise ValueError("STEP mesh exceeds the 300,000 triangle viewer limit")
        explorer.Next()
    return triangles


def _compare_step(base, head, output_dir, result):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    old, new = (_read_step(p) if p else None for p in (base, head))
    old_box, new_box = (_bbox(s) if s else None for s in (old, new))
    result["findings"].append(_finding("step_bounds", "Model bounding boxes in millimeters; compare them to investigate dimensional changes.", before_mm=old_box, after_mm=new_box))
    result["findings"].append(_finding("step_tolerances_unknown", "Authored dimensional tolerances and GD&T were not interpreted; absent or unsupported metadata must not be treated as unchanged.", "warning", status="unknown", comparison_tolerance_mm=COMPARISON_TOLERANCE_MM))
    result["findings"].append(_finding("hole_recognition_scope", "Hole recognition uses full reversed cylindrical faces within solids as a heuristic; face orientation alone does not prove hole intent. Split faces, open shells and repeated geometry may be ambiguous. Axial extents are face bounds, not verified hole depths.", "warning", status="unknown"))
    old_holes, old_uncertain = _holes(old) if old else ([], [])
    new_holes, new_uncertain = _holes(new) if new else ([], [])
    result["findings"].extend(old_uncertain + new_uncertain)
    result["findings"].extend(_hole_findings(old_holes, new_holes))
    def difference(a, b):
        if a is None:
            return None
        if b is None:
            return a
        operation = BRepAlgoAPI_Cut(a, b)
        operation.Build()
        if not operation.IsDone():
            raise ValueError("Open CASCADE Boolean subtraction failed")
        return operation.Shape()
    removed, added = difference(old, new), difference(new, old)
    meshes = {"before": _mesh(old) if old else [], "after": _mesh(new) if new else [], "removed": _mesh(removed) if removed else [], "added": _mesh(added) if added else []}
    if (old and not meshes["before"]) or (new and not meshes["after"]):
        raise ValueError("STEP geometry could not be tessellated for the viewer")
    report = output_dir / "step-view.html"
    _step_viewer(report, meshes)
    result["artifacts"].append(str(report))
    result["findings"].append(_finding("step_visual_review", "Interactive view shows removed solid geometry in red and added solid geometry in green. Open-shell/surface-only geometry may not yield meaningful Boolean results.", mesh_triangle_counts={k: len(v) for k,v in meshes.items()}))


def _step_viewer(path, meshes):
    # All embedded values are numeric arrays generated by Open CASCADE.
    data = json.dumps(meshes, separators=(",", ":"), allow_nan=False)
    template = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>STEP comparison</title><style>body{font:16px system-ui;background:#f4f5f7;color:#17202a;margin:1.5rem}canvas{background:white;width:100%;height:70vh;touch-action:none;border:1px solid #bbc}label{margin-right:1rem}</style><h1>STEP comparison</h1><p>Drag to rotate; scroll to zoom. Gray: original model; red: removed; green: added. Inspect uncertainties in the report.</p><div><label><input type="checkbox" id="before" checked>Before</label><label><input type="checkbox" id="after">After</label><label><input type="checkbox" id="removed" checked>Removed</label><label><input type="checkbox" id="added" checked>Added</label></div><canvas id="view" width="1400" height="900"></canvas><script>const data=DATA;const canvas=document.getElementById('view'),ctx=canvas.getContext('2d');let yaw=.6,pitch=-.4,scale=1,drag=null;const all=[...data.before,...data.after];let min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const t of all)for(let i=0;i<9;i+=3)for(let k=0;k<3;k++){min[k]=Math.min(min[k],t[i+k]);max[k]=Math.max(max[k],t[i+k])}const center=min.map((v,i)=>(v+max[i])/2),extent=Math.max(...max.map((v,i)=>v-min[i]),1);function project(x,y,z){x-=center[0];y-=center[1];z-=center[2];let x1=x*Math.cos(yaw)-y*Math.sin(yaw),y1=x*Math.sin(yaw)+y*Math.cos(yaw);return [canvas.width/2+x1*scale*canvas.height/extent*.7,canvas.height/2-(z*Math.cos(pitch)-y1*Math.sin(pitch))*scale*canvas.height/extent*.7,z*Math.sin(pitch)+y1*Math.cos(pitch)]}function render(){ctx.clearRect(0,0,canvas.width,canvas.height);let faces=[];for(const [key,color] of [['before','#9aa4b2'],['after','#657586'],['removed','#df3349'],['added','#18a057']])if(document.getElementById(key).checked)for(const t of data[key]){let p=[project(...t.slice(0,3)),project(...t.slice(3,6)),project(...t.slice(6,9))];faces.push({p,z:(p[0][2]+p[1][2]+p[2][2])/3,color})}faces.sort((a,b)=>a.z-b.z);for(const f of faces){ctx.beginPath();ctx.moveTo(f.p[0][0],f.p[0][1]);ctx.lineTo(f.p[1][0],f.p[1][1]);ctx.lineTo(f.p[2][0],f.p[2][1]);ctx.closePath();ctx.fillStyle=f.color;ctx.globalAlpha=.78;ctx.fill()}ctx.globalAlpha=1}document.querySelectorAll('input').forEach(x=>x.onchange=render);canvas.onpointerdown=e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)};canvas.onpointerup=()=>drag=null;canvas.onpointermove=e=>{if(drag){yaw+=(e.clientX-drag[0])*.01;pitch+=(e.clientY-drag[1])*.01;drag=[e.clientX,e.clientY];render()}};canvas.onwheel=e=>{e.preventDefault();scale=Math.min(30,Math.max(.1,scale*Math.exp(-e.deltaY*.001)));render()};render()</script></html>'''
    path.write_text(template.replace("DATA", data), encoding="utf-8")
