# PCBGit

PCBGit reviews hardware changes in Git: compare KiCad schematics and PCB
layouts, inspect STEP and Gerber changes, run selected manufacturing checks,
and check distributor availability. It produces a portable HTML report and
machine-readable JSON for local use or CI.

These checks assist reviewers; they do not replace a fabricator's review or
guarantee a design is manufacturable.

## Installation

Install Python 3.12, Git, and KiCad 9 with `kicad-cli` on `PATH`. From a trusted
checkout of PCBGit, install the CLI and the optional Open CASCADE and Gerber
dependencies:

```sh
python -m pip install '.[geometry]'
```

The base package supports Python 3.10+, but geometry dependencies may require
Python 3.12. If they are absent, changed STEP and Gerber comparisons are
reported as unknown. The included Dockerfile builds from the official KiCad
9.0.9 image with the geometry dependencies installed:

```sh
docker build -t pcbgit .
```

## Review a change

Run from the Git repository being reviewed. Both revisions must be present in
the local repository; fetch full history in CI:

```sh
pcbgit review --base origin/main --head HEAD --output pcbgit-report
```

Use `--base :empty:` to review every tracked file as newly added, including
on the first commit of a repository.

The tool compares the merge base of `--base` and `--head` with `--head` and
checks all KiCad files at the head revision, including unchanged ones. It reads
committed Git objects, not uncommitted working-tree changes. Open
`pcbgit-report/index.html` and its linked comparison viewers; `report.json`
contains the machine-readable findings and exact commits. Keep the full report
directory together when sharing it.

Exit codes are `0` for pass, `1` for a failed rule or inventory gate, and `2`
for an unknown required operation or an execution error. A failed gate takes
precedence over an unknown result. Partially covered DFM checks are identified
in the report; their absence of findings is not a complete DFM pass.

Additional options:

```sh
pcbgit review --base origin/main --head HEAD \
  --bom manufacturing/bom.csv --build-quantity 10 \
  --profile manufacturing/dfm.json --output pcbgit-report
```

`--repo` selects another Git repository. `--bom` and `--profile` are relative
to its root and are read from the head commit. `--build-quantity` defaults to
one board. `--skip-inventory` marks the stock gate skipped and is intended for
untrusted fork reviews where secrets are unavailable, not as a stock check.

Snapshots reject symlinks, submodules and unsafe paths. A single tracked file
is limited to 64 MiB and a snapshot to 256 MiB and 100,000 files. Git LFS
pointers are not materialized. Run tools and view reports from untrusted CAD
files in an isolated environment.

## Review coverage

- **KiCad:** KiCad 9 renders changed `.kicad_pcb` and `.kicad_sch` files to
  SVG for side-by-side review. It runs the project's DRC and ERC on head files.
- **STEP:** `.step` and `.stp` solid models get an interactive offline 3D
  viewer, with removed geometry in red and added geometry in green. The report
  includes bounding boxes and a conservative comparison of cylindrical
  interior faces, with evidence and ambiguity warnings. Repeated, split, blind,
  and other complex features may not be matched. Open-shell geometry may not
  produce meaningful Boolean differences. Authored dimensional tolerances and
  GD&T are **always unknown**: they are not interpreted. The 0.01 mm feature
  matching tolerance is numerical and is not a design tolerance. Any changed
  STEP file therefore makes the overall review unknown (exit code 2), unless a
  failed check takes precedence; inspect the generated viewer and findings.
- **Gerber:** Recognized Gerber extensions are shown as aligned before/after
  views and an overlay. Overlay colors denote revisions, not calculated added
  and removed copper. Check the report for unsupported or unreadable files.
- **DFM:** The bundled [JLCPCB standard two-layer profile](pcbgit/profiles/jlcpcb-standard-2layer.json)
  uses published limits for two-layer FR-4 with 1 oz outer copper, based on
  [JLCPCB's rigid PCB capabilities](https://jlcpcb.com/capabilities/Capabilities?type=1).
  It checks track widths, nominal drilled hole and slot dimensions, and
  straight-track clearance between distinct known nets on the same layer.
  Clearance for pads, vias, arcs, zones, same-net features and unknown nets,
  finished dimensions, stack-up assumptions, and many other manufacturing
  rules are not checked. Review the profile and manufacturer capabilities
  before ordering. To customize it, copy the complete JSON profile into the
  reviewed repository, change the limits, commit it, and use `--profile`.

## Distributor stock

Each populated BOM row needs an exact DigiKey or Mouser SKU with sufficient
stock for its quantity multiplied by the build quantity. The same SKU across
rows is counted together. Missing SKUs, unavailable credentials, ambiguous
matches, rate limits and API errors are unknown and fail the trusted-run stock
gate unless the other distributor verifies enough stock. Confirmed shortages
also fail. This is a point-in-time result, not a reservation or a guarantee
that stock will remain available.

When the head revision contains exactly one root schematic, PCBGit asks
KiCad to export its hierarchical BOM automatically using `DigiKey` and `Mouser`
symbol fields, excluding DNP components. With zero or multiple root schematics,
commit a KiCad-exported CSV and select it with `--bom`; use this option as
well if your project needs a particular BOM export. The accepted CSV has a
nonempty reference column, positive integer `Qty` or `Quantity`, and distributor SKU
columns, for example:

```csv
Reference,Qty,DigiKey,Mouser,DNP
R1 R2,2,EXAMPLE-DK-SKU,,
U1,1,,EXAMPLE-MOUSER-SKU,
```

The SKU values shown are placeholders. `DNP` values such as `yes`, `true`,
and `1`, and `In BOM` values such as `no`, `false`, and `0`, exclude a row.

Use environment variables or CI secrets for distributor credentials:

- DigiKey: `DIGIKEY_CLIENT_ID` and either `DIGIKEY_CLIENT_SECRET` or
  `DIGIKEY_ACCESS_TOKEN`; configure `DIGIKEY_ACCOUNT_ID` where required by
  DigiKey, including two-legged OAuth.
- Mouser: `MOUSER_API_KEY` for its search API.

Only the distributor APIs receive the corresponding credentials. Distributor
credentials are removed from the environment passed to KiCad subprocesses.
Use protected secrets and trusted runners; treat all CAD parsing as untrusted
input processing.

## GitHub Actions

Use a trusted, externally referenced PCBGit revision and a runner with Docker.
Replace `FULL_PCBGIT_COMMIT_SHA` below with the full commit SHA of a reviewed
PCBGit revision that contains `action.yml`. Store the distributor values as
repository secrets. The following workflow checks PR heads and uploads the
report even if the check fails:

```yaml
name: Hardware review
on: [pull_request]
permissions:
  contents: read
jobs:
  pcbgit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: lubacareer/PCBGit@FULL_PCBGIT_COMMIT_SHA
        with:
          base: ${{ github.event.pull_request.base.sha }}
          head: ${{ github.event.pull_request.head.sha }}
          digikey-client-id: ${{ secrets.DIGIKEY_CLIENT_ID }}
          digikey-client-secret: ${{ secrets.DIGIKEY_CLIENT_SECRET }}
          digikey-account-id: ${{ secrets.DIGIKEY_ACCOUNT_ID }}
          mouser-api-key: ${{ secrets.MOUSER_API_KEY }}
          # bom: manufacturing/bom.csv
          # build-quantity: '10'
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        if: always()
        with:
          name: pcbgit-report
          path: pcbgit-report/
```

The action refuses `pull_request_target` and skips inventory on fork PRs. Keep
fork workflows unprivileged; do not pass secrets to them or run local action
code from the PR checkout. The stock check must be run separately in a trusted,
credentialed context. Pin and periodically review all third-party actions.

To avoid rebuilding the image on every GitHub-hosted run, build and publish a
trusted PCBGit image and set the action's optional `image` input to its immutable
`registry/name@sha256:<digest>` reference. Without it the action builds locally.

## GitLab CI

Build and publish the Docker image from a trusted PCBGit revision to a registry
your runners can access, then set `PCBGIT_IMAGE` to an immutable image digest.
Include `.gitlab/pcbgit.yml` from a reviewed immutable revision, for example:

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/lubacareer/PCBGit/FULL_PCBGIT_COMMIT_SHA/.gitlab/pcbgit.yml'
variables:
  PCBGIT_IMAGE: 'registry.example.com/team/pcbgit@sha256:FULL_IMAGE_DIGEST'
  # PCBGIT_BOM: 'manufacturing/bom.csv'
  # PCBGIT_BUILD_QUANTITY: '10'
  # PCBGIT_PROFILE: 'manufacturing/dfm.json'
```

Replace both placeholders before use. Configure distributor variables as
protected GitLab CI/CD variables and allow them only in trusted protected-ref
pipelines. Never expose protected variables to untrusted merge-request code or
fork pipelines: a job script cannot enforce that boundary after variables have
already been injected. The template skips inventory on fork merge requests;
perform stock verification separately in a trusted pipeline with credentials.
It saves `pcbgit-report/` as an artifact even when the job fails.
Push pipelines compare against `CI_COMMIT_BEFORE_SHA`, including all commits in
the push. A new non-default branch fetches and compares against the default
branch. When a default-branch pipeline has no prior commit, it uses an empty
baseline. Missing prior commits or non-fast-forward pushes fail explicitly
instead of silently reviewing an incomplete range.

## Development

```sh
python -m pip install '.[geometry,test]'
python -m pytest -q
```

Licensed under the [Apache License 2.0](LICENSE).
