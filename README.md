# PCBGit

PCBGit reviews hardware changes in Git. This first phase compares KiCad 9 PCB
and schematic snapshots, renders SVGs, and runs the project's DRC and ERC rules.
The checks do not replace fabrication review or guarantee manufacturability.

Changed STEP and Gerber files also get standalone comparison viewers. Install
the optional geometry dependencies on Python 3.12 or newer with
`python -m pip install '.[geometry]'`. STEP solid differences are shown in red
and green; cylindrical-hole matching is limited and reports ambiguity. Authored
tolerances and GD&T are currently reported as unknown, so a STEP comparison
returns an unknown verdict (exit 2) until they can be interpreted. Gerber overlays show
revisions, not a calculated Boolean difference.

DFM checks use the versioned JLCPCB standard two-layer FR-4 profile in
`pcbgit/profiles/`. They cover track widths, nominal drilled holes and slots,
and straight-track clearance on the same copper layer for distinct known nets.
They do not cover all copper objects, stack-ups, finished tolerances, or all
fabrication rules. Pass `--profile path/to/profile.json` to use a profile from
the reviewed head commit. Review its assumptions against the fabricator's
current capabilities before ordering.

Install Python 3.10 or newer, Git, and KiCad 9 with `kicad-cli` on `PATH`:

```sh
python -m pip install .
pcbgit review --base origin/main --head HEAD --output pcbgit-report
```

PCBGit compares the merge base of the two revisions with the head revision.
Open `pcbgit-report/index.html` for the visual report or read `report.json` for
machine-readable results. The exit code is 0 for a pass, 1 for rule violations,
and 2 when a required operation cannot be verified. Unchanged KiCad files are
also checked, so changing a configuration file does not bypass checks. ERC runs
on root schematics; missing, cyclic or unsupported sheet references are reported
as unknown rather than silently skipped. Git tree symlinks and submodules are rejected rather than followed. Reports and
CAD files from untrusted sources should be viewed in an isolated environment.

Install test dependencies and run the full suite with
`python -m pip install '.[geometry,test]'` and `python -m pytest -q` (Python
3.12 or newer is required for geometry dependencies). Licensed under the
Apache License 2.0; see [LICENSE](LICENSE).
