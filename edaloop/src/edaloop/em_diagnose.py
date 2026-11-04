"""Turn solver disagreement into a next step.

Two solvers can only say that something is wrong. Three can say which one, and
the way an outlier is wrong usually names the setup fault. An agent gets a
hypothesis and an action to try, not just a failed comparison.

The signatures come from runs on this machine's boards: Palace Draft read 36.2
ohm with eps_eff 3.71 on a line the others put at 49 ohm and 3.3, and thin-sheet
planar MoM read 8-10% high with eps_eff unchanged.
"""
from __future__ import annotations

import statistics

ZC_CLOSE_PCT = 1.0
EPS_CLOSE_PCT = 1.5
LOSS_CLOSE_DB = 0.05


def _pct(value: float, reference: float) -> float:
    return 100 * (value - reference) / reference


def majority(results: dict, max_spread_pct: float) -> tuple[list[str], list[str]]:
    """The largest group of results agreeing within the limit, and everything outside it."""
    labels = sorted(results, key=lambda k: results[k]["zc_ohm"])
    best: list[str] = []
    for i, low in enumerate(labels):
        group = [low]
        for other in labels[i + 1:]:
            mean = statistics.mean(results[x]["zc_ohm"] for x in [*group, other])
            if 100 * (results[other]["zc_ohm"] - results[low]["zc_ohm"]) / mean <= max_spread_pct:
                group.append(other)
        if len(group) > len(best):
            best = group
    return best, [x for x in labels if x not in best]


def signature(label: str, zc_err_pct: float, eps_err_pct: float) -> dict:
    """What an outlier's error pattern most likely means, and what to try."""
    zc_off = abs(zc_err_pct) > ZC_CLOSE_PCT
    eps_off = abs(eps_err_pct) > EPS_CLOSE_PCT
    if zc_err_pct < 0 and eps_err_pct > 0 and zc_off and eps_off:
        return {"cause": "under-resolved mesh around the trace", "confidence": "observed",
                "why": "Zc low with eps_eff high reads as excess capacitance, as a coarse mesh gives",
                "try": f"raise {label}'s quality or refine at the trace edges, then rerun that solver only"}
    if zc_off and not eps_off and zc_err_pct > 0:
        return {"cause": "conductor model differs, such as thin-sheet metal", "confidence": "observed",
                "why": "propagation agrees but impedance reads high, so the field near the metal differs",
                "try": f"check that {label} models copper thickness; a fine line on thin dielectric is sensitive to it"}
    if zc_off and not eps_off:
        return {"cause": "port or reference-plane setup", "confidence": "hypothesis",
                "why": "the line propagates correctly but the terminal impedance differs",
                "try": f"check {label}'s port type, its ground reference, and that the coupon is long enough"}
    if eps_off and not zc_off:
        return {"cause": "dielectric or air-box setup", "confidence": "hypothesis",
                "why": "impedance agrees but phase velocity does not",
                "try": f"check the materials {label} actually received, then its air-box padding"}
    return {"cause": "unclassified disagreement", "confidence": "hypothesis",
            "why": "both Zc and eps_eff differ, and not in the coarse-mesh pattern",
            "try": f"read {label}'s run log and render its model before changing anything"}


def diagnose(results: dict, target_ohm: float, tol_pct: float, max_spread_pct: float) -> dict:
    """Classify a set of solver results. results: label -> zc_ohm, eps_eff, il_db and solver."""
    agree, out = majority(results, max_spread_pct)
    distinct = {results[x].get("solver", x) for x in results}
    actions: list[dict] = []
    if out and len(agree) >= 2 and len(agree) > len(out):
        verdict = "outlier"
        zc_ref = statistics.mean(results[x]["zc_ohm"] for x in agree)
        eps_ref = statistics.mean(results[x]["eps_eff"] for x in agree)
        for label in out:
            zc_err = _pct(results[label]["zc_ohm"], zc_ref)
            eps_err = _pct(results[label]["eps_eff"], eps_ref)
            actions.append({"solver": label, "zc_err_pct": round(zc_err, 1), "eps_err_pct": round(eps_err, 1),
                            **signature(label, zc_err, eps_err)})
    elif out:
        verdict = "no_majority"
        actions.append({"cause": "no two results support each other", "confidence": "none",
                        "why": f"{len(results)} result(s) and no agreeing majority, so the wrong one cannot be named",
                        "try": "add another solver, or refine each one and watch which moves" })
    else:
        verdict = "agree"
    trusted = agree if agree else list(results)
    zc = statistics.mean(results[x]["zc_ohm"] for x in trusted)
    if verdict == "agree" and abs(zc - target_ohm) > target_ohm * tol_pct / 100:
        verdict = "design_off_target"
        actions.append({"cause": "the design, not the setup", "confidence": "high",
                        "why": f"the solvers agree on {zc:.1f} ohm, and the target is {target_ohm}",
                        "try": "change the trace width or gap. Do not change the solver setup or the tolerance"})
    # Loss is reported apart from Zc: a difference here can be a modelling choice, not a fault.
    losses = {k: results[k]["il_db"] for k in trusted if results[k].get("il_db") is not None}
    loss_note = None
    if len(losses) >= 2 and max(losses.values()) - min(losses.values()) > LOSS_CLOSE_DB:
        loss_note = (f"insertion loss differs by {max(losses.values()) - min(losses.values()):.2f} dB while Zc agrees: "
                     f"{losses}. Check whether each solver models metal and dielectric loss. "
                     "openEMS on this setup treats metal as lossless")
    return {"verdict": verdict, "agreeing": agree, "outliers": out, "distinct_solvers": len(distinct),
            "actions": actions, "loss_note": loss_note, "zc_trusted_ohm": round(zc, 2)}
