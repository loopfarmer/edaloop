"""What should happen next. This is the part an agent actually loops on."""
from __future__ import annotations

from .checks import REGISTRY, _lookup, gate_state, requirement_message
from .ledger import Evidence
from .project import Project

# Who may change the design to clear a failure. Checks measure; these act.
REMEDY = {
    "project_files": "Copy the .kicad_pro and library tables next to the board.",
    "capture": "Fix the .ato source (atopile skills), then rerun capture.",
    "erc": "Edit the schematic with the KiCad MCP or the ato source.",
    "drc": "Move copper with the KiCad MCP, or run `edaloop route`. Waive only with a named reviewer and reason.",
    "rf_path": "off_axis: change placement, since re-routing can't fix it. vias or over_floor: relay that net and lock it.",
    "rf_preserved": "RF copper differs from the recorded baseline, whether a router or a hand edit changed it. Restore it, or discard the router result. If the change is meant, tell a person what it does and check it with impedance and em_hops.",
    "impedance": "A short low section named as a part's land is that pad, and usually a review note. Otherwise change width or gap at the reported location. Check against rf-board-design-lessons.",
    "rf_reference": "Move the named net off the reference layer under the RF line, to another layer or round the line by more than reference_margin_mm. Don't move the RF line to dodge it.",
    "rf_layout": "Each finding names a rule in docs/rf-layout-rules.md. Fence gaps and holes: rerun `edaloop ground`, and if a stretch stays open, move what blocks its vias. Placement findings, such as pad gaps, taps and bypass order: change placement, then re-lay the RF.",
    "power": "Widen the named segment, add a parallel path or a pour, or move the load. A short neck between pads: read thermal_field before widening it. No rails described: a person states each rail's source and load currents under power.rails.",
    "thermal_field": "Hotspot or drop: add copper or vias where it names. Not converged or an energy error: refine dx_mm and rerun, since a bad solve is no reading. A missing supply_v or max_part_rise_c is a person's figure to state, not an agent's to choose.",
    "em_fast": "A trend reading only. Change width or gap and rerun, then confirm with em_crosscheck before believing a pass or a failure.",
    "em_hops": "Read the worst hop's return loss against its geometry: a pad wider than the line, a via or a tap stub. Change the land or the line, then rerun. Don't change the band or the solver to pass.",
    "spice": "A part with no model: add one to the registry with its provenance, or have a person state an exclusion. A measurement that didn't resolve: fix the bench's probe or the circuit, not the limit.",
    "rules": "Change via or track sizes on the board, or pick the fabricator profile that was actually agreed.",
    "em_crosscheck": "Follow metrics.diagnosis: fix the named outlier solver and rerun, or change the cross-section if the verdict is design_off_target.",
    "cascade": "Change the stage table only with a datasheet or measurement behind it.",
    "requirements": "Clear the stale or missed checks listed. A requirement changes only when a person changes it.",
}


def status(p: Project) -> dict:
    rows = gate_state(p)
    ready = all(r["state"] in ("pass", "warn") for r in rows)
    return {"project": p.name, "ready_to_release": ready, "gate": rows}


def next_action(p: Project) -> dict:
    """The first gated check that does not hold, in gate order, and what to do about it."""
    latest = p.ledger.latest()
    for row in gate_state(p):
        name, state = row["check"], row["state"]
        if state in ("empty", "unknown"):
            return {"action": "ask_person", "check": name, "question": row["summary"],
                    "do_not": "An agent mustn't choose what the gate holds."}
        if state in ("missing", "stale"):
            return {"action": "run_check", "check": name, "reason": f"{name} evidence is {state}",
                    "command": f"edaloop run {name}"}
        if state in ("fail", "error"):
            ev: Evidence = latest[name]
            live = [f for f in ev.findings if not f["waived_by"]][:10]
            step = {"action": "fix", "check": name, "state": state, "summary": ev.summary,
                    "findings": live, "remedy": REMEDY.get(name, ""), "then": f"edaloop run {name}"}
            if "diagnosis" in ev.metrics:
                step["diagnosis"] = ev.metrics["diagnosis"]
                decision = _needs_person(p, name, ev, live)
                if decision:
                    return decision
            return step
    return {"action": "release", "command": "edaloop release", "reason": "every gated check holds on these bytes"}


def _needs_person(p: Project, name: str, ev: Evidence, live: list) -> dict | None:
    """When the reference backs the agreeing solvers, the design is fine by every other method, and all that's left is a waiver.

    An agent can't grant one, so this hands the decision over instead of sending it round the same check again.
    """
    diagnosis = ev.metrics["diagnosis"]
    reference = ev.metrics.get("reference") or {}
    if diagnosis.get("verdict") != "outlier" or not reference.get("backs"):
        return None
    if set(reference["backs"]) != set(diagnosis.get("agreeing") or []):
        return None
    outliers = ", ".join(diagnosis["outliers"])
    # Each one is bound to the reading in front of the person, so a different reading later comes back to them.
    waivers = [{"match": f["id"], "message": f["message"], "by": "YOUR NAME", "reason": "YOUR REASON"} for f in live]
    limit = (p.section("em") or {}).get("max_spread_pct", 2)
    # The requirements this check's own readings miss would still hold the release, so they're offered too, quoted the same way.
    for req in (getattr(p, "cfg", None) or {}).get("requirements") or []:
        if str(req.get("metric", "")).startswith(f"{name}."):
            value = _lookup(ev.metrics, req["metric"].split(".", 1)[1])
            low, high = req.get("min"), req.get("max")
            if value is not None and not ((low is None or value >= low) and (high is None or value <= high)):
                waivers.append({"match": f"requirements:{req['id']}", "message": requirement_message(req, value),
                                "by": "YOUR NAME", "reason": "YOUR REASON"})
    return {"action": "ask_person", "check": name,
            "question": f"{outliers} disagrees with {', '.join(diagnosis['agreeing'])} by {ev.metrics['spread_pct']}%, past the {limit}% limit. "
                        f"The 2D reference reads {reference['z0_ohm']} ohm, which backs the others. Waive it, or hold the release?",
            "findings": live, "diagnosis": diagnosis, "if_waived": {"add_to_edaloop_yaml": {"waivers": waivers}},
            "see": "docs/openems-coupon-bias.md", "do_not": "An agent mustn't add this waiver itself."}


def run_all(p: Project, stop_on_failure: bool = False, skip: tuple = (), progress=None) -> list[dict]:
    """Run missing or stale gated checks in order. Results already fresh are not recomputed. `progress` is told each check's name before it runs."""
    from .checks import run_check
    done = []
    for row in gate_state(p):
        if row["state"] in ("pass", "warn") or row["check"] in skip or row["check"] not in REGISTRY:
            continue
        if progress:
            progress(row["check"])
        ev = run_check(p, row["check"])
        done.append({"check": ev.check, "status": ev.status, "summary": ev.summary, "seconds": ev.seconds})
        if stop_on_failure and ev.status in ("fail", "error"):
            break
    return done


def describe_checks() -> dict:
    return {k: {"stage": v["stage"], "doc": v["doc"]} for k, v in REGISTRY.items()}
