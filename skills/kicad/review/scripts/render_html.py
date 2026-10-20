"""Self-contained static HTML triage view for the Deep Review report.

The renderer consumes the normalized context from ``report_data.py`` and
writes no network or server integration. All report text is escaped for HTML;
the small JSON bootstrap block is separately protected against ``</script>``
termination. The browser-side triage is local to the current browser profile
(localStorage) and can be exported as an informal JSON sidecar.
"""
from __future__ import annotations

import json
from html import escape as _html_escape


_STATE_OPTIONS = (
    "verified_full", "verified_partial", "not_checked", "quarantined",
    "verified_ok", "refuted", "escalated",
)

_CSS = """
:root { color-scheme: light; font: 15px/1.5 system-ui, -apple-system, sans-serif;
        color: #17212b; background: #f5f7fa; }
* { box-sizing: border-box; }
body { max-width: 1440px; margin: 0 auto; padding: 1.25rem; }
h1, h2, h3 { color: #14202b; line-height: 1.25; }
h1 { margin-bottom: .35rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ccd4dc; padding-bottom: .35rem; }
h3 { margin-top: 1.25rem; }
.banner, .callout { background: #fff8df; border: 1px solid #e2cc78; padding: .8rem 1rem;
                    border-radius: 6px; margin: .75rem 0; }
.gap { background: #fff0f0; border: 1px solid #dda8a8; padding: .65rem .8rem;
       border-radius: 5px; margin: .4rem 0; overflow-wrap: anywhere; }
.card { background: #fff; border: 1px solid #d7dee5; border-radius: 6px;
        padding: .8rem 1rem; margin: .65rem 0; }
.card h3 { margin-top: 0; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 10px;
         background: #e8edf2; font-size: .84rem; }
.state-quarantined, .state-refuted { background: #f3e8e8; }
.state-not_checked, .state-not_recorded { background: #fff6df; }
.state-verified_full, .state-verified_ok { background: #e8f5e9; }
.state-escalated { background: #fcebd7; }
.summary { font-weight: 600; }
.muted { color: #53616f; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f1f4f7;
      padding: .65rem; border-radius: 4px; }
dl { display: grid; grid-template-columns: minmax(10rem, 18rem) 1fr; gap: .3rem .8rem; }
dt { font-weight: 650; }
dd { margin: 0; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; background: #fff; margin: .6rem 0 1rem; }
th, td { border: 1px solid #ccd4dc; padding: .45rem .55rem; text-align: left;
         vertical-align: top; overflow-wrap: anywhere; }
th { background: #eaf0f5; }
.filters { display: flex; flex-wrap: wrap; align-items: end; gap: .7rem; padding: .8rem;
           background: #edf2f6; border-radius: 6px; }
.filters label { display: grid; gap: .2rem; font-weight: 600; }
.filters input, .filters select { min-width: 10rem; padding: .4rem; font: inherit; }
#filter-search { min-width: 16rem; }
#download-bar { position: sticky; top: 0; z-index: 2; padding: .6rem 0;
                background: #f5f7fa; border-bottom: 2px solid #cbd4dc; }
button { font: inherit; cursor: pointer; }
#download-bar button { padding: .45rem .85rem; }
.triage-controls { min-width: 14rem; }
.triage-controls button { margin: 0 .25rem .25rem 0; }
.triage-controls textarea { display: block; width: 100%; min-height: 3.2rem; margin-top: .4rem;
                           font: inherit; }
.triage-status { font-weight: 700; }
.evidence-list { margin: .25rem 0 .25rem 1.2rem; padding: 0; }
progress { width: min(24rem, 100%); height: 1rem; }
@media (max-width: 760px) {
  body { padding: .7rem; }
  table { display: block; overflow-x: auto; }
  dl { grid-template-columns: 1fr; }
  dd { margin-bottom: .35rem; }
}
"""

_JS = r"""
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("triage-data").textContent);
  var RUN_KEY = "kicad-happy-triage:" + (DATA.run_id || DATA.analysis_dir || "default");
  var triageRows = Array.prototype.slice.call(
    document.querySelectorAll("#finding-table tbody tr[data-finding-id]")
  );
  var state = Object.create(null);

  function loadState() {
    try {
      var raw = window.localStorage.getItem(RUN_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (e) {
      /* Browser privacy settings may disable localStorage for file:// pages. */
    }
    return Object.create(null);
  }
  function saveState() {
    try {
      window.localStorage.setItem(RUN_KEY, JSON.stringify(state));
    } catch (e) {
      /* The current in-memory state remains available for the sidecar download. */
    }
  }
  function entryFor(id) {
    var entry = state[id];
    return entry && typeof entry === "object" && !Array.isArray(entry)
      ? entry : { status: "unset", comment: "" };
  }
  function renderTriage() {
    triageRows.forEach(function (row) {
      var id = row.getAttribute("data-finding-id");
      var entry = entryFor(id);
      var statusEl = row.querySelector(".triage-status");
      var textarea = row.querySelector("textarea[data-comment-for]");
      if (statusEl) statusEl.textContent = String(entry.status || "unset");
      if (textarea) {
        var comment = typeof entry.comment === "string" ? entry.comment : "";
        if (textarea.value !== comment) textarea.value = comment;
      }
    });
  }
  function applyFilters() {
    var severity = document.getElementById("filter-severity").value.toLowerCase();
    var source = document.getElementById("filter-source").value.toLowerCase();
    var verification = document.getElementById("filter-state").value.toLowerCase();
    var query = document.getElementById("filter-search").value.trim().toLowerCase();
    var visible = 0;
    triageRows.forEach(function (row) {
      var matches = (!severity || row.getAttribute("data-severity").toLowerCase() === severity) &&
        (!source || row.getAttribute("data-source").toLowerCase() === source) &&
        (!verification || row.getAttribute("data-state").toLowerCase() === verification) &&
        (!query || row.textContent.toLowerCase().indexOf(query) !== -1);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    document.getElementById("visible-count").textContent = String(visible);
    document.getElementById("total-count").textContent = String(triageRows.length);
  }
  function downloadSidecar() {
    var sidecar = {
      schema_version: "1.0",
      kind: "deep_review_triage_sidecar",
      run_id: DATA.run_id || null,
      analysis_dir: DATA.analysis_dir || null,
      generated_at: new Date().toISOString(),
      triage: state
    };
    var blob = new Blob([JSON.stringify(sidecar, null, 2)], { type: "application/json" });
    var blobUrl = window.URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = blobUrl;
    link.download = "deep_review_triage.json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { window.URL.revokeObjectURL(blobUrl); }, 1000);
  }
  function setTriage(id, patch) {
    var current = entryFor(id);
    state[id] = Object.assign({}, current, patch);
    saveState();
    renderTriage();
  }

  state = loadState();
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-action]");
    if (!button) return;
    var action = button.getAttribute("data-action");
    if (action === "download") {
      downloadSidecar();
      return;
    }
    var row = button.closest("tr[data-finding-id]");
    if (!row) return;
    var id = row.getAttribute("data-finding-id");
    if (action === "resolve") setTriage(id, { status: "resolved" });
    if (action === "ignore") setTriage(id, { status: "ignored" });
    if (action === "reset") setTriage(id, { status: "unset" });
  });
  document.addEventListener("input", function (event) {
    var search = event.target.closest("#filter-search");
    if (search) applyFilters();
    var textarea = event.target.closest("textarea[data-comment-for]");
    if (textarea) setTriage(textarea.getAttribute("data-comment-for"), { comment: textarea.value });
  });
  document.addEventListener("change", function (event) {
    if (event.target.matches("#filter-severity, #filter-source, #filter-state")) applyFilters();
  });

  renderTriage();
  applyFilters();
})();
"""


def _esc(value) -> str:
    """HTML-escape an untrusted value, including values placed in attributes."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return _html_escape(str(value), quote=True)


def _shown(value, fallback: str = "Not provided") -> str:
    return _esc(fallback if value is None or value == "" else value)


def _safe_json_for_script(data) -> str:
    """Serialize JSON without allowing a value to terminate its script block."""
    return json.dumps(data, sort_keys=True).replace("</", "<\\/")


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _json_text(value) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _collect_triage_rows(ctx: dict) -> list[dict]:
    """Flatten normalized Deep Review findings, quarantine and annotations."""
    rows = []
    deep_review = ctx.get("deep_review") or {}
    for finding in _list(deep_review.get("findings")):
        if not isinstance(finding, dict):
            continue
        rows.append({
            "finding_id": finding.get("finding_id") or "(unassigned)",
            "source": "deep_review",
            "category": finding.get("category", ""),
            "severity": finding.get("severity", ""),
            "confidence": finding.get("confidence", ""),
            "summary": finding.get("summary", ""),
            "state": finding.get("_state") or "not_recorded",
            "finding": finding,
        })
    for quarantine in _list(deep_review.get("quarantined")):
        if not isinstance(quarantine, dict):
            continue
        rows.append({
            "finding_id": quarantine.get("finding_id") or "(quarantined, no id)",
            "source": "deep_review",
            "category": quarantine.get("category", ""),
            "severity": quarantine.get("severity", ""),
            "confidence": quarantine.get("confidence", ""),
            "summary": quarantine.get("summary", "(no summary)"),
            "state": quarantine.get("_state") or "quarantined",
            "disposition": quarantine.get("_disposition") or "gate_quarantined",
            "quarantine_reason": quarantine.get("quarantine_reason", ""),
            "finding": quarantine,
        })
    annotations = ctx.get("review_annotations") or {}
    for annotation in _list(annotations.get("annotations")):
        if not isinstance(annotation, dict):
            continue
        rows.append({
            "finding_id": annotation.get("finding_id") or "(unassigned)",
            "source": "review_annotations",
            "category": "",
            "severity": annotation.get("suggested_severity", ""),
            "confidence": annotation.get("confidence", ""),
            "summary": annotation.get("reason", ""),
            # The normalizer's explicit label is authoritative. Do not derive
            # a refuted/confirmed state from prose or missing fields here.
            "state": annotation.get("_state") or "not_recorded",
            "finding": annotation,
        })
    return rows


def _render_schema_errors(errors) -> str:
    errors = _list(errors)
    if not errors:
        return ""
    out = ["<div class=\"callout\"><strong>Schema issues recorded by the loader</strong><ul>"]
    out.extend(f"<li>{_esc(error)}</li>" for error in errors)
    out.append("</ul></div>")
    return "".join(out)


def _render_synthesis(ctx: dict) -> str:
    synthesis = ctx.get("synthesis") or {}
    out = ['<section id="synthesis"><h2>Verdict &amp; Synthesis</h2>']
    if not synthesis.get("present"):
        out.append('<p><code>synthesis.json</code> is unavailable; no normalized synthesis was recorded.</p>')
        out.append("</section>")
        return "".join(out)
    out.append(_render_schema_errors(synthesis.get("schema_errors")))
    out.append("<dl>")
    out.append(f"<dt>Verdict status</dt><dd>{_shown(synthesis.get('verdict_status'))}</dd>")
    out.append(f"<dt>Verdict summary</dt><dd>{_shown(synthesis.get('verdict_summary'))}</dd>")
    out.append("</dl>")
    for heading, key, text_key in (
        ("Top Risks", "top_risks", "reason"),
        ("Verified OK (as recorded in synthesis)", "verified_ok", "note"),
        ("Not Checked / Explicit Gaps", "not_checked", "reason"),
    ):
        items = _list(synthesis.get(key))
        out.append(f"<h3>{_esc(heading)}</h3>")
        if not items:
            out.append("<p>None listed in the synthesis artifact.</p>")
            continue
        out.append('<ul class="evidence-list">')
        for item in items:
            if isinstance(item, dict):
                finding_id = item.get("finding_id")
                text = item.get(text_key) or item.get("reason") or item.get("note")
                prefix = f"<code>{_esc(finding_id)}</code>: " if finding_id else ""
                out.append(f"<li>{prefix}{_shown(text)}</li>")
            else:
                out.append(f"<li>{_esc(item)}</li>")
        out.append("</ul>")
    contradictions = _list(synthesis.get("contradictions"))
    out.append("<h3>Contradictions</h3>")
    if contradictions:
        out.append('<ul class="evidence-list">')
        for item in contradictions:
            if not isinstance(item, dict):
                out.append(f"<li>{_esc(item)}</li>")
                continue
            ids = ", ".join(f"<code>{_esc(fid)}</code>" for fid in _list(item.get("finding_ids")))
            out.append(f"<li>{ids}: {_shown(item.get('description'))}</li>")
        out.append("</ul>")
    else:
        out.append("<p>None listed in the synthesis artifact.</p>")
    extra = synthesis.get("extra")
    if isinstance(extra, dict) and extra:
        out.append("<h3>Additional synthesis data</h3>")
        out.append(f"<pre>{_esc(_json_text(extra))}</pre>")
    out.append("</section>")
    return "".join(out)


def _render_evidence(finding: dict) -> str:
    evidence = finding.get("evidence") or {}
    out = ['<div class="evidence"><strong>Evidence recorded</strong>']
    if not isinstance(evidence, dict):
        out.append(f"<pre>{_esc(_json_text(evidence))}</pre></div>")
        return "".join(out)
    anchors_found = False
    for key in ("components", "nets", "pins"):
        values = evidence.get(key)
        if not values:
            values = finding.get(key)
        if values:
            anchors_found = True
            out.append(f"<p><strong>{_esc(key.title())}:</strong> {_shown(values)}</p>")
    if not anchors_found:
        out.append("<p>No component, net or pin anchors were included in this normalized finding.</p>")
    datasheets = _list(evidence.get("datasheet"))
    if datasheets:
        out.append("<h4>Datasheet citations</h4><ul class=\"evidence-list\">")
        for citation in datasheets:
            if not isinstance(citation, dict):
                out.append(f"<li>{_esc(citation)}</li>")
                continue
            page = citation.get("page")
            page_text = f"; page {_esc(page)}" if page is not None else ""
            out.append("<li><strong>MPN:</strong> " + _shown(citation.get("mpn")) + page_text)
            if citation.get("quote") is not None:
                out.append(f"<blockquote>{_esc(citation.get('quote'))}</blockquote>")
            out.append("</li>")
        out.append("</ul>")
    computation = evidence.get("computation")
    if computation:
        if isinstance(computation, dict):
            out.append("<h4>Computation</h4><dl>")
            for label, key in (("Description", "description"), ("Result", "result"), ("Script", "script")):
                if computation.get(key) is not None:
                    out.append(f"<dt>{label}</dt><dd>{_esc(computation.get(key))}</dd>")
            out.append("</dl>")
        else:
            out.append(f"<p><strong>Computation:</strong> {_esc(computation)}</p>")
    extra = {key: value for key, value in evidence.items()
             if key not in {"components", "nets", "pins", "datasheet", "computation"}}
    if extra:
        out.append(f"<details><summary>Other evidence fields</summary><pre>{_esc(_json_text(extra))}</pre></details>")
    out.append("</div>")
    return "".join(out)


def _render_deep_review(ctx: dict) -> str:
    deep_review = ctx.get("deep_review") or {}
    findings = [f for f in _list(deep_review.get("findings")) if isinstance(f, dict)]
    quarantined = [q for q in _list(deep_review.get("quarantined")) if isinstance(q, dict)]
    meta = deep_review.get("meta") or {}
    out = ['<section id="deep-review"><h2>Deep Review Findings &amp; Evidence</h2>']
    if not deep_review.get("present"):
        out.append("<p><code>deep_review.json</code> is unavailable. Its absence is a report gap, "
                   "not evidence that the design has no issues.</p>")
    else:
        out.append("<dl>")
        for label, key in (("Produced for run", "produced_for_run_id"),
                           ("Produced at", "produced_at"), ("Schema version", "schema_version")):
            out.append(f"<dt>{label}</dt><dd>{_shown(meta.get(key))}</dd>")
        out.append("</dl>")
        out.append(_render_schema_errors(deep_review.get("schema_errors")))
        if findings:
            for finding in findings:
                fid = finding.get("finding_id")
                out.append('<article class="card deep-review-finding">')
                out.append(f"<h3>{_shown(finding.get('category'), 'Uncategorized')} — "
                           f"{_shown(finding.get('severity'), 'Severity not recorded')}</h3>")
                out.append(f"<p class=\"summary\">{_shown(finding.get('summary'))}</p>")
                out.append("<p><strong>Finding ID:</strong> "
                           f"<code>{_shown(fid, 'Not assigned')}</code>; "
                           f"<strong>Confidence:</strong> {_shown(finding.get('confidence'))}; "
                           f"<strong>Recorded verification state:</strong> "
                           f"<span class=\"badge\">{_shown(finding.get('_state'), 'Not recorded')}</span></p>")
                if finding.get("description") is not None:
                    out.append(f"<p><strong>Description:</strong> {_esc(finding.get('description'))}</p>")
                if finding.get("recommendation") is not None:
                    out.append(f"<p><strong>Recommendation:</strong> {_esc(finding.get('recommendation'))}</p>")
                out.append(_render_evidence(finding))
                out.append("</article>")
        else:
            out.append("<p>No kept findings are recorded in this artifact.</p>")
    out.append("<h3>Quarantined claims</h3>")
    if quarantined:
        out.append("These entries remain visible with their recorded provenance. A verifier-refuted "
                   "entry is distinct from an evidence-gate quarantine and from reviewer suppression.")
        out.append("<ul class=\"evidence-list\">")
        for item in quarantined:
            disposition = item.get("_disposition") or "gate_quarantined"
            label = "Verifier refuted" if disposition == "verifier_refuted" else "Evidence-gate quarantine"
            out.append('<li class="card"><div class="state-quarantined">')
            out.append(f"<p><strong>Disposition:</strong> {_esc(label)}</p>")
            out.append(f"<p class=\"summary\">{_shown(item.get('summary'), '(no summary recorded)')}</p>")
            out.append(f"<p><strong>Category:</strong> {_shown(item.get('category'))}; "
                       f"<strong>Recorded state:</strong> {_shown(item.get('_state'), 'quarantined')}</p>")
            out.append(f"<p><strong>Quarantine reason:</strong> {_shown(item.get('quarantine_reason'))}</p>")
            out.append(_render_evidence(item))
            out.append("</div></li>")
        out.append("</ul>")
    else:
        out.append("<p>No quarantined entries are recorded.</p>")
    out.append("</section>")
    return "".join(out)


def _render_annotations(ctx: dict) -> str:
    annotations = ctx.get("review_annotations") or {}
    items = [a for a in _list(annotations.get("annotations")) if isinstance(a, dict)]
    observations = [o for o in _list(annotations.get("reviewer_observations")) if isinstance(o, dict)]
    out = ['<section id="review-annotations"><h2>Reviewer Annotations (including refuted)</h2>']
    if not annotations.get("present"):
        out.append("<p><code>review_annotations.json</code> is unavailable; no reviewer annotation "
                   "state is represented here.</p>")
    else:
        out.append(_render_schema_errors(annotations.get("schema_errors")))
        out.append("<p>Statuses and normalized states below are displayed as recorded by the "
                   "upstream annotation artifact; this renderer does not independently decide them.</p>")
        if items:
            for item in items:
                state = item.get("_state") or "not_recorded"
                out.append(f'<article class="card state-{_esc(state)}">')
                out.append(f"<h3><code>{_shown(item.get('finding_id'), 'Not assigned')}</code></h3>")
                out.append(f"<p><strong>Recorded status:</strong> {_shown(item.get('status'))}; "
                           f"<strong>Normalized state:</strong> {_shown(item.get('_state'), 'Not recorded')}; "
                           f"<strong>Confidence:</strong> {_shown(item.get('confidence'))}; "
                           f"<strong>Suggested severity:</strong> {_shown(item.get('suggested_severity'))}</p>")
                out.append(f"<p>{_shown(item.get('reason'))}</p></article>")
        else:
            out.append("<p>No individual annotations are recorded.</p>")
        if observations:
            out.append("<h3>Reviewer observations</h3>")
            for observation in observations:
                out.append('<article class="card">')
                out.append(f"<p class=\"summary\">{_shown(observation.get('observation'))}</p>")
                out.append(f"<p><strong>Severity:</strong> {_shown(observation.get('severity'))}; "
                           f"<strong>Confidence:</strong> {_shown(observation.get('confidence'))}</p>")
                out.append(f"<p>{_shown(observation.get('reasoning'))}</p></article>")
    out.append("</section>")
    return "".join(out)


def _render_review_plan(ctx: dict) -> str:
    plan = ctx.get("review_plan") or {}
    tasks = [t for t in _list(plan.get("tasks")) if isinstance(t, dict)]
    out = ['<section id="review-plan"><h2>Review Plan &amp; Task Progress</h2>']
    if not plan.get("present"):
        out.append("<p><code>review_plan.json</code> is unavailable; no task progress is recorded.</p>")
        out.append("</section>")
        return "".join(out)
    out.append(f"<p><strong>Plan:</strong> {_shown(plan.get('plan_id'))}; "
               f"<strong>Purpose:</strong> {_shown(plan.get('purpose'))}; "
               f"<strong>Created:</strong> {_shown(plan.get('created_at'))}</p>")
    present_count = sum(1 for task in tasks if task.get("_result_exists") is True)
    task_count = len(tasks)
    out.append(f'<p><strong>Result artifacts present:</strong> {present_count} of {task_count} tasks. '
               "Artifact presence is reported as availability, not proof that a task completed successfully.</p>")
    out.append(f'<progress max="{task_count or 1}" value="{present_count}" aria-label="Task result artifacts present"></progress>')
    if tasks:
        out.append("<table><thead><tr><th>Task</th><th>Type / Tier</th><th>Result path</th>"
                   "<th>Result artifact</th><th>Recorded status</th></tr></thead><tbody>")
        for task in tasks:
            result_present = "Present" if task.get("_result_exists") is True else "Not present"
            out.append("<tr>"
                       f"<td>{_shown(task.get('task_id'))}</td>"
                       f"<td>{_shown(task.get('task_type'))} / {_shown(task.get('tier'))}</td>"
                       f"<td><code>{_shown(task.get('result_path'))}</code></td>"
                       f"<td>{result_present}</td>"
                       f"<td>{_shown(task.get('status'), 'Not recorded')}</td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<p>No tasks are listed in the plan.</p>")
    out.append("</section>")
    return "".join(out)


def _render_analyzer_triage(ctx: dict) -> str:
    triage = ctx.get("analyzer_triage") or {}
    out = ['<section id="analyzer-triage"><h2>Analyzer Triage</h2>']
    if not triage.get("present"):
        out.append("<p><code>triage.json</code> is unavailable; no Layer 1 triage roll-up is recorded.</p>")
        out.append("</section>")
        return "".join(out)
    out.append(f"<p>{_shown(triage.get('finding_count'))} finding(s) in the recorded triage; "
               f"{_shown(triage.get('must_fix_count'))} listed as must-fix.</p>")
    table = [row for row in _list(triage.get("triage_table")) if isinstance(row, dict)]
    if table:
        out.append("<table><thead><tr><th>Rule</th><th>Count</th><th>Real</th>"
                   "<th>Likely FP</th><th>Review</th><th>Reason</th></tr></thead><tbody>")
        for row in table:
            out.append("<tr>"
                       f"<td><code>{_shown(row.get('rule_id'))}</code></td>"
                       f"<td>{_shown(row.get('count'))}</td>"
                       f"<td>{_shown(row.get('real'))}</td>"
                       f"<td>{_shown(row.get('likely_fp'))}</td>"
                       f"<td>{_shown(row.get('review'))}</td>"
                       f"<td>{_shown(row.get('reason'))}</td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<p>No per-rule triage roll-up is recorded.</p>")
    out.append("</section>")
    return "".join(out)


def _datasheet_citations(ctx: dict) -> list[dict]:
    citations = []
    deep_review = ctx.get("deep_review") or {}
    for finding in _list(deep_review.get("findings")):
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence") or {}
        if isinstance(evidence, dict):
            for citation in _list(evidence.get("datasheet")):
                if isinstance(citation, dict):
                    citations.append(citation)
    return citations


def _render_coverage_availability(ctx: dict) -> str:
    analyzers = [item for item in _list(ctx.get("analyzers")) if isinstance(item, dict)]
    inputs = {str(item.get("name")): item for item in analyzers}
    citations = _datasheet_citations(ctx)
    out = ['<section id="coverage-availability"><h2>Coverage, PDF &amp; Extraction Availability</h2>']
    out.append("<p>This section reports only normalized artifact/reference availability. A listed "
               "citation, PDF, extraction or analyzer artifact is not a verification result.</p>")
    out.append("<h3>Coverage input artifacts</h3>")
    out.append("<table><thead><tr><th>Input</th><th>Artifact availability in context</th>"
               "<th>Finding count when recorded</th></tr></thead><tbody>")
    for name in ("schematic", "pcb"):
        item = inputs.get(name)
        if item is None:
            present, count = "Not represented", "Not provided"
        else:
            present = "Present" if item.get("present") else "Not present"
            count = item.get("finding_count") if item.get("finding_count") is not None else "Not provided"
        out.append(f"<tr><td>{_esc(name)}</td><td>{_esc(present)}</td><td>{_esc(count)}</td></tr>")
    out.append("</tbody></table>")
    out.append(f"<p><strong>Datasheet citation entries in kept Deep Review findings:</strong> {len(citations)}.</p>")
    if citations:
        out.append("<ul class=\"evidence-list\">")
        for citation in citations:
            page = f"; page {_esc(citation.get('page'))}" if citation.get("page") is not None else ""
            out.append(f"<li>MPN {_shown(citation.get('mpn'))}{page}; citation is recorded, file availability is unknown.</li>")
        out.append("</ul>")
    else:
        out.append("<p>No datasheet citation entries are present in the kept Deep Review findings.</p>")
    coverage = ctx.get("coverage") or {}
    parts = [item for item in _list(coverage.get("parts")) if isinstance(item, dict)]
    if parts:
        out.append("<h3>Per-part availability</h3><table><thead><tr><th>MPN</th><th>Local PDF availability</th>"
                   "<th>Vibe inventory</th><th>Verified local Vibe PDFs</th><th>Extraction artifact</th>"
                   "<th>Fetch status</th></tr></thead><tbody>")
        for part in parts:
            if part.get("vibe_inventory_current"):
                inventory = "Current manifest snapshot"
            elif part.get("vibe_inventory_stale"):
                inventory = "Stale last-known snapshot"
            else:
                inventory = "Not recorded"
            vibe_files = (str(part.get("vibe_asset_count", 0))
                          if part.get("vibe_inventory_current") and part.get("vibe_asset_available")
                          else "None verified / unavailable")
            out.append(f"<tr><td>{_shown(part.get('mpn'))}</td>"
                       f"<td>{'Available' if part.get('pdf_available') else 'Not available'}</td>"
                       f"<td>{_esc(inventory)}</td><td>{_esc(vibe_files)}</td>"
                       f"<td>{'Available' if part.get('extraction_available') else 'Not available'}</td>"
                       f"<td>{_shown(part.get('fetch_status'), 'Not recorded')}</td></tr>")
        out.append("</tbody></table>")
        out.append("<p>Vibe inventory availability is checked against local file signature and SHA-256; "
                   "it is not datasheet extraction or engineering verification.</p>")
    else:
        out.append("<p><strong>PDF / extraction availability:</strong> Unknown; no per-part coverage rows were loaded.</p>")
    out.append("</section>")
    return "".join(out)


def _render_design_context(ctx: dict) -> str:
    design = ctx.get("design_context") or {}
    board = ctx.get("board_facts") or {}
    analyzers = [item for item in _list(ctx.get("analyzers")) if isinstance(item, dict)]
    out = ['<section id="kicad-ground-truth"><h2>KiCad Ground Truth &amp; Board Facts</h2>']
    out.append("<p>Values below are reported from the normalized design-context, raw board-facts and "
               "analyzer-artifact metadata. This renderer does not inspect a KiCad board or verify them.</p>")
    out.append("<h3>Design context (declared/inferred input)</h3>")
    if not design.get("present"):
        out.append("<p><code>design_context.json</code> is unavailable.</p>")
    else:
        data = design.get("data") if isinstance(design.get("data"), dict) else {}
        out.append("<dl>")
        for key in ("design_category", "environment"):
            value = data.get(key)
            if isinstance(value, dict):
                shown = (f"effective={value.get('effective')}; inferred={value.get('inferred')}; "
                         f"declared={value.get('declared')}")
            else:
                shown = value
            out.append(f"<dt>{_esc(key.replace('_', ' ').title())}</dt><dd>{_shown(shown)}</dd>")
        for label, key in (("Compliance targets", "compliance_targets"),
                           ("User-declared intent", "user_declared_intent"),
                           ("Confidence", "confidence"), ("Resolution", "resolution"),
                           ("Evidence", "evidence")):
            if data.get(key) is not None:
                out.append(f"<dt>{label}</dt><dd>{_esc(data.get(key))}</dd>")
        out.append("</dl>")
    out.append("<h3>Raw board facts</h3>")
    if not board.get("present"):
        out.append("<p><code>board_facts.json</code> is unavailable; no raw PCB facts are recorded here.</p>")
    else:
        out.append("<dl>")
        for label, key in (("Multi-ply stackup noted", "stackup_multi_ply"),
                           ("Impedance control noted", "impedance_control_noted"),
                           ("Graphic-copper nets", "graphic_copper_net_count"),
                           ("Unnetted copper shapes", "unnetted_copper_count"),
                           ("Board-fact finding count", "finding_count")):
            out.append(f"<dt>{label}</dt><dd>{_shown(board.get(key))}</dd>")
        out.append("</dl>")
        warnings = _list(board.get("stackup_warnings"))
        if warnings:
            out.append("<h4>Recorded stackup warnings</h4><ul class=\"evidence-list\">")
            out.extend(f"<li>{_esc(warning)}</li>" for warning in warnings)
            out.append("</ul>")
    out.append("<h3>Analyzer artifact availability</h3>")
    if analyzers:
        out.append("<table><thead><tr><th>Analyzer</th><th>Artifact present</th>"
                   "<th>Finding count</th><th>Path / loader note</th></tr></thead><tbody>")
        for item in analyzers:
            present = "Present" if item.get("present") else "Not present"
            count = item.get("finding_count") if item.get("finding_count") is not None else "Not provided"
            note = item.get("error") or item.get("path") or ""
            out.append(f"<tr><td>{_shown(item.get('name'))}</td><td>{present}</td>"
                       f"<td>{_esc(count)}</td><td><code>{_esc(note)}</code></td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<p>No analyzer availability entries are represented in the context.</p>")
    out.append("</section>")
    return "".join(out)


def _actual_metrics(ctx: dict) -> list[tuple[str, int | float]]:
    """Return only explicitly recorded numeric token/duration fields."""
    found = []
    excluded_parts = ("cost", "price", "money", "usd", "currency")

    def visit(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key)
                lowered = name.lower()
                next_path = f"{path}.{name}" if path else name
                if any(part in lowered for part in excluded_parts):
                    continue
                if ("token" in lowered or "duration" in lowered) and isinstance(child, (int, float)) \
                        and not isinstance(child, bool):
                    found.append((next_path, child))
                elif isinstance(child, (dict, list)):
                    visit(child, next_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}[{index}]")

    visit(ctx)
    return found


def _render_metrics(ctx: dict) -> str:
    metrics = _actual_metrics(ctx)
    out = ['<section id="metrics"><h2>Recorded Metrics</h2>']
    out.append("<p>Only numeric token and duration values explicitly present in the normalized "
               "context are shown. No token counts or durations are estimated; monetary values are omitted.</p>")
    if metrics:
        out.append("<ul class=\"evidence-list\">")
        out.extend(f"<li><code>{_esc(name)}</code>: {_esc(value)}</li>" for name, value in metrics)
        out.append("</ul>")
    else:
        out.append("<p>No numeric token or duration metrics were recorded in the report context.</p>")
    out.append("</section>")
    return "".join(out)


def _filter_select(select_id: str, label: str, options: list[tuple[str, str]]) -> str:
    out = [f'<label for="{_esc(select_id)}">{_esc(label)}<select id="{_esc(select_id)}">']
    for value, text in options:
        out.append(f'<option value="{_esc(value)}">{_esc(text)}</option>')
    out.append("</select></label>")
    return "".join(out)


def _render_interactive_triage(ctx: dict) -> str:
    rows = _collect_triage_rows(ctx)
    out = ['<section id="interactive-triage"><h2>Local Finding Triage</h2>']
    out.append("<p>Resolve/ignore/reset and comments are local browser annotations. They do not alter "
               "upstream verification states or source artifacts.</p>")
    out.append('<div id="download-bar"><button type="button" data-action="download">'
               "Download informal triage sidecar (JSON)</button></div>")
    out.append('<div class="filters" aria-label="Finding filters">')
    out.append(_filter_select("filter-severity", "Severity", [
        ("", "All severities"), ("error", "Error"), ("warning", "Warning"), ("info", "Info"),
    ]))
    out.append(_filter_select("filter-source", "Source", [
        ("", "All sources"), ("deep_review", "Deep Review"), ("review_annotations", "Reviewer annotation"),
    ]))
    state_options = [("", "All states")] + [(state, state.replace("_", " ")) for state in _STATE_OPTIONS]
    observed_states = sorted({str(row.get("state") or "not_recorded") for row in rows})
    state_options.extend((state, state.replace("_", " ")) for state in observed_states
                         if state not in _STATE_OPTIONS)
    out.append(_filter_select("filter-state", "Recorded state", state_options))
    out.append('<label for="filter-search">Text search<input id="filter-search" type="search" '
               'autocomplete="off" placeholder="Search IDs, summaries, evidence..."></label>')
    out.append('</div><p id="finding-count" aria-live="polite">Showing '
               '<span id="visible-count">0</span> of <span id="total-count">0</span> findings</p>')
    out.append('<table id="finding-table"><thead><tr><th>Finding ID</th><th>Source</th>'
               '<th>Category</th><th>Severity</th><th>Confidence</th><th>Summary &amp; Evidence</th>'
               '<th>Recorded state</th><th>Local triage</th></tr></thead><tbody>')
    if not rows:
        out.append('<tr class="empty-row"><td colspan="8">No finding rows are available.</td></tr>')
    for row in rows:
        finding_id = row.get("finding_id", "(unassigned)")
        finding = row.get("finding") if isinstance(row.get("finding"), dict) else {}
        state = row.get("state") or "not_recorded"
        severity = row.get("severity") or ""
        source = row.get("source") or ""
        out.append(f'<tr data-finding-id="{_esc(finding_id)}" data-source="{_esc(source)}" '
                   f'data-severity="{_esc(str(severity).lower())}" data-state="{_esc(str(state).lower())}">')
        out.append(f"<td><code>{_esc(finding_id)}</code></td><td>{_esc(source)}</td>")
        out.append(f"<td>{_esc(row.get('category'))}</td><td>{_esc(severity)}</td>")
        out.append(f"<td>{_esc(row.get('confidence'))}</td><td><div class=\"summary\">{_esc(row.get('summary'))}</div>")
        if row.get("quarantine_reason"):
            out.append(f"<p><strong>Quarantine reason:</strong> {_esc(row.get('quarantine_reason'))}</p>")
        if finding:
            out.append(_render_evidence(finding))
        out.append(f'<td><span class="badge state-{_esc(state)}">{_esc(state)}</span></td>')
        out.append('<td class="triage-controls">'
                   '<button type="button" data-action="resolve">Resolve</button>'
                   '<button type="button" data-action="ignore">Ignore</button>'
                   '<button type="button" data-action="reset">Reset</button>'
                   '<div>Status: <span class="triage-status">unset</span></div>'
                   f'<label>Comment<textarea data-comment-for="{_esc(finding_id)}" '
                   'placeholder="Reviewer comment"></textarea></label></td>')
        out.append("</tr>")
    out.append("</tbody></table></section>")
    return "".join(out)


def _render_gaps(ctx: dict) -> str:
    gaps = _list(ctx.get("gaps"))
    out = ['<section id="explicit-gaps"><h2>Explicit Gaps &amp; Limits</h2>']
    out.append("<p>Missing or unreadable artifacts are surfaced as gaps, not treated as evidence of a clean design.</p>")
    if not gaps:
        out.append("<p>No missing or invalid artifacts were recorded by the context loader.</p>")
    else:
        for gap in gaps:
            if not isinstance(gap, dict):
                out.append(f'<div class="gap">{_esc(gap)}</div>')
                continue
            out.append('<div class="gap"><strong>')
            out.append(_shown(gap.get("artifact")))
            out.append("</strong>")
            if gap.get("path") is not None:
                out.append(f" (<code>{_esc(gap.get('path'))}</code>)")
            out.append(f": {_shown(gap.get('reason'))}</div>")
    out.append("</section>")
    return "".join(out)


def render_html(ctx: dict) -> str:
    """Render one standalone HTML document from normalized ``report_data`` context."""
    triage_data = {
        "run_id": ctx.get("run_id"),
        "analysis_dir": ctx.get("analysis_dir"),
        "generated_at": ctx.get("generated_at"),
    }
    body = [
        '<header><h1>Deep Review Report &amp; Triage</h1>',
        f"<p>Generated at {_shown(ctx.get('generated_at'))} from "
        f"<code>{_shown(ctx.get('analysis_dir'))}</code>.</p></header>",
        '<div class="banner">This offline report renders existing normalized artifacts. It does not '
        'run analyzers, validate evidence, or independently verify findings. Browser annotations '
        'are stored locally and the downloaded JSON sidecar is informal, not a replacement for '
        '<code>review_annotations.json</code>.</div>',
        _render_synthesis(ctx),
        _render_deep_review(ctx),
        _render_annotations(ctx),
        _render_review_plan(ctx),
        _render_analyzer_triage(ctx),
        _render_coverage_availability(ctx),
        _render_design_context(ctx),
        _render_metrics(ctx),
        _render_interactive_triage(ctx),
        _render_gaps(ctx),
    ]
    return "\n".join((
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Deep Review Report &amp; Triage</title>",
        f"<style>{_CSS}</style>",
        "</head><body>",
        "".join(body),
        f'<script id="triage-data" type="application/json">{_safe_json_for_script(triage_data)}</script>',
        f"<script>{_JS}</script>",
        "</body></html>",
    )) + "\n"
