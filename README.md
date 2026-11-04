# PCBGit

PCBGit reviews hardware changes in Git. This first phase compares KiCad 9 PCB
and schematic snapshots, renders SVGs, and runs the project's DRC and ERC rules.
The checks do not replace fabrication review or guarantee manufacturability.

Install Python 3.10 or newer, Git, and KiCad 9 with `kicad-cli` on `PATH`:

```sh
python -m pip install .
pcbgit review --base origin/main --head HEAD --output pcbgit-report
```

PCBGit compares the merge base of the two revisions with the head revision.
Open `pcbgit-report/index.html` for the visual report or read `report.json` for
machine-readable results. The exit code is 0 for a pass, 1 for rule violations,
and 2 when a required operation cannot be verified. Unchanged KiCad files are
also checked, so changing a configuration file does not bypass checks. Git
archive symlinks and submodules are rejected rather than followed. Reports and
CAD files from untrusted sources should be viewed in an isolated environment.

Run the tests with `python -m unittest discover -s tests`. Licensed under the
Apache License 2.0; see [LICENSE](LICENSE).
