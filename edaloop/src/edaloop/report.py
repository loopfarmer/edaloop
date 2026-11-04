"""The scorecard: one page that says whether this exact board is ready, and on what evidence."""
from __future__ import annotations

import html
from pathlib import Path

from .checks import gate_state
from .ledger import sha_file
from .project import Project

CSS = """
:root{--bg:#fbfaf8;--panel:#fff;--ink:#22201c;--muted:#6b665d;--line:#e4e0d8;--ok:#1f7a4d;--warn:#9a6700;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#191816;--panel:#23211e;--ink:#ece8e1;--muted:#a39d92;--line:#3a3732;--ok:#5cc08d;--warn:#d9a93f;--bad:#ef7f77}}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:900px;margin:auto}h1{font-size:24px;margin:0}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--muted);font-size:13px;word-break:break-all}.verdict{font-weight:600;margin:12px 0}
.table{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}tr:last-child td{border-bottom:0}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.pass,.met,.agree{color:var(--ok);font-weight:600}.warn{color:var(--warn);font-weight:600}
.fail,.error,.stale,.missing,.missed,.outlier,.no_majority,.design_off_target{color:var(--bad);font-weight:600}
li{margin:4px 0}
"""


def _e(v) -> str:
    return html.escape(str(v if v is not None else "-"))


def write_report(p: Project, out: Path) -> Path:
    rows = gate_state(p)
    latest = p.ledger.latest()
    ready = all(r["state"] in ("pass", "warn") for r in rows)
    parts = [f"<title>{_e(p.name)} scorecard</title><style>{CSS}</style><main>",
             f"<h1>{_e(p.name)}</h1><div class=sub>board sha256 {sha_file(p.board)}</div>"]
    blockers = [r["check"] for r in rows if r["state"] not in ("pass", "warn")]
    parts.append(f"<p class=verdict>{'Every gated check holds on these exact bytes.' if ready else 'Not ready: ' + _e(', '.join(blockers))}</p>")

    parts.append("<h2>Gate</h2><div class=table><table><tr><th>Check<th>Stage<th>State<th>Result<th>When</tr>")
    for r in rows:
        parts.append(f"<tr><td>{_e(r['check'])}<td>{_e(r['stage'])}<td class={_e(r['state'])}>{_e(r['state'])}"
                     f"<td>{_e(r['summary'])}<td class=num>{_e(r['when'])}</tr>")
    parts.append("</table></div>")

    req = latest.get("requirements")
    if req and req.metrics.get("rows"):
        parts.append("<h2>Requirements</h2><div class=table><table><tr><th>Id<th>Requirement<th>Limit<th>Value<th>State</tr>")
        for r in req.metrics["rows"]:
            limit = " to ".join(str(x) for x in (r["min"], r["max"]) if x is not None)
            parts.append(f"<tr><td>{_e(r['id'])}<td>{_e(r['text'])}<td class=num>{_e(limit)}<td class=num>{_e(r['value'])}"
                         f"<td class={_e(r['state'].split()[0])}>{_e(r['state'])}</tr>")
        parts.append("</table></div>")

    em = latest.get("em_crosscheck")
    if em and em.metrics.get("solvers"):
        d = em.metrics.get("diagnosis", {})
        parts.append(f"<h2>EM cross-check at {_e(em.metrics.get('at_ghz'))} GHz: "
                     f"<span class={_e(d.get('verdict'))}>{_e(d.get('verdict'))}</span></h2>")
        parts.append("<div class=table><table><tr><th>Solver<th>Zc, ohm<th>eps_eff<th>Insertion loss, dB<th>Run time, s</tr>")
        for k, v in em.metrics["solvers"].items():
            parts.append(f"<tr><td>{_e(k)}<td class=num>{_e(v['zc_ohm'])}<td class=num>{_e(v['eps_eff'])}"
                         f"<td class=num>{_e(v.get('il_db'))}<td class=num>{_e(v.get('seconds'))}</tr>")
        parts.append("</table></div><ul>")
        for a in d.get("actions", []):
            parts.append(f"<li><b>{_e(a.get('solver', 'all'))}: {_e(a['cause'])}</b> ({_e(a['confidence'])}). {_e(a['why'])}. Try: {_e(a['try'])}.</li>")
        if d.get("loss_note"):
            parts.append(f"<li>{_e(d['loss_note'])}</li>")
        parts.append("</ul>")

    casc = latest.get("cascade")
    if casc and casc.metrics.get("by_freq"):
        parts.append("<h2>Chain</h2><div class=table><table><tr><th>MHz<th>Gain, dB<th>NF, dB<th>OIP3, dBm</tr>")
        for f, v in casc.metrics["by_freq"].items():
            parts.append(f"<tr><td class=num>{_e(f)}<td class=num>{_e(v['gain_db'])}<td class=num>{_e(v['nf_db'])}<td class=num>{_e(v['oip3_dbm']) if v['oip3_dbm'] is not None else 'not given'}</tr>")
        parts.append("</table></div>")

    notes, waived = [], []
    for name in (r["check"] for r in rows):
        ev = latest.get(name)
        for f in (ev.findings if ev else []):
            (waived if f["waived_by"] else notes).append((name, f))
    if notes:
        parts.append("<h2>Open findings</h2><ul>" + "".join(
            f"<li><span class={'fail' if f['severity'] == 'error' else 'warn'}>{_e(f['severity'])}</span> {_e(n)}: {_e(f['message'])}</li>"
            for n, f in notes[:60]) + "</ul>")
    if waived:
        parts.append("<h2>Waived</h2><ul>" + "".join(
            f"<li>{_e(f['message'])}. Waived by {_e(f['waived_by'])}: {_e(f['waiver_reason'])}</li>" for _, f in waived) + "</ul>")
    parts.append("</main>")
    out.write_text("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" + "".join(parts))
    return out
