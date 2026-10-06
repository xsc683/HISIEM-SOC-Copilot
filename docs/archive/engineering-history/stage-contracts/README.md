# HISIEM SOC Copilot — Four-Plane Spec Pack

Files:

1. [`00_Four-Plane_Architecture_Contract_Freeze.md`](00_Four-Plane_Architecture_Contract_Freeze.md) — frozen architecture/contract authority.
2. [`01_Stage-A_Knowledge_Closure_Validation_Implementation_Spec.md`](01_Stage-A_Knowledge_Closure_Validation_Implementation_Spec.md) — validate/harden completed Knowledge Closure; no redevelopment.
3. [`02_Stage-B_Observability_Foundation_Implementation_Spec.md`](02_Stage-B_Observability_Foundation_Implementation_Spec.md) — OTel/context/metrics/log-correlation/Collector baseline.
4. [`03_Stage-C_Capability_MCP_Implementation_Spec.md`](03_Stage-C_Capability_MCP_Implementation_Spec.md) — ToolProvider + governed read-only MCP V1.
5. [`04_Stage-D_Analyst_Experience_Implementation_Spec.md`](04_Stage-D_Analyst_Experience_Implementation_Spec.md) — Investigation Workspace productization without precommitting to DAG/3-column/full-SIEM redesign.
6. [`05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md`](05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md) — integrated security/authority/reliability/evaluation acceptance.
7. [`06_Stage-E_Detailed_Design.md`](06_Stage-E_Detailed_Design.md) — the Stage E detailed design — cited as an authority by the E2 and E4 reports.

Frozen implementation sequence:

```text
Stage A  Knowledge Closure Validation
   ↓
Stage B  Observability Foundation
   ↓
Stage C  Capability / MCP
   ├──────────────┐
   ↓              ↓
Stage D  Analyst Experience Productization
   \              /
    └──────┬─────┘
           ↓
Stage E  Cross-Plane Integration / Evaluation
```

Stage C and D may overlap only after the relevant frozen contracts are stable and shared backend/frontend boundaries have single ownership.

> **The list above was incomplete as inherited.** This README originally listed only six
> files: it omitted `06_Stage-E_Detailed_Design.md` and the eight `execution-prompts/` files
> it shipped alongside. `06` is now listed above. The eight prompts were deleted in the
> 2026-10-06 documentation-architecture pass — see the note at the end of this file.

---

## Where this lives, and why

These files were originally kept **outside both repositories** — the specs and the frozen
contract at `D:\Project\four-plane\`, the E2E run kit at `D:\Project\TEST\`. That made the
engineering record **unverifiable**: [`../stage-reports/`](../stage-reports/index.md) cites
these documents as its **authorities**, and
`SIEM/docs/design/copilot-workspace-ux-brief.md` cited `four-plane/…` as its `Authority:` —
yet anyone cloning this repository could not open a single one of them.

They now live here, so the record's **inputs and outputs sit in the same repository**:

```text
docs/archive/engineering-history/stage-contracts/   ← 输入：specified what to build
├── 00_Four-Plane_Architecture_Contract_Freeze.md    (FROZEN BASELINE v1.0, 2026-09-14)
├── 01 … 05   Stage A–E implementation specs
└── 06_Stage-E_Detailed_Design.md

docs/archive/engineering-history/stage-reports/      ← 输出：what was built, run and passed
```

**Move history** — 2026-09-23, per documentation-governance decision. File contents are
**byte-identical** to the originals (verified by hash); only this README and the two path
references (`stage-reports/STAGE_E_E0`, the SIEM UX brief) were updated.

**The eight execution prompts were deleted** — 2026-10-06, documentation-architecture pass.
They were one-time execution instructions to a coding agent, not specifications or design
records: their outcome is what `../stage-reports/` measures and records. They also still
pointed at `D:\Project\four-plane\`, a directory that stopped existing when the frozen
inputs were moved into this repository on 2026-09-23. Keeping them would have preserved a
set of instructions whose paths no longer resolve and whose work is already accounted for.
The reports they produced remain the traceable record.

## Status: frozen engineering history, not the product model

These are inputs to the engineering process, and they carry the **same rule**
[`../stage-reports/index.md`](../stage-reports/index.md) states for the reports:
**stage terminology is internal engineering history, not the product model.**

In particular `00_Four-Plane_Architecture_Contract_Freeze.md` is a **v1.0 baseline dated
2026-09-14**. Where it disagrees with the code, **the code wins**. The machine-readable
plane taxonomy the acceptance gates actually use is the `Plane` enum in
`evaluation/cross_plane/contracts.py`.

## One known discrepancy, recorded not corrected

`stage-reports/STAGE_E_E3_AUTHORITY_RELIABILITY_REPORT.md` lists
`06_Stage-E_XP-01_Cross-Plane_Evaluation_Pack.md` among its authorities. **No such file
exists** — the file here is `06_Stage-E_Detailed_Design.md`. Left as-is because that report
is evidence; noted here so a reader looking for it knows it is a naming slip rather than a
missing document.

