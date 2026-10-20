"""Canonical Markdown renderer for the static Deep Review report.

Stdlib-only. Consumes the context dict produced by `report_data.build_context`.
All untrusted, free-text fields (datasheet quotes, summaries, descriptions,
comments, quarantine reasons, etc.) are escaped before being embedded, since
this Markdown may later be rendered as HTML by a viewer (GitHub, an editor
preview, `action/format-report.py`, ...). Escaping never happens for values
the renderer itself controls (headers, table scaffolding, labels).
"""
from __future__ import annotations

from html import escape as _html_escape

STATE_DESCRIPTIONS = {
    "verified_full": "Evidence gate checked this finding's citations and datasheet quote in full.",
    "verified_partial": "Evidence gate checked citations, but the datasheet quote check was partial "
                         "(pdftotext unavailable or PDF missing) — treat as not fully verified.",
    "not_checked": "No evidence gate has run against this item yet.",
    "quarantined": "Failed or did not satisfy the evidence gate — visible here, never silently dropped.",
    "verifier_refuted": "A verifier explicitly marked the claim refuted; it remains visible with provenance.",
    "gate_quarantined": "The evidence gate quarantined the claim; no verifier refutation is implied.",
    "verified_ok": "A reviewer confirmed this finding (review_annotations.json status=confirmed).",
    "refuted": "A reviewer judged this finding not applicable (status=suppressed) — it still shows "
               "here, it is not hidden.",
    "escalated": "A reviewer escalated this finding (status=escalated).",
}


def esc(value) -> str:
    """Escape a single untrusted scalar for safe embedding in Markdown/HTML."""
    if value is None:
        return ""
    return _html_escape(str(value), quote=False)


def _fmt_list_str(items) -> str:
    return ", ".join(esc(i) for i in items) if items else "—"


def _finding_evidence_lines(finding: dict) -> list[str]:
    lines = []
    ev = finding.get("evidence") or {}
    anchors = []
    for key in ("components", "nets", "pins"):
        vals = ev.get(key) or finding.get(key)
        if vals:
            anchors.append(f"{key}: {_fmt_list_str(vals)}")
    if anchors:
        lines.append("  - Design anchors: " + "; ".join(anchors))
    for ds in ev.get("datasheet") or []:
        if not isinstance(ds, dict):
            continue
        page = ds.get("page")
        page_s = f", p.{esc(page)}" if page is not None else ""
        lines.append(f'  - Datasheet ({esc(ds.get("mpn"))}{page_s}): "{esc(ds.get("quote"))}"')
    comp = ev.get("computation")
    if isinstance(comp, dict):
        lines.append(f"  - Computation: {esc(comp.get('description'))} -> {esc(comp.get('result'))}"
                      + (f" (`{esc(comp.get('script'))}`)" if comp.get("script") else ""))
    return lines


def _render_deep_review(ctx: dict) -> list[str]:
    dr = ctx["deep_review"]
    out = ["## Deep Review", ""]
    if not dr["present"]:
        out.append("Deep Review pass not run — `analysis/deep_review.json` does not exist. "
                    "Per-IC usage-vs-datasheet findings are an explicit gap, not an absence of issues.")
        out.append("")
        return out
    meta = dr["meta"]
    out.append(f"Produced for run `{esc(meta.get('produced_for_run_id'))}` "
                f"at {esc(meta.get('produced_at'))} "
                f"(schema {esc(meta.get('schema_version'))}).")
    out.append("")
    if dr["schema_errors"]:
        out.append("**Schema violations found while loading `deep_review.json`** "
                    "(document rendered best-effort below):")
        for e in dr["schema_errors"]:
            out.append(f"- {esc(e)}")
        out.append("")

    findings = sorted(dr["findings"], key=lambda f: (str(f.get("category", "")), str(f.get("finding_id", ""))))
    if not findings:
        out.append("No verified findings.")
        out.append("")
    else:
        by_cat: dict[str, list[dict]] = {}
        for f in findings:
            by_cat.setdefault(str(f.get("category", "uncategorized")), []).append(f)
        for cat in sorted(by_cat):
            out.append(f"### {esc(cat)}")
            out.append("")
            for f in by_cat[cat]:
                sev = esc(f.get("severity", "?")).upper()
                conf = esc(f.get("confidence", "?"))
                state = f.get("_state", "not_checked")
                out.append(f"- **[{sev}]** {esc(f.get('summary'))} "
                            f"(confidence: {conf}; finding_id: `{esc(f.get('finding_id'))}`; "
                            f"verification: `{state}`)")
                if f.get("description"):
                    out.append(f"  - {esc(f.get('description'))}")
                if f.get("recommendation"):
                    out.append(f"  - Recommendation: {esc(f.get('recommendation'))}")
                out.extend(_finding_evidence_lines(f))
            out.append("")

    quarantined = sorted(dr["quarantined"], key=lambda q: (str(q.get("category", "")), str(q.get("summary", ""))))
    out.append("### Unverified claims (quarantined)")
    out.append("")
    refuted = [q for q in quarantined if q.get("_disposition") == "verifier_refuted"]
    gate_quarantined = [q for q in quarantined if q.get("_disposition") != "verifier_refuted"]
    out.append("These claims remain visible. A verifier refutation is distinguished by the recorded "
                "`verifier refuted:` provenance prefix; reviewer suppression is not verifier refutation.")
    out.append("")
    out.append("#### Verifier-refuted claims")
    if not refuted:
        out.append("None recorded as verifier-refuted.")
    else:
        for q in refuted:
            out.append(f"- {esc(q.get('summary', '(no summary)'))} — {esc(q.get('quarantine_reason'))}")
    out.append("")
    out.append("#### Evidence-gate quarantines")
    if not gate_quarantined:
        out.append("None quarantined by the evidence gate.")
    else:
        for q in gate_quarantined:
            out.append(f"- {esc(q.get('summary', '(no summary)'))} "
                        f"— **quarantine_reason:** {esc(q.get('quarantine_reason'))}")
    out.append("")
    return out


def _render_item_list(items: list[dict], text_key: str) -> list[str]:
    """Render a synthesis.json item list (`top_risks`/`verified_ok`/`not_checked`
    per `schemas/synthesis.schema.json`): each item is an object with an
    optional `finding_id` and a required text field (`reason` or `note`)."""
    out = []
    for item in items:
        fid = item.get("finding_id")
        text = item.get(text_key) or item.get("reason") or item.get("note") or ""
        prefix = f"`{esc(fid)}`: " if fid else ""
        out.append(f"- {prefix}{esc(text)}")
    return out


def _render_synthesis(ctx: dict) -> list[str]:
    syn = ctx["synthesis"]
    out = ["## Verdict & Synthesis", ""]
    if not syn.get("present"):
        out.append("`synthesis.json` not found — no cross-finding synthesis available. "
                    "The sections below reflect only the Deep Review evidence gate output.")
        out.append("")
        return out
    if syn.get("schema_errors"):
        out.append("**Schema violations found while loading `synthesis.json`** "
                    "(document rendered best-effort below):")
        for e in syn["schema_errors"]:
            out.append(f"- {esc(e)}")
        out.append("")
    out.append(f"**Verdict status:** `{esc(syn.get('verdict_status')) or '(not stated in synthesis.json)'}`")
    out.append("")
    out.append(f"**Verdict summary:** {esc(syn.get('verdict_summary')) or '(not stated in synthesis.json)'}")
    out.append("")
    out.append("### Top Risks")
    if syn.get("top_risks"):
        out.extend(_render_item_list(syn["top_risks"], "reason"))
    else:
        out.append("None listed.")
    out.append("")
    out.append("### Verified OK")
    if syn.get("verified_ok"):
        out.extend(_render_item_list(syn["verified_ok"], "note"))
    else:
        out.append("None listed.")
    out.append("")
    out.append("### Not Checked (explicit gaps)")
    if syn.get("not_checked"):
        out.append("Explicit coverage gaps the synthesis author recorded — never a silent omission:")
        out.append("")
        out.extend(_render_item_list(syn["not_checked"], "reason"))
    else:
        out.append("None listed.")
    out.append("")
    if syn.get("contradictions"):
        out.append("### Contradictions")
        for c in syn["contradictions"]:
            fids = ", ".join(f"`{esc(f)}`" for f in (c.get("finding_ids") or []))
            out.append(f"- {fids}: {esc(c.get('description'))}")
        out.append("")
    if syn.get("extra"):
        out.append("### Additional synthesis data (unrecognized keys, shown raw)")
        for k, v in syn["extra"].items():
            out.append(f"- `{esc(k)}`: {esc(v)}")
        out.append("")
    return out


def _render_design_context(ctx: dict) -> list[str]:
    dc = ctx["design_context"]
    out = ["## Design Context", ""]
    if not dc["present"]:
        out.append("`design_context.json` not found — optional Deep Review input not available.")
        out.append("")
        return out
    data = dc["data"]
    for key in ("design_category", "environment"):
        v = data.get(key)
        if isinstance(v, dict):
            out.append(f"- **{key}:** effective=`{esc(v.get('effective'))}` "
                        f"(inferred=`{esc(v.get('inferred'))}`, declared=`{esc(v.get('declared'))}`)")
        else:
            out.append(f"- **{key}:** {esc(v)}")
    out.append(f"- **compliance_targets:** {_fmt_list_str(data.get('compliance_targets') or [])}")
    out.append(f"- **user_declared_intent:** {esc(data.get('user_declared_intent'))}")
    out.append(f"- **confidence:** {esc(data.get('confidence'))}")
    out.append(f"- **resolution:** {esc(data.get('resolution'))}")
    if data.get("evidence"):
        out.append(f"- **evidence:** {esc(data.get('evidence'))}")
    out.append("")
    return out


def _render_review_plan(ctx: dict) -> list[str]:
    rp = ctx["review_plan"]
    out = ["## Review Plan", ""]
    if not rp["present"]:
        out.append("`review_plan.json` not found — no dispatch plan recorded for this run.")
        out.append("")
        return out
    out.append(f"Plan `{esc(rp.get('plan_id'))}` ({esc(rp.get('purpose'))}), "
                f"created {esc(rp.get('created_at'))}.")
    out.append("")
    if not rp["tasks"]:
        out.append("No tasks in plan.")
        out.append("")
        return out
    out.append("| Task | Type | Tier | Result Path | Result Present |")
    out.append("|---|---|---|---|---|")
    for t in rp["tasks"]:
        present = "yes" if t.get("_result_exists") else "no"
        out.append(f"| {esc(t.get('task_id'))} | {esc(t.get('task_type'))} | {esc(t.get('tier'))} "
                    f"| `{esc(t.get('result_path'))}` | {present} |")
    out.append("")
    return out


def _render_triage(ctx: dict) -> list[str]:
    ra = ctx["review_annotations"]
    mr = ctx["merge_report"]
    out = ["## Reviewer Triage (Layer 2 — review_annotations.json)", ""]
    if not ra["present"] and not mr["present"]:
        out.append("No `review_annotations.json` or merge report found — no Layer 2 detector-finding "
                    "triage has been recorded for this run.")
        out.append("")
        return out
    if mr["present"]:
        d = mr["data"]
        out.append(f"Merge report: {esc(d.get('applied_count'))} applied, "
                    f"{esc(d.get('suppressed_count'))} suppressed, "
                    f"{len(d.get('orphan_annotations') or [])} orphan annotation(s), "
                    f"{len(d.get('invariant_violations') or [])} invariant violation(s).")
        out.append("")
    if ra["schema_errors"]:
        out.append("**Schema violations in `review_annotations.json`:**")
        for e in ra["schema_errors"]:
            out.append(f"- {esc(e)}")
        out.append("")
    if ra["annotations"]:
        out.append("| Finding ID | Status | Verification | Confidence | Reason |")
        out.append("|---|---|---|---|---|")
        for a in ra["annotations"]:
            out.append(f"| `{esc(a.get('finding_id'))}` | {esc(a.get('status'))} "
                        f"| `{a.get('_state')}` | {esc(a.get('confidence'))} "
                        f"| {esc(a.get('reason'))} |")
        out.append("")
    else:
        out.append("No individual annotations recorded.")
        out.append("")
    return out


def _render_analyzer_triage(ctx: dict) -> list[str]:
    at = ctx["analyzer_triage"]
    out = ["## Analyzer Finding Triage (Layer 1 — analyzer_triage.py)", ""]
    if not at["present"]:
        out.append("`triage.json` not found — `analyzer_triage.py` has not been run against this "
                    "run's analyzer findings. Raw analyzer output (including likely false positives) "
                    "has not been separated from confirmed issues.")
        out.append("")
        return out
    out.append(f"{esc(at.get('finding_count'))} finding(s) triaged; "
                f"{esc(at.get('must_fix_count'))} remain must-fix (error-severity, not downgraded "
                "to `likely_fp`).")
    out.append("")
    table = at.get("triage_table") or []
    if table:
        out.append("| Rule | Count | Real | Likely FP | Review | Sample Reason |")
        out.append("|---|---|---|---|---|---|")
        for row in table:
            out.append(f"| `{esc(row.get('rule_id'))}` | {esc(row.get('count'))} "
                        f"| {esc(row.get('real'))} | {esc(row.get('likely_fp'))} "
                        f"| {esc(row.get('review'))} | {esc(row.get('reason'))} |")
        out.append("")
    else:
        out.append("No per-rule triage rollup recorded.")
        out.append("")
    return out


def _render_board_facts(ctx: dict) -> list[str]:
    bf = ctx["board_facts"]
    out = ["## Board Facts (Raw PCB Parse — board_facts.py)", ""]
    if not bf["present"]:
        out.append("`board_facts.json` not found — `board_facts.py` has not been run for this run "
                    "(PCB-only tool; also skipped on schematic-only reviews). Multi-ply stackup "
                    "correction and graphic-copper net detection are not available.")
        out.append("")
        return out
    out.append(f"- **Multi-ply stackup detected:** {esc(bf.get('stackup_multi_ply'))}")
    out.append(f"- **Impedance control noted in fab notes:** {esc(bf.get('impedance_control_noted'))}")
    out.append(f"- **Nets with graphic copper (gr_*) the analyzers do not count:** "
                f"{esc(bf.get('graphic_copper_net_count'))}")
    out.append(f"- **Unnetted graphic copper shapes:** {esc(bf.get('unnetted_copper_count'))}")
    out.append("")
    warnings = bf.get("stackup_warnings") or []
    if warnings:
        out.append("Stackup warnings:")
        for w in warnings:
            out.append(f"- {esc(w)}")
        out.append("")
    return out


def _render_analyzers(ctx: dict) -> list[str]:
    out = ["## Analyzer Verification", ""]
    out.append(f"Run directory: `{esc(ctx.get('run_dir'))}`"
                + (f" (run id `{esc(ctx.get('run_id'))}`)" if ctx.get("run_id") else ""))
    out.append("")
    out.append("| Analyzer | Present | Findings | Notes |")
    out.append("|---|---|---|---|")
    for a in ctx["analyzers"]:
        present = "yes" if a["present"] else "no"
        count = a.get("finding_count", "—")
        note = esc(a.get("error", "")) if a.get("error") else ("" if a["present"] else "not run / not found")
        out.append(f"| {esc(a['name'])} | {present} | {esc(count)} | {note} |")
    out.append("")
    return out


def _render_gaps(ctx: dict) -> list[str]:
    out = ["## Not Performed / Review Limits", ""]
    gaps = ctx.get("gaps") or []
    if not gaps:
        out.append("No missing artifacts detected by this renderer.")
        out.append("")
        return out
    out.append("Every artifact this renderer looked for but could not load is listed explicitly below "
                "— a missing artifact is a verification gap, not evidence of a clean design.")
    out.append("")
    out.append("| Artifact | Path | Reason |")
    out.append("|---|---|---|")
    for g in gaps:
        out.append(f"| {esc(g.get('artifact'))} | `{esc(g.get('path'))}` | {esc(g.get('reason'))} |")
    out.append("")
    return out


def _render_legend() -> list[str]:
    out = ["## Evidence & Verification States (legend)", ""]
    out.append("This renderer does not perform verification itself — it only displays the "
                "verification state that upstream tools (`deep_review_gate.py`, "
                "`merge_annotations.py`, `analyzer_triage.py`, or a human reviewer) already "
                "recorded. Generating this report is not a substitute for running those gates.")
    out.append("")
    for state, desc in STATE_DESCRIPTIONS.items():
        out.append(f"- `{state}`: {desc}")
    out.append("")
    return out


def _render_coverage(ctx: dict) -> list[str]:
    coverage = ctx.get("coverage") or {}
    lines = ["## Datasheet Coverage & Availability", ""]
    if not coverage.get("present"):
        lines.append("Coverage roster not available — component coverage is unknown.")
        lines.append("")
        return lines
    summary = coverage.get("summary") or {}
    lines.append("This is an inventory of available artifacts, not a verification result.")
    lines.append(f"Candidates: {esc(summary.get('candidates', len(coverage.get('candidates') or [])))}; "
                 f"missing MPN: {esc(summary.get('missing', len(coverage.get('missing') or [])))}; "
                 f"excluded: {esc(summary.get('excluded', len(coverage.get('excluded') or [])))}.")
    lines.append("")
    lines.append("| Ref | MPN | Class | PDF available | Vibe PDFs (current inventory) | Vibe inventory | Extraction available | Fetch status |")
    lines.append("|---|---|---|---|---:|---|---|---|")
    for part in coverage.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("vibe_inventory_current"):
            vibe_inventory = "current"
        elif part.get("vibe_inventory_stale"):
            vibe_inventory = "stale (last known)"
        else:
            vibe_inventory = "not recorded"
        lines.append(f"| {esc(part.get('ref'))} | {esc(part.get('mpn'))} | {esc(part.get('class'))} "
                     f"| {'yes' if part.get('pdf_available') else 'no'} "
                     f"| {esc(part.get('vibe_asset_count', 0)) if part.get('vibe_inventory_current') and part.get('vibe_asset_available') else 'no verified local Vibe PDFs'} "
                     f"| {vibe_inventory} | {'yes' if part.get('extraction_available') else 'no'} "
                     f"| {esc(part.get('fetch_status') or 'not recorded')} |")
    if coverage.get("vibe_manifest_present"):
        lines.append("Vibe PDF availability is read from the separate local Vibe manifest and verified against "
                     "the local file signature/hash. This confirms local file availability only; it does not "
                     "confirm extraction, datasheet correctness, or current remote catalog state.")
    for item in coverage.get("missing") or []:
        if isinstance(item, dict):
            lines.append(f"- Coverage gap: {esc(item.get('ref'))} — {esc(item.get('reason') or 'MPN missing')}")
    lines.append("")
    return lines


def _render_execution_metrics(ctx: dict) -> list[str]:
    metrics = ctx.get("execution_metrics") or {}
    lines = ["## Execution Metrics", ""]
    lines.append("Only explicitly recorded token counts and elapsed time are shown. No currency or estimates.")
    if not metrics.get("present"):
        lines.append("Run metrics were not recorded.")
        lines.append("")
        return lines
    if metrics.get("schema_errors"):
        lines.append("Metrics schema/run warnings: " + "; ".join(esc(x) for x in metrics["schema_errors"]))
    def _row(label: str, record: dict) -> str:
        def value(key: str) -> str:
            item = record.get(key)
            return "not recorded" if item is None else esc(item)
        return (f"| {label} | {value('input_tokens')} | {value('output_tokens')} "
                f"| {value('cache_read_tokens')} | {value('cache_write_tokens')} "
                f"| {value('duration_seconds')} |")
    lines.append("| Agent / total | Input tokens | Output tokens | Cache-read tokens | Cache-write tokens | Duration (s) |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for agent in metrics.get("agents") or []:
        if isinstance(agent, dict):
            lines.append(_row(esc(agent.get("domain") or "agent"), agent))
    if isinstance(metrics.get("totals"), dict):
        lines.append(_row("Total", metrics["totals"]))
    lines.append("")
    return lines


def _render_kicad_cli(ctx: dict) -> list[str]:
    cli = ctx.get("kicad_cli") or {}
    lines = ["## KiCad CLI Ground Truth", ""]
    if not cli.get("present"):
        lines.append("kicad-cli result not available; ERC/DRC status is a review gap, not a clean result.")
    else:
        summary = cli.get("summary") or {}
        for check, ran_key, count_key in (("ERC", "erc_ran", "erc_violations"),
                                          ("DRC", "drc_ran", "drc_geometric")):
            if cli.get(ran_key):
                lines.append(f"- {check}: ran; recorded count {esc(summary.get(count_key, 'not recorded'))}.")
            else:
                lines.append(f"- {check}: not run or result missing; not a passing result.")
        for note in cli.get("notes") or []:
            lines.append(f"- Note: {esc(note)}")
    lines.append("")
    return lines


def render_markdown(ctx: dict) -> str:
    lines = []
    lines.append("# Deep Review Report")
    lines.append("")
    lines.append(f"Generated by `render_report.py` at {esc(ctx.get('generated_at'))} "
                  f"from `{esc(ctx.get('analysis_dir'))}`.")
    lines.append("")
    lines.append("> This is a static report-generation utility. It renders existing analysis "
                  "artifacts; it does not re-run any analyzer or evidence gate, and producing "
                  "this report does not itself verify anything.")
    lines.append("")
    lines.extend(_render_synthesis(ctx))
    lines.extend(_render_deep_review(ctx))
    lines.extend(_render_design_context(ctx))
    lines.extend(_render_review_plan(ctx))
    lines.extend(_render_triage(ctx))
    lines.extend(_render_analyzer_triage(ctx))
    lines.extend(_render_board_facts(ctx))
    lines.extend(_render_analyzers(ctx))
    lines.extend(_render_coverage(ctx))
    lines.extend(_render_execution_metrics(ctx))
    lines.extend(_render_kicad_cli(ctx))
    lines.extend(_render_gaps(ctx))
    lines.extend(_render_legend())
    return "\n".join(lines).rstrip() + "\n"
