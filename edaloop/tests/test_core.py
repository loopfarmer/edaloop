"""Tests for what the gate promises. No external EDA tool is needed."""
import json
import math
import os
import re
import shutil
from pathlib import Path

import pytest

from edaloop import checks, controller, parts_api, power, toolchain
from edaloop.em_diagnose import diagnose
from edaloop.ledger import Evidence, sha_file
from edaloop.pcbfile import Board, compare_copper, parse_sexpr
from edaloop.project import Project, ProjectError
from edaloop.route import ORTHOROUTE_MIN_NETS, classify
from edaloop.scaffold import hook_message, init

PCB = """(kicad_pcb (version 20241229)
 (setup (stackup (layer "F.Cu" (type "copper")) (layer "In1.Cu" (type "copper"))))
 (footprint "x" (pad "1" smd rect (net "RF")) (pad "2" smd rect (net "GND")))
 (segment (start 0 0) (end 10 0) (width 0.36) (locked yes) (layer "F.Cu") (net "RF"))
 (segment (start 10 0) (end 10 5) (width 0.36) (locked yes) (layer "F.Cu") (net "RF"))
 (via (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND")))
"""


def make(tmp_path, extra=""):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    (tmp_path / "edaloop.yaml").write_text("name: t\nboard: b.kicad_pcb\ngate: [project_files]\n" + extra)
    return Project(tmp_path)


def test_sexpr_handles_quotes_and_escapes():
    assert parse_sexpr('(a "b \\" c" (d 1))') == ["a", 'b " c', ["d", "1"]]


def test_board_reads_copper(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    b = Board(tmp_path / "b.kicad_pcb")
    assert b.stats()["segments"] == 2 and b.stats()["vias"] == 1 and b.stats()["track_mm"] == 15.0
    assert b.net_names() == {"RF", "GND"}


def test_right_angle_found_and_straight_line_is_not(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    assert len(Board(tmp_path / "b.kicad_pcb").right_angles(["RF"])) == 1
    (tmp_path / "b.kicad_pcb").write_text(PCB.replace("(end 10 5)", "(end 20 0)"))
    assert Board(tmp_path / "b.kicad_pcb").right_angles(["RF"]) == []


def test_compare_copper_separates_moved_from_added(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    before = Board(tmp_path / "b.kicad_pcb").copper_items(["RF"])
    (tmp_path / "b.kicad_pcb").write_text(PCB.replace("(end 10 5)", "(end 10 6)"))
    moved = compare_copper(before, Board(tmp_path / "b.kicad_pcb").copper_items(["RF"]))
    assert moved["lost"] == {"RF": 1}
    jog = '(segment (start 10 5) (end 10.0005 5) (width 0.2) (layer "F.Cu") (net "RF"))\n)'
    (tmp_path / "b.kicad_pcb").write_text(PCB.rstrip()[:-1] + jog)
    added = compare_copper(before, Board(tmp_path / "b.kicad_pcb").copper_items(["RF"]))
    assert added["lost"] == {} and added["added_mm"] == pytest.approx(0.0005)


def test_losing_ground_vias_fails_rf_preserved(tmp_path):
    chains = 'chains:\n  - {net: RF, entry: J1.1, exit: J2.1}\n'
    (tmp_path / "c.yaml").write_text(chains)
    p = make(tmp_path, "rf: {chains: c.yaml}\n")
    assert checks.run_check(p, "rf_preserved").status == "pass"
    (tmp_path / "b.kicad_pcb").write_text(PCB.replace('(via (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))', ""))
    ev = checks.run_check(p, "rf_preserved")
    assert ev.status == "fail" and "ground_vias" in ev.findings[0]["id"]


def test_missing_kicad_pro_fails_rather_than_using_default_rules(tmp_path):
    p = make(tmp_path)
    ev = checks.run_check(p, "project_files")
    assert ev.status == "fail" and any("no_kicad_pro" in f["id"] for f in ev.findings)


def test_saving_the_board_makes_evidence_stale(tmp_path):
    p = make(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "fp-lib-table").write_text("")
    assert checks.run_check(p, "project_files").status == "pass"
    assert controller.status(p)["ready_to_release"]
    with open(tmp_path / "b.kicad_pcb", "a") as f:
        f.write("\n")
    assert controller.status(p)["gate"][0]["state"] == "stale"
    assert controller.next_action(p)["action"] == "run_check"


def test_release_refused_while_gate_is_open(tmp_path):
    p = make(tmp_path)
    result = checks.release(p)
    assert not result["released"] and result["blockers"][0]["state"] == "missing"
    assert not (tmp_path / "release").exists()


def test_waiver_needs_name_and_reason(tmp_path):
    p = make(tmp_path, "waivers:\n  - {match: 'project_files:*', reason: because}\n")
    with pytest.raises(ProjectError):
        p.waivers()


def test_waived_finding_passes_but_stays_on_record(tmp_path):
    p = make(tmp_path, "waivers:\n  - {match: 'project_files:*', by: Pat, reason: reviewed}\n")
    ev = checks.run_check(p, "project_files")
    assert ev.status == "pass"
    assert ev.findings and all(f["waived_by"] == "Pat" for f in ev.findings)


def test_tool_crash_is_an_error_never_a_pass(tmp_path):
    p = make(tmp_path)
    ev = checks.run_check(p, "cascade")          # no cascade section, so it raises
    assert ev.status == "error"
    assert not controller.status(p)["ready_to_release"] or "cascade" not in p.gate()


def test_em_refuses_draft_and_single_solver(tmp_path):
    p = make(tmp_path, "em: {solvers: [{solver: palace, quality: Draft}, {solver: openems}]}\n")
    assert "refused" in checks.run_check(p, "em_crosscheck").summary
    q = make(tmp_path, "em: {solvers: [{solver: openems}, {solver: openems, cells_per_wavelength: 60}]}\n")
    assert "two different" in checks.run_check(q, "em_crosscheck").summary


def test_friis_matches_hand_calculation():
    stages = [{"loss_db": [[1, 1.0]]},
              {"gain_db": [[1, 20.0]], "nf_db": [[1, 1.0]], "oip3_dbm": [[1, 30.0]]},
              {"loss_db": [[1, 3.0]]}]
    r = checks.friis(stages, 1000)
    assert r["gain_db"] == pytest.approx(16.0, abs=0.01)
    assert r["nf_db"] == pytest.approx(2.03, abs=0.02)
    assert r["oip3_dbm"] == pytest.approx(27.0, abs=0.05)


def test_short_dip_rl_falls_with_length_and_vanishes_at_zero_mismatch():
    assert checks.section_rl_db(28.9, 512.5, 1.25e9, 3.25, 50) > checks.section_rl_db(28.9, 5000, 1.25e9, 3.25, 50)
    assert checks.section_rl_db(50.0, 512.5, 1.25e9, 3.25, 50) > 100


def test_requirement_on_stale_evidence_fails(tmp_path):
    p = make(tmp_path, "requirements:\n  - {id: R1, metric: project_files.x, max: 1}\n")
    p.ledger.append(Evidence("project_files", "pass", "", {"b.kicad_pcb": "0" * 64}, {"x": 0}))
    ev = checks.run_check(p, "requirements")
    assert ev.status == "fail" and ev.metrics["rows"][0]["state"] == "stale"
    p.ledger.append(Evidence("project_files", "pass", "", {"b.kicad_pcb": sha_file(tmp_path / "b.kicad_pcb")}, {"x": 0}))
    ev = checks.run_check(p, "requirements")
    assert ev.status == "pass" and ev.metrics["rows"][0]["state"] == "met"


def test_orthoroute_excluded_from_ordinary_boards(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    v = classify(Board(tmp_path / "b.kicad_pcb"))
    assert v["allowed"] == ["freerouting"] and "excluded" in v["reasons"]["orthoroute"] and ORTHOROUTE_MIN_NETS > 2


def _r(zc, eps, il=0.1, solver="x"):
    return {"zc_ohm": zc, "eps_eff": eps, "il_db": il, "solver": solver}


def test_three_solvers_name_the_coarse_palace_run_from_the_recorded_numbers():
    d = diagnose({"openems": _r(49.0, 3.25, solver="openems"), "emerge": _r(49.3, 3.28, solver="emerge"),
                  "palace_draft": _r(36.2, 3.71, solver="palace")}, 50, 10, 2)
    assert d["verdict"] == "outlier" and d["outliers"] == ["palace_draft"]
    assert d["actions"][0]["cause"] == "under-resolved mesh around the trace"
    assert d["zc_trusted_ohm"] == pytest.approx(49.15)


def test_two_disagreeing_solvers_cannot_name_a_culprit():
    d = diagnose({"a": _r(49.0, 3.25), "b": _r(36.2, 3.71)}, 50, 10, 2)
    assert d["verdict"] == "no_majority" and d["outliers"]


def test_thin_sheet_signature():
    d = diagnose({"a": _r(49.0, 3.25), "b": _r(49.2, 3.26), "planar": _r(53.7, 3.24)}, 50, 10, 2)
    assert d["outliers"] == ["planar"] and "conductor model" in d["actions"][0]["cause"]


def test_agreement_away_from_target_blames_the_design():
    d = diagnose({"a": _r(62.0, 3.2), "b": _r(62.3, 3.2)}, 50, 10, 2)
    assert d["verdict"] == "design_off_target"


def test_loss_difference_is_a_note_not_a_disagreement():
    d = diagnose({"a": _r(49.0, 3.25, il=0.08), "b": _r(49.1, 3.26, il=0.23)}, 50, 10, 2)
    assert d["verdict"] == "agree" and "insertion loss" in d["loss_note"]


def test_init_never_overwrites_and_hook_reports_staleness(tmp_path):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    (tmp_path / "CLAUDE.md").write_text("mine")
    result = init(tmp_path)
    assert "CLAUDE.md" in result["left_alone"] and (tmp_path / "CLAUDE.md").read_text() == "mine"
    assert "edaloop.yaml" in result["wrote"]
    p = Project(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    checks.run_check(p, "project_files")
    assert hook_message(tmp_path, {"tool_name": "mcp__kicad__add_track", "tool_input": {}}) is None
    with open(tmp_path / "b.kicad_pcb", "a") as f:
        f.write("\n")
    msg = hook_message(tmp_path, {"tool_name": "mcp__kicad__add_track", "tool_input": {}})
    assert "project_files" in msg["hookSpecificOutput"]["additionalContext"]
    assert hook_message(tmp_path, {"tool_name": "Read", "tool_input": {"file_path": "x.txt"}}) is None


def test_mcp_server_module_imports_and_registers_its_tools():
    import asyncio

    from edaloop import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"next_action", "gate_status", "run_check", "route_board", "adopt_route", "release"} <= names
    assert not any("waive" in n or "requirement" in n for n in names)


# ---------------------------------------------------------------- the MCP interface, called the way an agent calls it

MCP_TOOLS = {"next_action", "gate_status", "run_check", "list_checks", "route_board", "adopt_route", "make_board",
             "describe_design", "make_mechanical", "review_pack", "release", "doctor"}


def _mcp(name, args=None):
    """Call a tool through the MCP server, and return what the agent would read. A refusal raises ToolError."""
    import asyncio

    from edaloop import mcp_server
    result = asyncio.run(mcp_server.mcp.call_tool(name, args or {}))
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def _mcp_refused(name, args):
    """Call a tool that must refuse: a deliberate ToolError, not a crash, with the message returned."""
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    from edaloop import mcp_server
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp_server.mcp.call_tool(name, args))
    assert not isinstance(e.value, UnexpectedToolError), e.value.__cause__
    return str(e.value)


def _gate(tmp_path):
    return {r["check"]: r["state"] for r in _mcp("gate_status", {"project_dir": str(tmp_path)})["gate"]}


def test_mcp_tool_list_is_exactly_the_reviewed_set_and_none_edits_requirements_or_waivers():
    import asyncio

    from edaloop import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    # A new tool must be added here on purpose, after checking it can't touch requirements or waivers.
    assert {t.name for t in tools} == MCP_TOOLS
    for t in tools:
        params = " ".join(t.input_schema.get("properties", {})).lower()
        assert "waive" not in params and "requirement" not in params and "gate" not in params, t.name


def test_mcp_gate_loop_end_to_end_goes_stale_when_the_board_changes(tmp_path):
    make(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    d = str(tmp_path)
    assert _gate(tmp_path) == {"project_files": "missing"}
    step = _mcp("next_action", {"project_dir": d})
    assert step["action"] == "run_check" and step["check"] == "project_files"
    ev = _mcp("run_check", {"check": "project_files", "project_dir": d})
    assert ev["check"] == "project_files" and ev["status"] in ("pass", "warn", "fail", "error")
    assert _gate(tmp_path) == {"project_files": ev["status"]}
    if ev["status"] in ("pass", "warn"):
        assert _mcp("next_action", {"project_dir": d})["action"] == "release"
    with open(tmp_path / "b.kicad_pcb", "a") as f:
        f.write("\n")
    assert _gate(tmp_path) == {"project_files": "stale"}
    step = _mcp("next_action", {"project_dir": d})
    assert step["action"] == "run_check" and step["check"] == "project_files"


def test_mcp_run_check_refuses_an_unknown_name_and_records_nothing(tmp_path):
    make(tmp_path)
    ledger = tmp_path / ".edaloop" / "ledger.jsonl"
    before = ledger.read_bytes() if ledger.exists() else None
    msg = _mcp_refused("run_check", {"check": "drc_typo", "project_dir": str(tmp_path)})
    assert "drc_typo" in msg and "project_files" in msg and "drc" in msg      # lists what there is
    assert (ledger.read_bytes() if ledger.exists() else None) == before
    assert _gate(tmp_path) == {"project_files": "missing"}
    with pytest.raises(checks.UnknownCheck, match="Available checks"):
        checks.run_check(Project(tmp_path), "drc_typo")


def test_cli_run_refuses_an_unknown_check_with_the_list_and_a_nonzero_exit(tmp_path, capsys):
    from edaloop.cli import main
    make(tmp_path)
    assert main(["-C", str(tmp_path), "run", "drc_typo"]) == 2
    err = capsys.readouterr().err
    assert "drc_typo" in err and "project_files" in err
    assert not (tmp_path / ".edaloop" / "ledger.jsonl").exists() or "drc_typo" not in (tmp_path / ".edaloop" / "ledger.jsonl").read_text()


def test_cli_stage_refuses_a_misspelt_check_before_running_any(tmp_path, capsys):
    from edaloop.cli import main
    make(tmp_path, "stages: {s: [project_files, drc_typo]}\n")
    assert main(["-C", str(tmp_path), "stage", "s"]) == 2
    assert "drc_typo" in capsys.readouterr().err
    assert not (tmp_path / ".edaloop" / "ledger.jsonl").exists()


def test_mcp_release_is_refused_while_the_gate_is_open(tmp_path):
    make(tmp_path)
    result = _mcp("release", {"project_dir": str(tmp_path)})
    assert result["released"] is False and result["blockers"][0]["check"] == "project_files"
    assert not (tmp_path / "release").exists() and not list(tmp_path.glob("*_release.zip"))


def test_mcp_release_with_no_exporter_is_an_error_and_leaves_no_release_folder(tmp_path, monkeypatch):
    make(tmp_path)
    p = Project(tmp_path)
    monkeypatch.setattr(checks, "gate_state", lambda p: [{"check": "project_files", "state": "pass"}])

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", str(cmd[0]))
    monkeypatch.setattr(toolchain, "run", missing)
    result = _mcp("release", {"project_dir": str(p.root)})
    assert result["released"] is False and result["blockers"][0]["state"] == "error"
    assert not (tmp_path / "release").exists() and not list(tmp_path.glob("*_release.zip"))


@pytest.fixture
def confined(tmp_path, monkeypatch):
    """A project the MCP server was started for, and a folder beside it that the server mustn't write into."""
    proj, outside = tmp_path / "proj", tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    make(proj)
    (proj / "sub").mkdir()
    (proj / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("EDALOOP_PROJECT", str(proj))
    monkeypatch.chdir(tmp_path)
    return proj, outside


def _tree(path):
    return sorted((str(f.relative_to(path)), f.read_bytes() if f.is_file() else None) for f in path.rglob("*"))


@pytest.mark.parametrize("where", ["outside", "dotdot", "symlink", "absolute_symlink", "edaloop_yaml", "ledger"])
def test_mcp_make_board_writes_only_inside_the_project(confined, where):
    from edaloop import testbench
    proj, outside = confined
    target = {"outside": str(outside / "new"), "dotdot": "sub/../../outside/new", "symlink": "link/new",
              "absolute_symlink": str(proj / "link" / "new"), "edaloop_yaml": "edaloop.yaml",
              "ledger": ".edaloop/new"}[where]
    before_proj, before_out = _tree(proj), _tree(outside)
    msg = _mcp_refused("make_board", {"description_file": str(testbench.BENCHES / "gain_block_demo.yaml"), "output_dir": target})
    assert "refused" in msg
    assert _tree(proj) == before_proj and _tree(outside) == before_out


def test_mcp_make_board_refuses_the_project_folder_and_a_full_folder(confined):
    from edaloop import testbench
    proj, _ = confined
    bench = str(testbench.BENCHES / "gain_block_demo.yaml")
    assert "project folder itself" in _mcp_refused("make_board", {"description_file": bench, "output_dir": "."})
    assert "project folder itself" in _mcp_refused("make_board", {"description_file": bench, "output_dir": str(proj / "sub" / "..")})
    assert "existing file" in _mcp_refused("make_board", {"description_file": bench, "output_dir": "b.kicad_pcb"})
    (proj / "sub" / "routed.kicad_pcb").write_text("x")
    assert "isn't empty" in _mcp_refused("make_board", {"description_file": bench, "output_dir": "sub"})
    assert (proj / "sub" / "routed.kicad_pcb").read_text() == "x"


@pytest.mark.parametrize("where", ["outside", "dotdot", "symlink", "absolute_symlink", "edaloop_yaml",
                                   "edaloop_yaml_dotdot", "link_to_edaloop_yaml", "ledger", "existing", "not_yaml"])
def test_mcp_describe_design_writes_only_a_new_description_inside_the_project(confined, where):
    proj, outside = confined
    (proj / "desc.yaml").symlink_to(proj / "edaloop.yaml")
    (proj / "mine.yaml").write_text("filled in by a person\n")
    target = {"outside": str(outside / "d.yaml"), "dotdot": "../outside/d.yaml", "symlink": "link/d.yaml",
              "absolute_symlink": str(proj / "link" / "d.yaml"), "edaloop_yaml": "edaloop.yaml",
              "edaloop_yaml_dotdot": "sub/../edaloop.yaml", "link_to_edaloop_yaml": "desc.yaml",
              "ledger": ".edaloop/d.yaml", "existing": "mine.yaml", "not_yaml": "b.kicad_pro"}[where]
    before_proj, before_out = _tree(proj), _tree(outside)
    msg = _mcp_refused("describe_design", {"source_file": str(proj / "b.kicad_pcb"), "output_file": target, "band_mhz": [900, 1500]})
    assert "refused" in msg
    assert _tree(proj) == before_proj and _tree(outside) == before_out


def _describable_board(path):
    def fp(ref, value, pads):
        body = "".join(f'(pad "{n}" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {i}))' for n, i in pads)
        return f'(footprint "Lib:{ref}" (at 0 0) (property "Reference" "{ref}") (property "Value" "{value}") {body})'
    path.write_text('(kicad_pcb (version 20241229) (net 0 "") (net 1 "A") (net 2 "B") (net 3 "GND") '
                    + fp("J1", "in", [("1", 1), ("2", 3)]) + fp("U1", "LNA", [("1", 1), ("2", 2), ("3", 3)])
                    + fp("J2", "out", [("1", 2), ("2", 3)]) + ')')


def test_mcp_describe_design_writes_a_new_file_inside_the_project(confined):
    proj, _ = confined
    _describable_board(proj / "src.kicad_pcb")
    result = _mcp("describe_design", {"source_file": str(proj / "src.kicad_pcb"),
                                      "output_file": "sub/desc.yaml", "band_mhz": [900, 1500]})
    assert Path(result["written"]) == (proj / "sub" / "desc.yaml").resolve() and (proj / "sub" / "desc.yaml").exists()


def test_describe_never_writes_edaloop_yaml_from_the_cli_either(tmp_path):
    from edaloop.cli import main
    make(tmp_path)
    _describable_board(tmp_path / "src.kicad_pcb")
    before = (tmp_path / "edaloop.yaml").read_bytes()
    assert main(["describe", str(tmp_path / "src.kicad_pcb"), "-o", str(tmp_path / "edaloop.yaml"), "--band-mhz", "900", "1500"]) == 1
    assert (tmp_path / "edaloop.yaml").read_bytes() == before


def test_no_safe_mcp_tool_changes_edaloop_yaml(confined):
    from edaloop import testbench
    proj, _ = confined
    (proj / "b.kicad_pro").write_text("{}")
    _describable_board(proj / "src.kicad_pcb")
    before = (proj / "edaloop.yaml").read_bytes()
    d = str(proj)
    _mcp("list_checks")
    _mcp("next_action", {"project_dir": d})
    _mcp("gate_status", {"project_dir": d})
    _mcp("run_check", {"check": "project_files", "project_dir": d})
    _mcp("run_check", {"check": "requirements", "project_dir": d})
    _mcp("release", {"project_dir": d})
    _mcp_refused("run_check", {"check": "waivers", "project_dir": d})
    _mcp_refused("describe_design", {"source_file": str(proj / "src.kicad_pcb"), "output_file": "edaloop.yaml", "band_mhz": [900, 1500]})
    _mcp_refused("make_board", {"description_file": str(testbench.BENCHES / "gain_block_demo.yaml"), "output_dir": "edaloop.yaml"})
    _mcp("describe_design", {"source_file": str(proj / "src.kicad_pcb"), "output_file": "new.yaml", "band_mhz": [900, 1500]})
    assert (proj / "edaloop.yaml").read_bytes() == before


def test_ipc2221_matches_published_shape_and_round_trips():
    rise = power.ipc2221_rise_c(1.0, 0.3, 35, True)
    assert 10 < rise < 40                 # 1 A on 12 mil 1 oz outer is known to be warm
    assert power.ipc2221_rise_c(1.0, 0.3, 35, False) > rise     # inner layers run hotter
    width = power.ipc2221_min_width_mm(1.0, rise, 35, True)
    assert width == pytest.approx(0.3, rel=1e-6)


def test_power_solver_on_a_hand_calculated_rail(tmp_path):
    from edaloop.pcbfile import Track
    t = [Track("segment", "V", "F.Cu", (0, 0), (10, 0), 0.25, 0, False)]
    pads = {"S.1": (0, 0, 0.1, 0.1), "L.1": (10, 0, 0.1, 0.1)}
    r = power.solve_rail("V", t, pads, "S.1", {"L.1": 2.0}, thickness_um={"F.Cu": 35.0}, board_height_mm=1.6)
    expected_mv = 2.0 * (1.72e-5 * 10 / (0.25 * 0.035)) * 1000
    assert r.worst_drop_mv == pytest.approx(expected_mv, rel=1e-3)
    assert r.worst_rise_c == pytest.approx(power.ipc2221_rise_c(2.0, 0.25, 35, True), rel=1e-2)
    assert r.worst_segment["current_a"] == pytest.approx(2.0, rel=1e-3)


def test_power_solver_splits_tee_and_reports_unreached_load():
    from edaloop.pcbfile import Track
    t = [Track("segment", "V", "F.Cu", (0, 0), (10, 0), 0.5, 0, False),
         Track("segment", "V", "F.Cu", (5, 0), (5, 5), 0.5, 0, False)]
    pads = {"S.1": (0, 0, 0.1, 0.1), "A.1": (5, 5, 0.1, 0.1), "B.1": (10, 0, 0.1, 0.1), "X.1": (50, 50, 0.1, 0.1)}
    r = power.solve_rail("V", t, pads, "S.1", {"A.1": 1.0, "B.1": 1.0}, thickness_um={}, board_height_mm=1.6)
    assert r.connected and r.drops_mv["A.1"] > 0 and r.drops_mv["B.1"] > 0
    r2 = power.solve_rail("V", t, pads, "S.1", {"X.1": 1.0}, thickness_um={}, board_height_mm=1.6)
    assert r2.unreached == ["X.1"]


def test_spice_value_parsing_and_missing_model_fails():
    from edaloop import spice as sp
    assert sp.parse_value("100nF blk") == "100n" and sp.parse_value("1k0") == "1.0k" and sp.parse_value("4R7") == "4.7"
    assert sp.parse_value("LNA choke") is None and sp.parse_value("10uH") == "10u" and sp.parse_value("1M") == "1Meg"
    part = sp.Part("U9", "MYSTERY", "MYSTERY", {"1": "A"})
    assert sp.resolve(part, {}, {})["kind"] == "missing"
    with pytest.raises(sp.BenchError):
        sp.resolve(part, {"U9": {"kind": "open"}}, {})


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_ngspice_rc_lowpass_matches_closed_form(tmp_path):
    from edaloop import spice as sp
    deck = "* rc\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 1n\n.save all\n.ac dec 200 1k 10Meg\n.meas ac f3db when vdb(out)=-3.0103\n.end\n"
    run = sp.run_deck(deck, tmp_path, "rc")
    assert run["values"]["f3db"] == pytest.approx(1 / (2 * 3.141592653589793 * 1e3 * 1e-9), rel=0.005)


def test_parts_api_keeps_tolerance_band_inside_the_request():
    db = toolchain.paths()["pcbparts_db"]
    if not os.path.exists(db):
        pytest.skip("no pcbparts database")
    r = parts_api.Catalogue(db).query("resistors", {"resistance": parts_api._quantity(9900, 10100, "ohm"), "qty": 1})
    assert r
    for part in r:
        data = part["attributes"]["resistance"]["data"]["intervals"]["data"]["intervals"][0]["data"]
        assert data["min"] >= 9900 * 0.999 and data["max"] <= 10100 * 1.001
    assert parts_api.Catalogue(db).by_lcsc(25744)[0]["lcsc"] == 25744


def test_power_solver_conducts_through_a_via_pad_that_touches_a_track():
    from edaloop.pcbfile import Track
    t = [Track("segment", "V", "In1.Cu", (0, 0), (5, 0), 0.5, 0, False),
         Track("via", "V", "F.Cu", (5, 0), (5, 0), 0.6, 0.3, False),
         Track("segment", "V", "F.Cu", (2, 0.2), (8, 0.2), 0.2, 0, False)]
    pads = {"S.1": (0, 0, 0.1, 0.1), "L.1": (8, 0.2, 0.1, 0.1)}
    r = power.solve_rail("V", t, pads, "S.1", {"L.1": 1.0}, thickness_um={}, board_height_mm=1.6,
                         pad_layer_map={"S.1": ("In1.Cu",), "L.1": ("F.Cu",)})
    assert r.connected and r.worst_drop_mv > 0


def test_power_solver_keeps_a_narrow_neckdown_visible():
    from edaloop.pcbfile import Track
    wide = Track("segment", "V", "F.Cu", (0, 0), (5, 0), 1.5, 0, False)
    neck = Track("segment", "V", "F.Cu", (5, 0), (10, 0), 0.2, 0, False)
    pads = {"S.1": (0, 0, 0.1, 0.1), "L.1": (10, 0, 0.1, 0.1)}
    r = power.solve_rail("V", [wide, neck], pads, "S.1", {"L.1": 1.5}, thickness_um={"F.Cu": 35.0}, board_height_mm=1.6)
    assert r.worst_segment["width_mm"] == 0.2 and r.worst_rise_c > 30


def test_copper_under_an_rf_line_fails_rf_reference(tmp_path):
    chains = 'chains:\n  - {net: RF, entry: J1.1, exit: J2.1}\n'
    (tmp_path / "c.yaml").write_text(chains)
    p = make(tmp_path, "rf: {chains: c.yaml}\nroute: {reference_layers: [In1.Cu], keep_nets: [GND]}\n")
    assert checks.run_check(p, "rf_reference").status == "pass"
    extra = '(segment (start 5 -5) (end 5 5) (width 0.2) (layer "In1.Cu") (net "VDD"))\n)'
    (tmp_path / "b.kicad_pcb").write_text(PCB.rstrip()[:-1] + extra)
    ev = checks.run_check(p, "rf_reference")
    assert ev.status == "fail" and "VDD" in ev.findings[0]["message"]
    far = '(segment (start 50 50) (end 60 50) (width 0.2) (layer "In1.Cu") (net "VDD"))\n)'
    (tmp_path / "b.kicad_pcb").write_text(PCB.rstrip()[:-1] + far)
    ev = checks.run_check(p, "rf_reference")
    assert ev.status == "pass" and ev.metrics["non_ground_on_reference_mm"] == 10.0


def test_by_class_spine_uses_45_degree_bends_only():
    from edaloop.route_classes import _spine
    pts = _spine((0.0, 0.0), (10.0, 3.0))
    assert pts[0] == (0.0, 0.0) and pts[-1] == (10.0, 3.0)
    for a, b in zip(pts, pts[1:]):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        assert dx < 1e-9 or dy < 1e-9 or abs(dx - dy) < 1e-9
    assert _spine((0.0, 0.0), (10.0, 0.0)) == [(0.0, 0.0), (10.0, 0.0)]


def _qfn_pad(name, net, x, y, fx=30.875, fy=13.5):
    return {"name": name, "net": net, "x": x, "y": y, "hw": 0.4, "hh": 0.125, "fx": fx, "fy": fy, "layers": ["F.Cu"]}


class _NoBoard:
    tracks = []

    def __init__(self, pads):
        self.pads = pads

    def all_pads(self):
        return self.pads


def test_tap_from_a_qfn_pin_leaves_outward_and_never_runs_along_the_pin_row():
    from edaloop.route_classes import Obstacles, _seg_rect, plan_tap
    tap = _qfn_pad("U.14", "SUM", 32.825, 13.0)
    row = [_qfn_pad(f"U.{i}", "OTHER", 32.825, 13.0 + 0.5 * k) for k, i in enumerate((13, 12, 11), start=1)]
    row.append(_qfn_pad("U.15", "OTHER", 32.825, 12.5))
    obstacles = Obstacles(_NoBoard([tap, *row]), "F.Cu", 0.2)
    spine = [(32.825, 19.0), (43.225, 19.0)]
    stub = plan_tap(tap, spine, "SUM", 0.358, [0.3, 0.25, 0.2], obstacles)
    assert stub is not None
    first = stub[0]
    assert first["start"] == (32.825, 13.0) and first["end"][1] == 13.0 and first["end"][0] > 32.825
    assert first["width"] < 0.358                                # necked between the pins
    assert stub[-1]["width"] == 0.358 and abs(stub[-1]["end"][1] - 19.0) < 1e-6
    for seg in stub:
        for pad in row:
            assert _seg_rect(seg["start"], seg["end"], pad) >= 0.2 + seg["width"] / 2
    for a, b in zip(stub, stub[1:]):
        v1 = (a["end"][0] - a["start"][0], a["end"][1] - a["start"][1])
        v2 = (b["end"][0] - b["start"][0], b["end"][1] - b["start"][1])
        assert v1[0] * v2[0] + v1[1] * v2[1] > 0                 # no corner of 90 degrees or sharper


def test_tap_goes_around_a_pad_in_its_way():
    from edaloop.route_classes import Obstacles, _seg_rect, plan_tap
    tap = {"name": "A.1", "net": "S", "x": 0.0, "y": 0.0, "hw": 0.2, "hh": 0.2, "fx": 0.0, "fy": -1.0, "layers": ["F.Cu"]}
    block = {"name": "B.1", "net": "X", "x": 0.0, "y": 2.0, "hw": 0.5, "hh": 0.5, "fx": 0.0, "fy": 2.0, "layers": ["F.Cu"]}
    stub = plan_tap(tap, [(-5.0, 5.0), (5.0, 5.0)], "S", 0.3, [0.2], Obstacles(_NoBoard([tap, block]), "F.Cu", 0.2))
    assert stub is not None and len(stub) > 1
    assert all(_seg_rect(s["start"], s["end"], block) >= 0.35 for s in stub)


def test_unreachable_tap_is_reported_not_drawn():
    from edaloop.route_classes import Obstacles, plan_tap
    tap = {"name": "A.1", "net": "S", "x": 0.0, "y": 0.0, "hw": 0.2, "hh": 0.2, "fx": 0.0, "fy": -1.0, "layers": ["F.Cu"]}
    wall = {"name": "B.1", "net": "X", "x": 0.0, "y": 1.0, "hw": 50.0, "hh": 0.5, "fx": 0.0, "fy": 1.0, "layers": ["F.Cu"]}
    assert plan_tap(tap, [(-5.0, 5.0), (5.0, 5.0)], "S", 0.3, [0.2], Obstacles(_NoBoard([tap, wall]), "F.Cu", 0.2)) is None


def test_run_necks_only_at_the_crowded_end_and_keeps_full_width_elsewhere():
    from edaloop.route_classes import Obstacles, necked_run
    pad = {"name": "U.2", "net": "S", "x": 10.0, "y": 0.0, "hw": 0.4, "hh": 0.125, "fx": 12.0, "fy": 0.0, "layers": ["F.Cu"]}
    neighbour = {"name": "U.1", "net": "X", "x": 10.0, "y": 0.5, "hw": 0.4, "hh": 0.125, "fx": 12.0, "fy": 0.0, "layers": ["F.Cu"]}
    runs = necked_run("S", (0.0, 0.0), (10.0, 0.0), 0.358, [0.3, 0.25, 0.2], Obstacles(_NoBoard([pad, neighbour]), "F.Cu", 0.2), "F.Cu")
    assert len(runs) == 2 and runs[0]["width"] == 0.358 and runs[1]["width"] < 0.358
    assert math.dist(runs[1]["start"], runs[1]["end"]) < 1.0     # a short neck, not the whole line
    assert runs[0]["start"] == (0.0, 0.0) and runs[1]["end"] == (10.0, 0.0)


def test_run_with_room_stays_one_full_width_segment():
    from edaloop.route_classes import Obstacles, necked_run
    runs = necked_run("S", (0.0, 0.0), (10.0, 0.0), 0.36, [0.2], Obstacles(_NoBoard([]), "F.Cu", 0.2), "F.Cu")
    assert len(runs) == 1 and runs[0]["width"] == 0.36


def test_router_is_pinned_to_project_rules_and_cannot_keep_a_rewrite(tmp_path, monkeypatch):
    from edaloop import route_classes as rc
    project = tmp_path / "p.kicad_pro"
    data = {"board": {"design_settings": {"rules": {"min_via_diameter": 0.56, "min_through_hole_diameter": 0.2,
                                                    "min_track_width": 0.2, "min_via_annular_width": 0.1778}}},
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2,
                                          "via_diameter": 0.56, "via_drill": 0.2}]}}
    project.write_text(json.dumps(data))
    args = rc.krt_rule_args(rc.project_rules(project))
    assert "--no-fix-drc-settings" in args
    assert args[args.index("--clearance") + 1] == "0.2" and args[args.index("--via-drill") + 1] == "0.2"
    out = tmp_path / "out.kicad_pcb"

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        relaxed = json.loads(project.read_text())
        relaxed["board"]["design_settings"]["rules"]["min_via_annular_width"] = 0.05
        out.with_suffix(".kicad_pro").write_text(json.dumps(relaxed))
        return R()
    monkeypatch.setattr(rc.toolchain, "run", fake_run)
    result = rc._krt(tmp_path / "in.kicad_pcb", out, ["*"], project=project)
    assert result["rules_rewritten_by_router"] is True
    assert rc.project_rules(out.with_suffix(".kicad_pro")) == rc.project_rules(project)


def test_tap_can_join_a_diagonal_spine_at_45_degrees_not_only_at_its_nearest_point():
    from edaloop.route_classes import Obstacles, plan_tap
    pad = {"name": "F.1", "net": "D", "x": 9.9, "y": 8.0, "hw": 0.5, "hh": 0.5, "fx": 11.5, "fy": 8.0, "layers": ["F.Cu"]}
    spine = [(4.725, 14.5), (4.725, 15.16), (9.9, 20.34), (9.9, 21.0)]
    stub = plan_tap(pad, spine, "D", 0.36, [0.2], Obstacles(_NoBoard([pad]), "F.Cu", 0.2))
    assert stub is not None and stub[0]["start"] == (9.9, 8.0)
    assert stub[0]["end"][0] < 9.9                               # leaves the pad westward, its outward side
    end = stub[-1]["end"]
    assert min(math.dist(end, p) for p in spine) < 20


def test_tap_keeps_clear_of_neighbouring_pins_escape_room():
    from edaloop.route_classes import Obstacles, plan_tap
    pads = [_qfn_pad("U.14", "SUM", 32.825, 13.0)]
    pads += [_qfn_pad(f"U.{i}", f"N{i}", 32.825, 13.0 + 0.5 * k) for k, i in enumerate((13, 12, 11), start=1)]
    pads += [_qfn_pad(f"U.{i}", f"N{i}", 32.825, 12.5 - 0.5 * k) for k, i in enumerate((15, 16, 17, 18))]
    obstacles = Obstacles(_NoBoard(pads), "F.Cu", 0.2)
    stub = plan_tap(pads[0], [(32.825, 19.0), (43.225, 19.0)], "SUM", 0.358, [0.3, 0.25, 0.2], obstacles)
    assert stub[0]["end"][1] == 13.0
    # It stays on the pin's own row until it's past the corridors in front of pins 13, 12 and 11.
    assert stub[0]["end"][0] >= 33.225 + 1.2


def test_failed_nets_reads_every_form_the_router_reports(tmp_path):
    from edaloop.route_classes import failed_nets
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"failed_single": ["A"], "failed_multipoint": [{"net_name": "B", "failed_pads": []}], "open_single": ["A", "C"]}))
    assert failed_nets(f) == ["A", "B", "C"]
    assert failed_nets(tmp_path / "missing.json") == []


# ---------------------------------------------------------------- written test benches

def _bench(name):
    import yaml

    from edaloop import testbench
    return yaml.safe_load((testbench.BENCHES / f"{name}.yaml").read_text())


@pytest.mark.parametrize("name", ["lband_element", "cband_element"])
def test_bench_descriptions_pass(name, tmp_path):
    from edaloop import testbench
    result = testbench.run(testbench.BENCHES / f"{name}.yaml", tmp_path)
    assert result["ok"], result["checks"]
    assert {r["check"]: r["status"] for r in result["checks"]}["design_table"] == "pass"


def test_bench_catches_a_broken_chain():
    from edaloop import testbench
    bench = _bench("cband_element")
    assert testbench.check_chain(bench)["status"] == "pass"
    del bench["chain"][4]       # lose the path out of the first LNA
    assert testbench.check_chain(bench)["status"] == "fail"


def test_bench_catches_a_pin_on_two_nets_and_an_unconnected_part():
    from edaloop import testbench
    bench = _bench("lband_element")
    bench["nets"]["VDD"].append("U2.1")
    bench["nets"]["GND"].remove("R2.2")
    bench["nets"]["TSENSE"].remove("R2.1")
    row = testbench.check_connectivity(bench)
    assert row["status"] == "fail" and "U2.1" in row["detail"] and "R2 has no connections" in row["detail"]


def test_bench_catches_a_wrong_stage_gain():
    from edaloop import testbench
    bench = _bench("cband_element")
    assert testbench.check_design_table(bench)["status"] == "pass"
    next(r for r in bench["design_table"]["rows"] if r["stage"] == "U2")["gain_db"] -= 1.0
    assert testbench.check_design_table(bench)["status"] == "fail"


def test_bench_without_ngspice_is_skipped_not_passed(tmp_path, monkeypatch):
    from edaloop import testbench
    real = toolchain.paths()
    monkeypatch.setattr(toolchain, "paths", lambda: {**real, "ngspice": str(tmp_path / "absent")})
    row = testbench.check_bias(_bench("lband_element"), tmp_path, "x")
    assert row["status"] == "skipped"


def test_bench_catches_a_wrong_bias_and_a_missing_model(tmp_path):
    from edaloop import testbench
    if not shutil.which(str(toolchain.paths()["ngspice"])):
        pytest.skip("needs ngspice")
    bench = _bench("cband_element")
    bench["spice"]["expect"]["i_total_ma"]["value"] = 50.0
    assert testbench.check_bias(bench, tmp_path, "wrong")["status"] == "fail"
    bench = _bench("cband_element")
    del bench["spice"]["models"]["FL2"]
    row = testbench.check_bias(bench, tmp_path, "missing")
    assert row["status"] == "fail" and "FL2" in row["detail"]


# ---------------------------------------------------------------- building a board from a description

def test_build_files_carry_the_description(tmp_path):
    import yaml

    from edaloop import boardgen, testbench
    bench = yaml.safe_load((testbench.BENCHES / "cband_element.yaml").read_text())
    from edaloop import schematic
    sheet = schematic.schematic(bench, "x")
    assert sheet.count("(no_connect") == 3 and 'global_label "LNA1OUT"' in sheet and "(wire" in sheet
    assert '(symbol "U2"' in schematic.symbol_library(sheet)
    from edaloop.scripts.kicad_pcb import load_chains
    spec = tmp_path / "chains.yaml"
    spec.write_text(boardgen.chains_file(bench))
    chains, width, net_width = load_chains(spec)
    assert ("BIAS1", ("L1", "2"), ("R1", "2")) in chains and width == bench["rf_line_width_mm"]
    assert net_width == bench["layout"]["net_width"]
    rules = boardgen.project_file(bench, "x")["board"]["design_settings"]["rules"]
    assert rules["min_through_hole"] <= 0.2     # vias are 0.2 mm drill: KiCad's default rule would reject them all


def test_stackup_text_matches_the_description():
    import yaml

    from edaloop import boardgen, testbench
    bench = yaml.safe_load((testbench.BENCHES / "lband_element.yaml").read_text())
    text = boardgen.seed_board(bench)
    assert '(layer "In2.Cu" (type "copper") (thickness 0.0152))' in text and "(thickness 0.2104)" in text
    tree = parse_sexpr(text)
    assert tree[0] == "kicad_pcb"


def test_ground_pad_distance_follows_the_pads_true_angle():
    from edaloop import ground
    pad = {"x": 10.0, "y": 10.0, "w": 2.0, "h": 0.4, "rot": 45.0}
    # Along the long side, in the pad's own axes and back again.
    x, y = ground._from_pad(pad, 1.0, 0.0)
    assert ground.pad_gap(pad, x, y) == pytest.approx(0.0, abs=1e-9)
    x, y = ground._from_pad(pad, 0.0, 0.5)
    assert ground.pad_gap(pad, x, y) == pytest.approx(0.3, abs=1e-9)


def test_rule_errors_never_delete_ground_vias():
    from edaloop import ground
    assert "clearance" in ground.PLACEMENT_FAULTS
    assert "drill_out_of_range" not in ground.PLACEMENT_FAULTS
    assert "annular_width" not in ground.PLACEMENT_FAULTS


def test_coupon_text_loses_parts_and_keeps_pours():
    from edaloop.route_classes import _without_parts
    text = '(kicad_pcb (net "A)") (footprint "x" (pad "1")) (segment (start 0 0)) (zone (layer "F.Cu")) (via (at 1 1)))'
    out = _without_parts(text)
    assert "footprint" not in out and "segment" not in out and "via" not in out
    assert '(zone (layer "F.Cu"))' in out and '(net "A)")' in out


# ---------------------------------------------------------------- hops solved as routed

def test_hop_ports_sit_at_a_rotated_lands_edge():
    from shapely.geometry import Point

    from edaloop import em_hops
    pad = {"x": 13.0, "y": 29.0, "w": 0.9, "h": 0.5, "rot": 128.2, "net": "RF"}
    at = em_hops.land_edge(pad, (0.0, -1.0))
    # Just past the land's outline, on the track side, and still on the line through the pad's centre.
    assert not em_hops.pad_polygon(pad).contains(Point(at))
    assert em_hops.pad_polygon(pad).distance(Point(at)) < 0.01
    assert at[0] == pytest.approx(13.0) and at[1] < 29.0


def test_hop_port_direction_points_into_the_copper():
    from edaloop import em_hops
    assert em_hops.direction_label((1.0, 0.0)) == "R0"
    # Board y runs down, so moving down the board is clay's -y.
    assert em_hops.direction_label((0.0, 1.0)) == "R270"
    assert em_hops.direction_label((0.0, -1.0)) == "R90"


def test_pour_holes_are_split_away():
    from shapely.geometry import Polygon, box

    from edaloop import em_hops
    ring = box(0, 0, 10, 10).difference(box(4, 4, 6, 6))
    pieces = em_hops._no_holes(ring)
    assert all(not x.interiors for x in pieces)
    assert sum(x.area for x in pieces) == pytest.approx(96.0)
    assert not Polygon(pieces[0].exterior).is_empty


def test_cascade_uses_nearest_solved_loss():
    assert checks._nearest_loss({"1.000": 0.1, "2.000": 0.3}, 1.8) == 0.3


# ---------------------------------------------------------------- the 2D reference

def test_reference_matches_hammerstad_jensen_microstrip():
    from edaloop import em_reference
    # A thin 420 um strip on 210 um of er 4.3, with ground far away: 49.23 ohm and eps_eff 3.275 by closed form.
    z, eps = em_reference._solve(420, 3000, 1, 210, 4.3, 3000, 3000, 4)
    assert z == pytest.approx(49.23, abs=0.5)
    assert eps == pytest.approx(3.275, abs=0.03)


def test_reference_grid_converges_on_the_board_line():
    from edaloop import em_reference
    r = em_reference.coplanar_z0(355, 300, 35, 210.4, 4.3)
    assert r["z0_ohm"] == pytest.approx(50.05, abs=0.2)
    assert r["grid_change_ohm"] < 0.5


@pytest.mark.parametrize("name", ["lband_element", "cband_element"])
def test_drawn_schematic_keeps_the_described_netlist(name, tmp_path):
    import yaml

    from edaloop import schematic, testbench
    cli = toolchain.paths()["kicad_cli"]
    if not shutil.which(str(cli)):
        pytest.skip("needs kicad-cli")
    bench = yaml.safe_load((testbench.BENCHES / f"{name}.yaml").read_text())
    (tmp_path / "x.kicad_sch").write_text(schematic.schematic(bench, "x"))
    out = tmp_path / "n.xml"
    toolchain.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, tmp_path / "x.kicad_sch"], timeout=120)
    assert schematic.check_netlist(out, bench) == []


def test_netlist_check_catches_a_moved_pin(tmp_path):
    from edaloop import schematic
    out = tmp_path / "n.xml"
    out.write_text('<export><components><comp ref="R1"/></components><nets><net name="A"><node ref="R1" pin="2"/></net></nets></export>')
    problems = schematic.check_netlist(out, {"nets": {"A": ["R1.1"]}, "parts": {"R1": {}}})
    assert problems and "R1.1" in problems[0]


def test_ground_vias_meet_the_fabricators_preferred_drill():
    from edaloop import boardgen, ground
    assert ground.DRILL >= 0.254 and (ground.VIA - ground.DRILL) / 2 >= boardgen.RULES["min_via_annular_width"]


# ---------------------------------------------------------------- placement derived from the nets

def _built(name, tmp_path, footprints=()):
    from edaloop import boardgen, testbench
    if not shutil.which(str(toolchain.paths()["kicad_cli"])) or not Path(toolchain.paths()["kicad_python"]).exists():
        pytest.skip("needs KiCad")
    return boardgen.build(testbench.BENCHES / f"{name}.yaml", tmp_path / "board", list(footprints))


def test_demo_board_builds_from_standard_footprints_with_rf_pads_on_the_line(tmp_path):
    result = _built("gain_block_demo", tmp_path)
    assert len(result["positions"]) == 18
    from edaloop import ground
    facts = ground._facts(tmp_path / "board" / "gain_block_demo.kicad_pcb", tmp_path)
    at = {f"{p['ref']}.{p['pad']}": p for p in facts["pads"]}
    for pin in ("J1.1", "C1.1", "C1.2", "U1.3", "U1.1", "C2.1", "L1.1", "U2.3", "U2.1", "C3.1", "L3.1", "J2.1"):
        assert at[pin]["y"] == pytest.approx(12.0, abs=0.002), pin


def test_placer_finds_the_diagonal_parts_angles(tmp_path):
    # The custom land patterns aren't in the repo. Name the folder holding them to run this.
    pretty = Path(os.environ.get("EDALOOP_FOOTPRINTS", "/nonexistent"))
    if not (pretty / "RFLib_FP.pretty").is_dir():
        pytest.skip("set EDALOOP_FOOTPRINTS to run this")
    result = _built("cband_element", tmp_path, [pretty])
    assert result["positions"]["U1"][2] == pytest.approx(49.6, abs=0.05)
    assert result["positions"]["U2"][1] == pytest.approx(9.75, abs=0.01)       # half a pitch off the row


def test_build_refuses_a_folder_with_a_board_in_it(tmp_path):
    from edaloop import boardgen, testbench
    (tmp_path / "board").mkdir()
    (tmp_path / "board" / "routed.kicad_pcb").write_text("x")
    with pytest.raises(boardgen.BuildError):
        boardgen.build(testbench.BENCHES / "gain_block_demo.yaml", tmp_path / "board", [])


def test_a_refused_build_says_why(tmp_path):
    from edaloop import boardgen, testbench
    (tmp_path / "board").mkdir()
    (tmp_path / "board" / "routed.kicad_pcb").write_text("x")
    result = boardgen.make(testbench.BENCHES / "gain_block_demo.yaml", tmp_path / "board", [])
    assert result["made"] is False and "isn't empty" in result["refused"]


def test_a_backed_outlier_goes_to_a_person_not_round_again(tmp_path):
    (tmp_path / "edaloop.yaml").write_text("name: x\nem: {max_spread_pct: 2}\n")
    (tmp_path / "x.kicad_pcb").write_text("(kicad_pcb)")
    project = type("P", (), {"section": lambda self, name: {"max_spread_pct": 2}})()
    ev = type("E", (), {"metrics": {"spread_pct": 7.4, "reference": {"z0_ohm": 50.1, "backs": ["a", "b"]},
                                    "diagnosis": {"verdict": "outlier", "outliers": ["c"], "agreeing": ["a", "b"]}}})()
    step = controller._needs_person(project, "em_crosscheck", ev, [{"id": "em_crosscheck:c:outlier"}])
    assert step["action"] == "ask_person" and "c disagrees" in step["question"]
    assert step["if_waived"]["add_to_edaloop_yaml"]["waivers"][0]["by"] == "YOUR NAME"
    ev.metrics["reference"]["backs"] = ["a"]       # the reference only half backs them: still the agent's to work on
    assert controller._needs_person(project, "em_crosscheck", ev, []) is None


# ---------------------------------------------------------------- describing a schematic

def _netlist(tmp_path, nets, parts):
    comps = "".join(f'<comp ref="{r}"><value>{v}</value><footprint>Lib:{r}</footprint></comp>' for r, v in parts.items())
    body = "".join(f'<net name="{n}">' + "".join(f'<node ref="{m.split(".")[0]}" pin="{m.split(".")[1]}"/>' for m in ms) + "</net>" for n, ms in nets.items())
    path = tmp_path / "n.xml"
    path.write_text(f"<export><components>{comps}</components><nets>{body}</nets></export>")
    return path


def test_describe_finds_the_rf_path_and_leaves_out_the_taps(tmp_path):
    from edaloop import describe
    parts = {"J1": "in", "C1": "100nF", "U1": "LNA", "L1": "100nH", "R1": "0R", "C2": "100nF", "L2": "100nH", "J2": "out", "C3": "100nF"}
    nets = {"A": ["J1.1", "C1.1"], "B": ["C1.2", "U1.3"], "OUT": ["U1.1", "L1.1", "C2.1"], "BIAS": ["L1.2", "R1.2"],
            "COAX": ["C2.2", "L2.1", "J2.1"], "VDD": ["L2.2", "R1.1", "C3.1"], "GND": ["U1.2", "C3.2", "J1.2", "J2.2"]}
    d = describe.describe(_netlist(tmp_path, nets, parts), "x", [900, 1500])
    assert [c[1:] for c in d["chain"]] == [["J1.1", "C1.1"], ["C1.2", "U1.3"], ["U1.1", "C2.1"], ["C2.2", "J2.1"]]
    assert d["project"]["rails"][0]["loads"] == {"R1.1": describe.NEEDED}


def test_describe_refuses_a_board_with_one_connector(tmp_path):
    from edaloop import describe
    with pytest.raises(describe.DescribeError):
        describe.describe(_netlist(tmp_path, {"A": ["J1.1", "C1.1"]}, {"J1": "in", "C1": "100nF"}), "x", [1, 2])


def test_a_build_refuses_an_unknown_current(tmp_path):
    import yaml

    from edaloop import boardgen, testbench
    bench = yaml.safe_load((testbench.BENCHES / "gain_block_demo.yaml").read_text())
    bench["project"]["rails"][0]["loads"]["R1.1"] = "REQUIRED"
    path = tmp_path / "d.yaml"
    path.write_text(yaml.safe_dump(bench))
    with pytest.raises(boardgen.BuildError, match="R1.1"):
        boardgen.build(path, tmp_path / "board", [])


def test_describe_gives_up_on_a_search_too_large_rather_than_hanging(tmp_path, monkeypatch):
    from edaloop import describe
    monkeypatch.setattr(describe, "SEARCH_LIMIT", 3)
    parts = {"J1": "in", "C1": "1nF", "C2": "1nF", "C3": "1nF", "J2": "out"}
    nets = {"A": ["J1.1", "C1.1", "C2.1", "C3.1"], "B": ["C1.2", "C2.2", "C3.2", "J2.1"]}
    with pytest.raises(describe.DescribeError, match="searched"):
        describe.describe(_netlist(tmp_path, nets, parts), "x", [1, 2])


def test_describe_separates_channels_and_ignores_a_shared_control_bus(tmp_path):
    from edaloop import describe
    parts = {"J1": "i", "U1": "a", "J2": "o", "J3": "i", "U2": "a", "J4": "o", "J5": "h", "U3": "c", "U4": "c", "U5": "c", "U6": "c", "U7": "c"}
    nets = {"A": ["J1.1", "U1.1"], "B": ["U1.2", "J2.1"], "C": ["J3.1", "U2.1"], "D": ["U2.2", "J4.1"],
            "CTRL": ["U1.3", "U2.3", "U3.1", "U4.1", "U5.1", "U6.1", "U7.1", "J5.1"], "GND": ["U1.4", "U2.4"]}
    d = describe.describe(_netlist(tmp_path, nets, parts), "x", [1, 2])
    assert [r[0] for r in d["chain"]] == ["A", "B", "C", "D"] or [r[0] for r in d["chain"]] == ["C", "D", "A", "B"]


def test_channel_parts_found_from_nets():
    from edaloop.channel_groups import channel_owners, spread_bypass

    pins = {
        "U1": {"1": "CTL_A", "2": "SCL", "3": "GND"}, "U2": {"1": "CTL_B", "2": "SCL", "3": "GND"},
        "R1": {"1": "CTL_A", "2": "SET_A"}, "R2": {"1": "CTL_B", "2": "SET_B"},
        "Q1": {"1": "SET_A", "2": "VBUS", "3": "GND"}, "Q2": {"1": "SET_B", "2": "VBUS", "3": "GND"},
        # The expander drives both channels, so it belongs to neither, however near it is to the first.
        "X1": {"1": "SET_A", "2": "SET_B", "3": "SCL", "4": "GND"},
        "C1": {"1": "VBUS", "2": "GND"}, "C2": {"1": "VBUS", "2": "GND"},
        "C9": {"1": "VBOARD", "2": "GND"},
    }
    rows = [{"U1"}, {"U2"}]
    owners = channel_owners(pins, rows)
    assert owners == {"R1": 0, "Q1": 0, "R2": 1, "Q2": 1}
    # One capacitor per row on the shared rail, so one each. A rail's lone capacitor stays with the shared parts.
    assert spread_bypass(pins, rows, owners) == {"C1": 0, "C2": 1}
    # Two over stay shared, rather than the whole rail staying at the far end of the board.
    pins.update({"C3": {"1": "VBUS", "2": "GND"}, "C4": {"1": "VBUS", "2": "GND"}, "C5": {"1": "VBUS", "2": "GND"}})
    assert spread_bypass(pins, rows, owners) == {"C1": 0, "C2": 0, "C3": 1, "C4": 1}


def test_seat_takes_the_cheapest_free_place():
    from edaloop.channel_groups import seat, seating_order

    shapes = {0.0: {"box": (-1, -1, 1, 1), "pads": [("N", 0, 0)]}}
    # Free ground puts it right on its target.
    assert seat(shapes, [], {"N": [(5, 5)]}, (0, 0, 10, 10)) == (5, 5, 0.0)
    # With the target occupied it stops at the obstacle's edge, not somewhere arbitrary.
    x, y, _ = seat(shapes, [(4, 4, 6, 6)], {"N": [(5, 5)]}, (0, 0, 10, 10))
    assert abs(x - 5) + abs(y - 5) == 2
    # A full region says so rather than overlapping.
    assert seat(shapes, [(0, 0, 10, 10)], {"N": [(5, 5)]}, (0, 0, 10, 10)) is None
    # Growing the row costs board length across every channel, so free room inside it is preferred.
    x, y, _ = seat(shapes, [], {"N": [(5, 9.5)]}, (0, 0, 10, 20), stack_high=10.0)
    assert y + 1 <= 10.0
    pins = {"A": {"1": "N1"}, "B": {"1": "N1", "2": "N2"}, "C": {"1": "N2"}}
    assert seating_order(["C", "B"], pins, {"A"}) == ["B", "C"]


def test_pad_on_no_net_is_visible(tmp_path):
    from edaloop.pcbfile import Board

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229)
 (footprint "x" (at 0 0) (property "Reference" "U1")
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A"))
  (pad "7" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
  (pad "" smd rect (at 2 0) (size 1 1) (layers "F.Cu"))))
""")
    names = {x["name"]: x["net"] for x in Board(f).all_pads()}
    assert names["U1.7"] == ""
    assert sorted(n for n, net in names.items() if not net and not n.endswith(".")) == ["U1.7"]


def test_describe_reads_a_board_with_no_schematic(tmp_path):
    from edaloop import describe

    def fp(ref, value, pads):
        body = "".join(f'(pad "{n}" smd rect (at 0 0) (size 1 1) (layers "F.Cu") {f"(net {i})" if i else ""})' for n, i in pads)
        return f'(footprint "Lib:{ref}" (at 0 0) (property "Reference" "{ref}") (property "Value" "{value}") {body})'
    f = tmp_path / "b.kicad_pcb"
    f.write_text('(kicad_pcb (version 20241229) (net 0 "") (net 1 "A") (net 2 "B") (net 3 "GND") '
                 + fp("J1", "in", [("1", 1), ("2", 3)]) + fp("U1", "LNA", [("1", 1), ("2", 2), ("3", 3), ("4", 0), ("3", 3)])
                 + fp("J2", "out", [("1", 2), ("2", 3)]) + ')')
    parts, nets = describe.read_board(f)
    assert parts["U1"] == {"value": "LNA", "footprint": "Lib:U1"}
    # A pad repeated for a thermal paddle counts once, and an empty pad is an open pin.
    assert nets["GND"] == ["J1.2", "U1.3", "J2.2"]
    assert nets["unconnected-(U1-Pad4)"] == ["U1.4"]
    d = describe.describe(f, "x", [900, 1500])
    assert [c[1:] for c in d["chain"]] == [["J1.1", "U1.1"], ["U1.2", "J2.1"]]
    assert d["open_pins"] == ["U1.4"]


def test_chain_oip3_is_unknown_when_a_stage_gives_none():
    stages = [{"loss_db": [[1, 1.0]]}, {"gain_db": [[1, 20.0]], "nf_db": [[1, 1.0]], "oip3_dbm": [[1, 30.0]]}]
    assert checks.friis(stages, 1)["oip3_dbm"] is not None
    stages.append({"gain_db": [[1, 10.0]], "nf_db": [[1, 2.0]]})
    r = checks.friis(stages, 1)
    # Gain and noise still come out; only the figure that can't be known is withheld.
    assert r["oip3_dbm"] is None and r["gain_db"] == 29.0


def _tiny_project(tmp_path, extra=""):
    (tmp_path / "b.kicad_pcb").write_text(PCB)
    (tmp_path / "edaloop.yaml").write_text("name: t\nboard: b.kicad_pcb\n" + extra)
    return Project(tmp_path)


def test_no_rails_is_not_a_pass(tmp_path):
    p = _tiny_project(tmp_path, "power:\n  rails: []\n")
    o = checks.power_rails(p)
    assert [f.id for f in o.findings] == ["power:no_rails"] and o.findings[0].severity == "error"
    o = checks.thermal_field(p)
    assert [f.id for f in o.findings] == ["thermal_field:no_rails"]


def test_retries_stop_when_a_long_attempt_gains_nothing():
    from edaloop.route_classes import RETRY_WITHOUT_GAIN_S, keep_retrying

    assert keep_retrying(False, 60)                         # quick board: order is luck, so try again
    assert keep_retrying(True, RETRY_WITHOUT_GAIN_S * 3)    # slow but still gaining
    assert not keep_retrying(False, RETRY_WITHOUT_GAIN_S + 1)


def test_one_unsolved_hop_fails_without_losing_the_others(tmp_path, monkeypatch):
    from edaloop import em_hops, route_classes

    p = _tiny_project(tmp_path, "rf:\n  chains: c.yaml\nem_hops:\n  band_ghz: [1, 2]\n")
    (tmp_path / "c.yaml").write_text("")
    monkeypatch.setattr(route_classes, "_load_chains", lambda path: ([("A", ("U1", "1"), ("U2", "1")), ("B", ("U2", "2"), ("U3", "1"))], 0.36, {}))
    monkeypatch.setattr(em_hops, "run", lambda *a, **k: {
        "A": {"rl_db": 30.0, "il_max_db": 0.1}, "B": {"unsolved": "no field", "digest": "x"}})
    o = checks.em_hops(p)
    assert o.metrics["worst_rl_db"] == 30.0 and o.metrics["unsolved"] == ["B"]
    assert [(f.id, f.severity) for f in o.findings] == [("em_hops:B:unsolved", "error")]


def test_losses_add_along_a_path_and_not_across_paths():
    from edaloop.checks import _hop_loss_table, series_groups

    chains = [("A", ("J1", "1"), ("U1", "1")), ("B", ("U1", "2"), ("J2", "1")), ("C", ("J3", "1"), ("U2", "1")), ("D", ("U2", "2"), ("J4", "1"))]
    assert series_groups(chains) == [["A", "B"], ["C", "D"]]
    hops = {n: {"il_by_ghz": {"1": v}} for n, v in {"A": 0.1, "B": 0.2, "C": 0.1, "D": 0.5}.items()}
    ev = Evidence("em_hops", "pass", "", {}, {"hops": hops, "channels": [["A", "B"], ["C", "D"]]})
    # Four hops on two paths: the worst path's loss, not all four added up.
    assert _hop_loss_table(ev) == [[1000.0, 0.6]]


def test_many_channels_draw_on_their_own_pages():
    from edaloop import schematic as s

    parts = {r: {"value": "x"} for r in ("J1", "U1", "J2", "R1", "J3", "U2", "J4", "R2", "X1")}
    nets = {"A": ["J1.1", "U1.1"], "B": ["U1.2", "J2.1"], "C": ["J3.1", "U2.1"], "D": ["U2.2", "J4.1"], "CTL1": ["U1.3", "R1.1"], "CTL2": ["U2.3", "R2.1"],
            "S1": ["R1.2", "X1.1"], "S2": ["R2.2", "X1.2"], "GND": ["U1.4", "U2.4", "X1.3"]}
    bench = {"name": "t", "parts": parts, "nets": nets, "chain": [["A", "J1.1", "U1.1"], ["B", "U1.2", "J2.1"], ["C", "J3.1", "U2.1"], ["D", "U2.2", "J4.1"]],
             "layout": {"footprints": {r: "L:" + r for r in parts}}, "summary": "s"}
    plan = s.page_plan(bench)
    assert [p["title"] for p in plan] == ["Channel 1", "Channel 2", "Shared"]
    assert plan[0]["refs"] == {"J1", "U1", "J2", "R1"} and plan[2]["refs"] == {"X1"}
    out = s.project_sheets(bench, "t")
    assert list(out["files"]) == ["t.kicad_sch", "t_ch1.kicad_sch", "t_ch2.kicad_sch", "t_shared.kicad_sch"]
    # The board's footprints have to name the page's sheet as well as the symbol, or parity fails.
    assert out["uuids"]["U1"].count("/") == 1 and out["placed"]["U1"]["file"] == "t_ch1.kicad_sch"
    # Every net crosses pages by name, so a net split across two pages has the same global label on each.
    assert 'global_label "S2"' in out["files"]["t_shared.kicad_sch"] and 'global_label "S2"' in out["files"]["t_ch2.kicad_sch"]
    assert '"edaloop:U1"' in out["library"].replace('(symbol "U1"', '"edaloop:U1"')
    bench["layout"]["schematic_pages"] = "one"
    assert s.page_plan(bench) == []


def test_describe_takes_stackup_and_width_from_a_board(tmp_path):
    from edaloop import describe

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229) (net 0 "") (net 1 "A") (net 2 "B")
 (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "d" (type "core") (thickness 0.5) (epsilon_r 3.5) (loss_tangent 0.004))
  (layer "B.Cu" (type "copper") (thickness 0.035))))
 (footprint "L:J" (at 0 0) (property "Reference" "J1") (property "Value" "in") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1)))
 (footprint "L:U" (at 5 0) (property "Reference" "U1") (property "Value" "x") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1))
  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2)))
 (footprint "L:J" (at 9 0) (property "Reference" "J2") (property "Value" "out") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2)))
 (segment (start 0 0) (end 5 0) (width 0.9) (layer "F.Cu") (net 1)))
""")
    d = describe.describe(f, "x", [1, 2])
    assert d["stackup"] == {"copper_um": [35.0, 35.0], "dielectric_mm": [0.5], "epsilon_r": 3.5, "loss_tangent": 0.004}
    assert d["rf_line_width_mm"] == 0.9 and d["assumed"] == []


def test_describe_says_what_it_assumed(tmp_path):
    from edaloop import describe

    d = describe.describe(_netlist(tmp_path, {"A": ["J1.1", "U1.1"], "B": ["U1.2", "J2.1"], "GND": ["U1.3"]}, {"J1": "i", "U1": "a", "J2": "o"}), "x", [1, 2])
    assert d["assumed"] == ["stackup", "rf_line_width_mm"]


def test_compare_says_when_boards_differ_and_by_how_much(tmp_path):
    from edaloop.compare import compare

    def board(name, end, via, other):
        f = tmp_path / f"{name}.kicad_pcb"
        f.write_text(f"""(kicad_pcb (version 20241229) (net 0 "") (net 1 "RF") (net 2 "{other}")
 (gr_rect (start 0 0) (end 20 {end}) (layer "Edge.Cuts"))
 (footprint "x" (at 0 0) (property "Reference" "U1") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1)) (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2)))
 (footprint "x" (at 9 0) (property "Reference" "U2") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1)))
 (segment (start 0 0) (end {end} 0) (width 0.3) (layer "F.Cu") (net 1))
 {via})""")
        return f
    a = board("a", 10, "", "N")
    b = board("b", 14, '(via (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))', "RENAMED")
    r = compare(a, b, ["RF"])
    # A renamed net joins the same pads, so it's the same design.
    assert r["same_connections"]
    assert r["difference"]["rf_mm"] == -4.0 and r["difference"]["rf_vias"] == -1 and r["rf_net_changes_mm"] == {"RF": -4.0}
    c = board("c", 10, "", "N")
    c.write_text(c.read_text().replace('(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2))', ""))
    assert not compare(a, c, ["RF"])["same_connections"]


def test_hops_solve_at_once_and_a_failure_stays_with_its_hop(tmp_path):
    import threading

    from edaloop.em_hops import solve_unique

    entered = threading.Barrier(2, timeout=5)

    def solve(ws):
        # Waits for its partner, so this only finishes if both run at the same time.
        entered.wait()
        if ws.name == "bad":
            raise RuntimeError("no field")
        return (ws, 1.0)
    jobs = {"a": tmp_path / "good", "b": tmp_path / "bad"}
    out = solve_unique(jobs, solve, 2)
    assert out["a"] == (tmp_path / "good", 1.0) and out["b"] == "no field"


@pytest.mark.parametrize("args", [["status"], ["next"], ["checks"]])
def test_every_project_command_starts(tmp_path, monkeypatch, capsys, args):
    # A function-level import in main() once shadowed a module for every other command, and nothing noticed.
    from edaloop import cli

    _tiny_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(args)
    out = capsys.readouterr()
    assert code in (0, 1) and "Traceback" not in out.err and "UnboundLocalError" not in out.err


def test_plane_voids_go_under_taps_and_not_under_the_path(tmp_path):
    from shapely.geometry import box

    from edaloop import em_hops
    from edaloop.ground import tap_voids

    def pad(ref, n, net, w, h=None):
        return {"ref": ref, "pad": n, "net": net, "x": 5.0, "y": 0.0, "w": w, "h": h or w, "rot": 0}
    pads = [pad("U1", "1", "A", 0.9), pad("L1", "1", "A", 0.9), pad("R1", "1", "A", 0.3), pad("U2", "1", "A", 0.9), pad("L2", "1", "B", 0.9)]
    voids = tap_voids(pads, [["A", "U1.1", "U2.1"]], 0.15, 0.36)
    # Only the wide tap: not the ends of the hop, not a pad no wider than the line, and not another net.
    assert [v["pad"] for v in voids] == ["L1.1"]
    xs = [p[0] for p in voids[0]["pts"]]
    assert max(xs) - min(xs) == pytest.approx(1.2)

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229)
 (zone (net 0) (layer "In1.Cu") (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
  (polygon (pts (xy 1 1) (xy 2 1) (xy 2 2) (xy 1 2))))
 (zone (net 0) (layer "In2.Cu") (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
  (polygon (pts (xy 1 1) (xy 2 1) (xy 2 2) (xy 1 2))))
 (zone (net 1) (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 9 0) (xy 9 9)))))
""")
    found = em_hops.plane_voids(Board(f), "In1.Cu")
    assert len(found) == 1 and found[0].area == pytest.approx(1.0)
    entry, leave = pad("U1", "1", "A", 0.5), pad("U2", "1", "A", 0.5)
    entry["x"], leave["x"] = 0.0, 10.0
    def planes(**k):
        shapes = em_hops.cut_shapes("A", [], [], [], [], box(-3, -3, 13, 3), 1, 2, entry, leave, **k)
        return [s["Layer"]["Layer"] for s in shapes if s["$type"] == "Poly"]
    assert planes() == [2]
    hole = box(4, -1, 6, 1)
    # A hole makes the plane more than one piece, and brings in the plane underneath it.
    with_hole = planes(voids=[hole], below=3)
    assert with_hole.count(2) > 1 and with_hole.count(3) == 1


def test_escape_stubs_only_beside_rf_and_only_where_they_fit(tmp_path):
    from edaloop.route_classes import ESCAPE_WIDTH_MM, escape_stubs

    pads = "".join(f'(pad "{i + 1}" smd rect (at -2 {-1 + i * 0.5}) (size 0.8 0.25) (layers "F.Cu") (net "{n}"))'
                   for i, n in enumerate(["A", "RF", "B", "C", "D"]))
    pads += "".join(f'(pad "{i + 6}" smd rect (at 2 {-1 + i * 0.5}) (size 0.8 0.25) (layers "F.Cu") (net "E{i}"))' for i in range(5))
    f = tmp_path / "b.kicad_pcb"
    f.write_text(f'(kicad_pcb (version 20241229) (footprint "x" (at 10 10) (property "Reference" "U1") {pads}))')
    made = escape_stubs(Board(f), ["B"], {"RF"}, 0.1)
    # All signal pins within two pitches of the RF pin on its side, not the far pin and not the other side.
    assert sorted(s["net"] for s in made) == ["A", "B", "C"]
    assert all(s["width"] == ESCAPE_WIDTH_MM and s["end"][0] < s["start"][0] and s["start"][1] == s["end"][1] for s in made)
    # A part with no failed pin is left alone.
    assert escape_stubs(Board(f), ["Z"], {"RF"}, 0.1) == []
    # With no room at any length, there's no stub rather than a short.
    blocked = f.read_text().replace('(footprint "x" (at 10 10)', '(segment (start 7.65 8) (end 7.65 12) (width 0.1) (layer "F.Cu") (net "Q")) (footprint "x" (at 10 10)')
    f.write_text(blocked)
    assert all(abs(s["end"][0] - 7.65) > 0.25 for s in escape_stubs(Board(f), ["B"], {"RF"}, 0.1))


def test_only_stubs_kicad_calls_dangling_are_trimmed():
    from edaloop.route_classes import dangling_stubs

    stubs = [{"net": "A", "start": [0, 0], "end": [2, 0]}, {"net": "B", "start": [0, 1], "end": [2, 1]}, {"net": "C", "start": [0, 2], "end": [2, 2]}]
    def dangling(desc, x, y):
        return {"type": "track_dangling", "items": [{"description": desc, "pos": {"x": x, "y": y}}]}
    drc = {"violations": [dangling("Track [A] on F.Cu, length 2 mm", 0, 0), dangling("Track [B] on F.Cu, length 9 mm", 7, 7),
                          {"type": "clearance", "items": [{"description": "Track [C] on F.Cu", "pos": {"x": 2, "y": 2}}]}]}
    # A's is its own stub. B's dangling track is some other copper, and C's report isn't about dangling at all.
    assert [s["net"] for s in dangling_stubs(drc, stubs)] == ["A"]
    assert dangling_stubs({}, stubs) == []


def test_a_stub_is_cut_back_to_its_last_join(tmp_path):
    from edaloop.route_classes import stub_cut

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229)
 (segment (start 0 0) (end 2 0) (width 0.15) (layer "F.Cu") (net "A"))
 (segment (start 1.2 0) (end 5 5) (width 0.2) (layer "F.Cu") (net "A"))
 (segment (start 0 1) (end 2 1) (width 0.15) (layer "F.Cu") (net "B"))
 (segment (start 0 1) (end 0 9) (width 0.2) (layer "F.Cu") (net "B"))
 (segment (start 0 2) (end 2 2) (width 0.15) (layer "F.Cu") (net "C"))
 (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "C")))
""")
    b = Board(f)
    assert stub_cut(b, {"net": "A", "start": [0, 0], "end": [2, 0]}) == [1.2, 0.0]   # the tail past the join goes
    assert stub_cut(b, {"net": "B", "start": [0, 1], "end": [2, 1]}) is None         # left the pad another way: nothing to keep
    assert stub_cut(b, {"net": "C", "start": [0, 2], "end": [2, 2]}) == [2.0, 2.0]   # in use to its end


def test_describe_lists_nets_that_look_like_supplies():
    from edaloop.describe import likely_rails

    nets = {"RF1": ["J1.1", "C1.1"], "GND": ["C2.2", "C3.2", "U1.9"], "VBULK": ["C2.1", "U1.1"], "+5V": ["U1.2", "U2.2"],
            "SCL": ["U1.3", "U2.3"], "SEEN": ["C3.1", "U2.4"], "LONE": ["C9.1"]}
    # A capacitor to ground or a supply's name makes a rail. RF nets, signals and rails already known don't count.
    assert likely_rails(nets, [["RF1", "J1.1", "C1.1"]], ["SEEN"]) == ["+5V", "VBULK"]


def test_tidy_finds_short_tails_but_leaves_rf_alone(tmp_path):
    from edaloop.route_classes import dangling_tails

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229)
 (segment (start 1 1) (end 1 1.05) (width 0.2) (layer "F.Cu") (net "A"))
 (segment (start 2 2) (end 4 2) (width 0.3) (layer "F.Cu") (net "RF")))
""")
    def item(desc, x, y):
        return {"type": "track_dangling", "items": [{"description": desc, "pos": {"x": x, "y": y}}]}
    drc = {"violations": [item("Track [A] on F.Cu, length 0.0500 mm", 1, 1), item("Track [RF] on F.Cu, length 2.0000 mm", 2, 2)]}
    assert dangling_tails(drc, Board(f), {"RF"}) == [{"net": "A", "start": [1.0, 1.0], "end": [1.0, 1.05]}]


def test_part_heat_follows_each_load_to_the_part_using_it():
    from edaloop.thermal import heated_parts, part_heat

    def pad(name, net, x=0.0):
        return {"name": name, "net": net, "x": x, "y": 0.0, "hw": 0.2, "hh": 0.2, "layers": [0]}
    pads = [pad("R1.1", "VDD"), pad("R1.2", "BIAS"), pad("L1.1", "BIAS"), pad("L1.2", "OUT"), pad("U2.6", "OUT", 1.0), pad("U2.2", "GND", 3.0), pad("C2.1", "OUT"),
            pad("C5.1", "BIAS"), pad("C5.2", "GND"), pad("RT1.1", "VDD"), pad("RT1.2", "GND"), pad("R3.1", "VDD"), pad("R3.2", "B2"), pad("U3.1", "B2", 9.0), pad("U3.2", "GND", 9.0)]
    # The same 58 mA listed on the supply and on the bias it feeds is one current, not two.
    rails = [{"net": "VDD", "loads": {"R1.1": 0.058, "R3.1": 0.06, "RT1.1": 0.002}}, {"net": "BIAS", "loads": {"L1.1": 0.058}}]
    heat = part_heat(pads, rails, 5.0)
    assert heat["U2"]["watts"] == pytest.approx(0.29) and heat["U3"]["watts"] == pytest.approx(0.3)
    # A divider that reaches no active part dissipates in itself. It doesn't vanish.
    assert heat["RT1"]["watts"] == pytest.approx(0.01)
    assert set(heat) == {"U2", "U3", "RT1"}
    # A person's number wins, and 0 leaves a part out.
    given = part_heat(pads, rails, 5.0, {"U2": 0.18, "RT1": 0})
    assert given["U2"] == {"amps": None, "watts": 0.18, "source": "given"} and "RT1" not in given
    box = heated_parts(pads, {"U2": heat["U2"]})[0]
    assert box["name"] == "U2" and box["x"] == pytest.approx(2.0) and box["hw"] == pytest.approx(1.2)
    with pytest.raises(ValueError):
        heated_parts(pads, {"U9": heat["U2"]})


def test_no_supply_voltage_is_said_out_loud(tmp_path, monkeypatch):
    from edaloop import thermal

    p = _tiny_project(tmp_path, "power:\n  rails:\n  - {net: RF, source: x.1, loads: {y.1: 0.1}}\nthermal: {}\n")
    outcome = {"hotspot": {"rise_c": 1.0, "layer": "F.Cu", "x": 0, "y": 0}, "converged": True, "energy_error_pct": 0.0, "rails": [], "backend": "cpu", "device": "t", "grid": [1, 1, 1], "timing": {"total_s": 0.1}}
    calls = []
    monkeypatch.setattr(thermal, "fill_copy", lambda board, out: board)
    monkeypatch.setattr(thermal, "export_problem", lambda *a, **k: {"pads": [], "parts": []})
    monkeypatch.setattr(thermal, "run_field", lambda problem, work, *a, **k: (calls.append(problem), outcome)[1])
    o = checks.thermal_field(p)
    assert [f.id for f in o.findings] == ["thermal_field:no_part_heat"] and len(calls) == 1 and "part_rise_c" not in o.metrics


def test_rf_path_to_a_radio_through_a_matching_inductor(tmp_path):
    from edaloop import describe

    parts = {"X1": "2.4GHz", "L2": "3.3nH", "C9": "1pF", "C7": "1.2pF", "U1": "ESP32", "JP1": "header"}
    nets = {"N1": ["X1.FP", "L2.1", "C9.1"], "LNA": ["C7.1", "U1.2", "L2.2"], "D": ["U1.3", "JP1.1"], "E": ["U1.4", "JP1.2"], "GND": ["C9.2", "C7.2", "U1.1"]}
    d = describe.describe(_netlist(tmp_path, nets, parts), "x", [2400, 2500])
    assert [c[1:] for c in d["chain"]] == [["X1.FP", "L2.1"], ["L2.2", "U1.2"]]
    # The matching inductor's far side is the RF path, not a supply it hangs from.
    assert d["project"]["rails"] == []


def test_a_header_is_not_an_rf_port(tmp_path):
    from edaloop import describe

    # The X parts here are a USB connector and a crystal. Neither says it's RF, and JP isn't a J.
    parts = {"X1": "USB TYPE C", "X2": "12MHz", "JP1": "header", "U1": "MCU"}
    nets = {"D": ["X1.1", "U1.1"], "C": ["X2.1", "U1.2"], "P": ["JP1.1", "U1.3"]}
    with pytest.raises(describe.DescribeError):
        describe.describe(_netlist(tmp_path, nets, parts), "x", [1, 2])


def test_eagle_reading_and_repair(tmp_path):
    from edaloop import evaluate

    f = tmp_path / "b.brd"
    f.write_text("""<eagle><drawing><board><designrules><param name="mdWireWire" value="6mil"/><param name="msWidth" value="0.2mm"/></designrules>
    <signals><signal name="A"><contactref element="U1" pad="1"/><contactref element="R1" pad="2"/><wire layer="1"/><wire layer="16"/><wire layer="21"/><via/></signal>
    <signal name="B"><contactref element="U1" pad="2"/></signal></signals></board></drawing></eagle>""")
    e = evaluate.read_eagle(f)
    assert e["groups"] == [frozenset({"U1.1", "R1.2"})] and e["tracks"] == 2 and e["vias"] == 1 and e["copper_layers"] == 2
    assert evaluate.to_mm(e["rules"]["mdWireWire"], 0) == pytest.approx(0.1524) and evaluate.to_mm("bad", 0.2) == 0.2
    pcb = '(kicad_pcb (layers\n\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n\t\t(2 "B.Cu" signal)\n) (setup\n) (gr_line (layer "UNDEFINED")) (segment (layer "In2.Cu")))'
    fixed = evaluate.repair(pcb, {"copper_um": [35, 35], "dielectric_mm": [1.5], "epsilon_r": 4.5})
    assert '"UNDEFINED"' not in fixed and '"In1.Cu" signal' not in fixed and '"In2.Cu" signal' in fixed and "(setup (stackup" in fixed
    stack, guessed = evaluate.stack_for({"known": {"copper_um": [70, 70]}, "assumed": {"dielectric_mm": [1.5]}}, 2)
    assert stack["copper_um"] == [70, 70] and "copper_um" not in guessed and "dielectric_mm" in guessed


def test_every_sample_is_pinned():
    from edaloop import evaluate

    samples = evaluate.load_samples()
    assert len(samples) == 20
    for name, s in samples.items():
        assert re.fullmatch(r"[0-9a-f]{40}", s["commit"]), name
        assert s["file"].endswith((".brd", ".kicad_pcb")), name
        # An Eagle sample states its RF path. A native KiCad one may have none verified yet, which must read as info and never as "no RF path".
        assert "rf_path" in s["known"] or s["file"].endswith(".kicad_pcb"), name
        assert s.get("licence") or s.get("owner", "adafruit") == "adafruit", name


def test_import_score_knows_kicads_reference_renames(tmp_path):
    from edaloop import evaluate

    f = tmp_path / "b.kicad_pcb"
    f.write_text("""(kicad_pcb (version 20241229) (net 0 "") (net 1 "V")
 (footprint "L:A" (at 0 0) (property "Reference" "VBAT0") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1)))
 (footprint "L:B" (at 5 0) (property "Reference" "U1") (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1))))""")
    rows = evaluate.import_score(f, {"groups": [frozenset({"VBAT.1", "U1.2"})], "tracks": 0, "vias": 0, "elements": {"VBAT", "U1"}})
    assert [r["result"] for r in rows] == ["pass", "pass"]


def test_still_air_depends_on_the_board():
    from edaloop.thermal import still_air_h

    small, large = still_air_h(0.54, 168e-6), still_air_h(0.53, 1110e-6)
    # Balance: the average rise is the power over both faces' area and the coefficient.
    for air, w, a in ((small, 0.54, 168e-6), (large, 0.53, 1110e-6)):
        assert air["average_rise_c"] == pytest.approx(w / (2 * a * air["h_w_m2k"]), rel=0.02)
        assert air["h_w_m2k"] == pytest.approx(air["convection"] + air["radiation"], abs=0.02)
    # A small hot plate sheds heat faster per area than a large cool one, and radiation grows with temperature.
    assert small["h_w_m2k"] > large["h_w_m2k"] > 10 and small["radiation"] > large["radiation"]
    # Nearly no heat still radiates, at about 5.4 W/m2K near room temperature.
    assert 5.0 < still_air_h(1e-4, 1e-3)["radiation"] < 6.0


def test_a_port_follows_the_track_off_a_land():
    from types import SimpleNamespace as NS

    from edaloop.em_hops import leaving_direction

    pad = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "rot": 0}
    chord = (0.7071, 0.7071)
    # The far end is off at 45 degrees, but this track leaves straight along x.
    assert leaving_direction(pad, [NS(start=(0.0, 0.0), end=(3.0, 0.0))], chord) == (1.0, 0.0)
    assert leaving_direction(pad, [NS(start=(3.0, 0.0), end=(0.0, 0.0))], chord) == (1.0, 0.0)
    # A track that agrees with the line, or none at all, leaves the port on the line.
    assert leaving_direction(pad, [NS(start=(0.0, 0.0), end=(2.0, 2.0))], (0.70711, 0.70711)) == (0.70711, 0.70711)
    assert leaving_direction(pad, [], chord) == chord


def test_a_hop_reading_as_open_is_flagged(tmp_path, monkeypatch):
    from edaloop import em_hops, route_classes

    p = _tiny_project(tmp_path, "rf:\n  chains: c.yaml\nem_hops:\n  band_ghz: [1, 2]\n")
    (tmp_path / "c.yaml").write_text("")
    monkeypatch.setattr(route_classes, "_load_chains", lambda path: ([("A", ("U1", "1"), ("U2", "1"))], 0.36, {}))
    monkeypatch.setattr(em_hops, "run", lambda *a, **k: {"A": {"rl_db": 0.0, "il_max_db": 66.9}})
    assert [f.id for f in checks.em_hops(p).findings][0] == "em_hops:A:open"


def _mech_facts():
    def pad(ref, n, net, x, y, w=1.0, h=1.0, drill=0.0, back=False):
        return {"ref": ref, "pad": n, "net": net, "x": x, "y": y, "w": w, "h": h, "rot": 0.0, "drill": drill, "back": back, "front": True}
    return {"outline": [0.0, 0.0, 40.0, 20.0],
            "courtyards": [{"ref": r, "rot": 0.0, "back": False, "value": ""} for r in ("U1", "J1", "J2", "C1", "H1", "FID1", "R9")] ,
            "pads": [pad("U1", "1", "A", 18, 9), pad("U1", "2", "GND", 22, 11), pad("J1", "1", "A", 1.5, 10), pad("J1", "2", "GND", 3, 10),
                     pad("J2", "1", "B", 20, 16, drill=0.8), pad("J2", "2", "GND", 20, 14, drill=0.8), pad("C1", "1", "A", 30, 5), pad("C1", "2", "GND", 31, 5),
                     pad("H1", "1", "", 4, 4, 3.2, 3.2, drill=3.2), pad("FID1", "1", "", 35, 15), pad("R9", "1", "VIN", 28, 15), pad("R9", "2", "A", 29, 15, back=True)]}


def test_mechanical_reads_what_the_board_needs():
    from edaloop import mechanical as m

    f = _mech_facts()
    parts = m.part_boxes(f, {"U1": 2.4})
    refs = {p["ref"]: p for p in parts}
    # A mounting hole and a fiducial have no body to clear, and a given height beats the rough one.
    assert "H1" not in refs and "FID1" not in refs and refs["U1"]["tall"] == 2.4 and refs["U1"]["height_given"] and not refs["C1"]["height_given"]
    assert [(h["ref"], h["d"]) for h in m.holes(f)] == [("H1", 3.2)]
    edges = m.edge_openings(parts, f["outline"])
    assert [(e["ref"], e["side"]) for e in edges] == [("J1", "left")]
    lids = m.lid_openings(parts, edges)
    assert [o["ref"] for o in lids] == ["J2"]
    # A drilled or underside pad meets a pin from below, and a top-side pad from above. A pad that isn't there is named.
    found, missing = m.probes(f, [{"net": "B", "source": "J2.1", "loads": {"C1.1": 0.1, "R9.2": 0.1, "Z9.1": 0.1}}])
    assert [(x["pad"], x["from"]) for x in found] == [("J2.1", "below"), ("C1.1", "above"), ("R9.2", "below"), ("J2.2", "below")] and missing == ["Z9.1"]
    # Pegs press near each corner, on clear ground.
    corners = m.press_points(parts, f["outline"])
    assert len(corners) == 4
    for x, y in corners:
        assert all(abs(p["x"] - x) > m._extent(p)[0] or abs(p["y"] - y) > m._extent(p)[1] for p in parts if not p["back"])


def test_mechanical_fit_problems_are_named():
    from edaloop import mechanical as m

    mounts = [{"ref": "H1", "x": 0.5, "y": 10, "d": 3.2}, {"ref": "H2", "x": 20, "y": 10, "d": 3.2}]
    under = [{"ref": "U9", "x": 20, "y": 10, "w": 2, "h": 2, "rot": 0, "back": True}, {"ref": "U8", "x": 30, "y": 10, "w": 2, "h": 2, "rot": 0, "back": True}]
    problems = m.check_fit(under, mounts, [0, 0, 40, 20])
    assert any("H1" in x and "off the board" in x for x in problems) and any("U9" in x for x in problems) and not any("U8" in x for x in problems)


def test_mechanical_scad_uses_the_board_frame():
    from edaloop import mechanical as m

    f = _mech_facts()
    parts = m.part_boxes(f)
    text = m.scad_text("t", f["outline"], 1.6, parts, m.holes(f), m.edge_openings(parts, f["outline"]), [], [], True)
    # Board y runs down and OpenSCAD's runs up, so a hole 4 mm from the top edge of a 20 mm board sits at y 16.
    assert "holes = [[4.0, 16.0, 3.2]]" in text and "board = [40.0, 20.0];" in text and 'part = "assembly"' in text
    for name in ("module test_mount", "module case_bottom", "module case_top"):
        assert name in text


def test_a_mesh_with_a_hole_is_not_closed(tmp_path):
    from edaloop import mechanical as m

    v = {"a": "0 0 0", "b": "1 0 0", "c": "0 1 0", "d": "0 0 1"}
    def stl(faces):
        return "solid x\n" + "".join(f"facet normal 0 0 1\nouter loop\nvertex {v[p]}\nvertex {v[q]}\nvertex {v[r]}\nendloop\nendfacet\n" for p, q, r in faces) + "endsolid x\n"
    tet = [("a", "c", "b"), ("a", "b", "d"), ("b", "c", "d"), ("c", "a", "d")]
    f = tmp_path / "s.stl"
    f.write_text(stl(tet))
    assert m.closed_solid(f) == (True, 4)
    f.write_text(stl(tet[:3]))
    assert m.closed_solid(f)[0] is False


def test_fritzing_part_round_trip_and_agreement(tmp_path):
    from edaloop import fritzing as f

    parts = [{"ref": "U1", "x": 5.0, "y": 5.0, "w": 2.0, "h": 2.0, "rot": 0.0, "back": False}]
    pins = [{"pad": "J1.1", "net": "VIN", "x": 1.0, "y": 2.0, "w": 1.0, "h": 1.0}, {"pad": "J1.2", "net": "GND", "x": 3.54, "y": 2.0, "w": 1.0, "h": 1.0}]
    path = f.write_part("demo", [0.0, 0.0, 10.0, 8.0], parts, pins, tmp_path)
    back = f.read_part(path)
    assert back["title"] == "demo" and [c["name"] for c in back["connectors"]] == ["VIN", "GND"]
    assert back["size_mm"] == [10.0, 8.0] and back["pads"]["connector1"] == pytest.approx([3.54, 2.0], abs=0.01)
    # Pin names survive KiCad's marks and common spellings, and a pin the board lacks is named.
    nets = {"~{RESET}": [], "UNK3V3": [], "GND": [], "3.3V": [], "CS_5V": [], "unconnected-(U1-Pad3)": []}
    part = {"connectors": [{"name": "RST"}, {"name": "RESET"}, {"name": "3V"}, {"name": "GND"}, {"name": "SDA"}, {"name": "CS"}]}
    agree = f.pin_agreement(part, nets)
    assert agree["found"] == 4 and agree["missing"] == ["RST", "SDA"]
    # Pads agree whatever way round the part was drawn, and a wrong layout doesn't.
    board = [[10.0, 10.0], [12.54, 10.0], [15.08, 10.0]]
    assert f.pad_agreement([[0.0, 0.0], [0.0, 2.54], [0.0, 5.08]], board)["share"] == 1.0
    assert f.pad_agreement([[0.0, 0.0], [7.0, 7.0], [20.0, 1.0]], board)["share"] < 0.5


def test_withdrawn_waiver_makes_evidence_stale(tmp_path):
    p = make(tmp_path, "waivers:\n  - {match: 'project_files:*', by: Pat, reason: reviewed}\n")
    p.cfg["gate"] = ["project_files"]
    ev = checks.run_check(p, "project_files")
    assert ev.status == "pass" and ev.findings
    assert checks.withdrawn_waivers(p, ev) == []
    assert checks.gate_state(p)[0]["state"] == "pass"
    p.cfg["waivers"] = []
    assert checks.withdrawn_waivers(p, ev)
    row = checks.gate_state(p)[0]
    assert row["state"] == "stale" and "no longer in edaloop.yaml" in row["summary"]
    assert not controller.status(p)["ready_to_release"]
    assert controller.next_action(p)["action"] == "run_check"


def test_impedance_section_names_the_pad_it_sits_on(tmp_path, monkeypatch):
    class Fake:
        def __init__(self, path):
            pass

        def all_pads(self):
            return [{"name": "L1.2", "net": "OUT", "x": 13.0, "y": 29.86}, {"name": "J1.1", "net": "ANT", "x": 13.0, "y": 30.0}]

    monkeypatch.setattr(checks, "Board", Fake)
    p = make(tmp_path)
    assert checks._pad_land(p, "OUT", 13000, -30115.7) == ", on the land of L1.2"
    assert checks._pad_land(p, "OUT", 20000, -30115.7) == ""


def test_released_kicad_file_is_read_before_any_upgrade(tmp_path):
    from edaloop import evaluate

    f = tmp_path / "old.kicad_pcb"
    f.write_text("""(kicad_pcb (version 4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (setup (trace_clearance 0.1524) (trace_min 0.2) (edge_clearance 0.1))
  (net 0 "") (net 1 GND) (net 2 RF)
  (net_class Default "x" (clearance 0.127) (trace_width 0.1524))
  (module a (layer F.Cu) (fp_text reference P1 (at 0 0) (layer F.SilkS))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 2 RF)))
  (module b (layer F.Cu) (fp_text reference C1 (at 0 0) (layer F.SilkS))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 2 RF))
    (pad 2 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 1 GND)))
  (segment (start 0 0) (end 1 0) (width 0.2) (layer F.Cu) (net 2))
  (via (at 1 0) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2)))
""")
    facts = evaluate.read_kicad(f)
    assert facts["tracks"] == 1 and facts["vias"] == 1 and facts["copper_layers"] == 2
    assert facts["groups"] == [frozenset({"P1.1", "C1.1"})] and facts["elements"] == {"P1", "C1"}
    assert facts["rules"] == {"clearance": 0.127, "width": 0.1524, "edge": 0.1}


def test_unstated_rf_path_reads_as_info_not_as_none(tmp_path, monkeypatch):
    from edaloop import evaluate

    def refuse(*a):
        raise evaluate.described.DescribeError("too many routes")

    monkeypatch.setattr(evaluate.described, "describe", refuse)
    rows, d = evaluate.describe_score({"known": {"band_mhz": [1, 2]}}, tmp_path, "x")
    assert rows[0]["result"] == "info" and d is None
    rows, _ = evaluate.describe_score({"known": {"rf_path": ["J1", "J2"]}}, tmp_path, "x")
    assert rows[0]["result"] == "gap"
    rows, _ = evaluate.describe_score({"known": {"rf_path": None}}, tmp_path, "x")
    assert rows[0]["result"] == "pass"


def _track(net, a, b, width=0.3, layer="F.Cu", kind="segment"):
    from types import SimpleNamespace
    return SimpleNamespace(net=net, start=a, end=b, width=width, layer=layer, kind=kind)


def _pad(ref, pad, net, x, y, w=0.6, h=0.6):
    return {"ref": ref, "pad": pad, "net": net, "x": x, "y": y, "w": w, "h": h, "rot": 0.0, "front": True, "back": False}


def test_makers_ground_patterns_and_fence_pitch():
    from edaloop import rf_rules

    assert rf_rules.land_pattern("PSA4-5043+")["pins"] == {"2": 2, "4": 2}
    assert rf_rules.land_pattern("RLM-63-2W+")["under"]
    assert rf_rules.land_pattern("0603HP-12N") is None and rf_rules.land_pattern(None) is None
    assert rf_rules.fence_pitch(1.2) == pytest.approx(1.778)          # 70 mil holds through C-band
    assert rf_rules.fence_pitch(10.0) < 1.0                            # and a higher band tightens it


def test_a_real_capacitor_has_inductance():
    from edaloop import rf_rules

    assert rf_rules.capacitance_f("4.7pF") == pytest.approx(4.7e-12) and rf_rules.capacitance_f("100nF") == pytest.approx(1e-7)
    assert rf_rules.capacitance_f("1uF") == pytest.approx(1e-6) and rf_rules.capacitance_f("0R") is None and rf_rules.capacitance_f(None) is None
    # An ideal 20 pF would look like a better block across C-band, but with a chip's inductance it's above its resonance there.
    assert rf_rules.srf_ghz(4.7e-12, 0.35) == pytest.approx(3.92, abs=0.02)
    assert rf_rules.series_cap(20e-12, 0.35, 4.2e9) > 6
    assert abs(rf_rules.series_cap(4.7e-12, 0.35, 4.2e9)) < 1.5
    assert rf_rules.series_rl_db(0.0) > 100 and rf_rules.series_rl_db(50.0) < 10


def test_fence_stretch_is_measured_per_side():
    from edaloop import rf_rules

    line = _track("RF", (0.0, 0.0), (10.0, 0.0))
    both = [(x, s * 0.8) for x in (2.0, 4.0, 6.0, 8.0) for s in (1, -1)]
    assert rf_rules.fence_stretches([line], both, {"RF"}, 1.3)["RF"] == 2.0
    one_side = [(x, 0.8) for x in (2.0, 4.0, 6.0, 8.0)]
    assert rf_rules.fence_stretches([line], one_side, {"RF"}, 1.3)["RF"] == 10.0
    assert rf_rules.fence_stretches([line], [(5.0, 3.0)], {"RF"}, 1.3)["RF"] == 10.0      # too far off to fence the line


def test_parallel_run_and_top_layer_run():
    from edaloop import rf_rules

    rf = _track("RF", (0.0, 0.0), (10.0, 0.0))
    beside = _track("DATA", (1.0, 0.6), (6.0, 0.6), 0.15)
    away = _track("OTHER", (1.0, 5.0), (6.0, 5.0), 0.15)
    inner = _track("INNER", (1.0, 0.6), (6.0, 0.6), 0.15, "In2.Cu")
    crossing = _track("CROSS", (5.0, -3.0), (5.0, 3.0), 0.15)
    runs = rf_rules.parallel_runs([rf, beside, away, inner, crossing], {"RF"}, 1.0)
    assert runs == {("DATA", "RF"): pytest.approx(5.0)}
    join = [_track("V", (0.0, 0.0), (3.0, 0.0)), _track("V", (3.0, 0.0), (5.0, 0.0)), _track("V", (9.0, 0.0), (10.0, 0.0)), _track("V", (0.0, 1.0), (9.0, 1.0), layer="In2.Cu")]
    assert rf_rules.longest_top_run(join, "V") == (6.0, 5.0)


def test_audit_finds_what_drc_cannot(tmp_path):
    from edaloop import rf_rules

    pads = [_pad("J1", "1", "A", 0.0, 0.0), _pad("U1", "1", "A", 10.0, 0.0), _pad("U1", "2", "GND", 10.0, 1.0), _pad("U1", "3", "OUT", 12.0, 1.0), _pad("U1", "4", "GND", 12.0, 0.0),
            _pad("L1", "1", "A", 5.0, 6.0), _pad("L1", "2", "BIAS", 5.0, 8.0), _pad("C1", "1", "BIAS", 5.0, 18.0), _pad("C1", "2", "GND", 5.0, 19.0),
            _pad("C2", "1", "BIAS", 6.0, 8.5), _pad("C2", "2", "GND", 6.0, 9.5)]
    facts = {"pads": pads, "courtyards": [{"ref": "C1", "value": "3.3pF"}, {"ref": "C2", "value": "100nF"}]}
    tracks = [_track("A", (0.0, 0.0), (10.0, 0.0)), _track("V", (4.0, 7.0), (6.0, 7.0)), _track("V", (11.0, -1.0), (11.0, 2.0), layer="In2.Cu")]
    rails = [{"net": "V"}]
    parts = {"U1": {"value": "limiter", "mpn": "RLM-63-2W+"}}
    metrics, found = rf_rules.audit(tracks, facts, [("A", "J1.1", "U1.1")], rails, parts, (1.0, 2.0), "In1.Cu", {"A": {"coverage": 0.67, "at": [1.0, 2.0]}}, 4)
    ids = {f[0]: f[2] for f in found}
    assert ids["rf_layout:plane:A"] == "error"
    assert "rf_layout:pad_gap:L1" in ids and "rf_layout:limiter:U1" in ids and "rf_layout:tap:L1.1" in ids
    assert "rf_layout:bypass_far:L1" in ids and "rf_layout:bypass_order:L1" in ids
    assert "rf_layout:ground_pattern" in ids and metrics["ground_pattern_short"]
    clean, none = rf_rules.audit([_track("A", (0.0, 0.0), (1.0, 0.0))], {"pads": [_pad("J1", "1", "A", 0.0, 0.0), _pad("J2", "1", "A", 1.0, 0.0)]}, [("A", "J1.1", "J2.1")], [], {}, (1.0, 2.0), "In1.Cu", {"A": {"coverage": 1.0, "at": [0, 0]}}, 4)
    assert none == [] and clean["plane_coverage_min"] == 1.0


def test_a_shunt_inductor_to_ground_has_no_bypass_to_check():
    from edaloop import rf_rules

    pads = [_pad("J1", "1", "A", 0, 0), _pad("J2", "1", "A", 9, 0), _pad("L1", "1", "A", 4, 0.5), _pad("L1", "2", "GND", 4, 2), _pad("C1", "1", "VDD", 20, 20), _pad("C1", "2", "GND", 20, 21)]
    metrics, found = rf_rules.audit([], {"pads": pads, "courtyards": [{"ref": "C1", "value": "3.3pF"}]}, [("A", "J1.1", "J2.1")], [], {}, (1.0, 2.0), "In1.Cu", {}, 4)
    assert metrics["bypass"] == {} and not [f for f in found if "bypass" in f[0]]


def test_filters_tied_at_a_node_are_named():
    from edaloop import rf_rules

    pads = [_pad("FL1", "1", "SUM", 0, 0), _pad("FL2", "1", "SUM", 1, 0), _pad("FL3", "1", "SUM", 2, 0), _pad("J1", "1", "SUM", 3, 0)]
    _, found = rf_rules.audit([], {"pads": pads}, [("SUM", "J1.1", "FL1.1")], [], {}, (1.0, 2.0), "In1.Cu", {}, 4)
    assert any(f[0] == "rf_layout:tee:SUM" for f in found)


def test_maker_patterns_only_ground_real_ground_pads():
    from edaloop import ground

    pads = [_pad("U2", "1", "RFIN", 0, 0), _pad("U2", "2", "GND", 1, 0), _pad("U2", "3", "RFOUT", 2, 0), _pad("U2", "4", "GND", 3, 0),
            _pad("U1", "1", "A", 0, 5), _pad("U1", "6", "GND", 2, 5), _pad("U1", "3", "GND", 0, 7), _pad("U1", "4", "B", 2, 7)]
    bench = {"parts": {"U2": {"mpn": "PSA4-5043+"}, "U1": {"mpn": "RLM-63-2W+"}}}
    got = ground.maker_patterns(bench, {"pads": pads}, {"per_pad": {"U2.4": 3}})
    assert got["counts"]["U2.2"] == 2 and got["counts"]["U2.4"] == 3     # the description wins
    assert "U2.1" not in got["counts"] and got["counts"]["U1.6"] == 2
    where = [at for name, at in got["under"]]
    assert (2.0, 0.0) in where and (1.0, 5.0) in where                    # a limiter's row runs between RF and ground pads


def test_layer_costs_reach_the_router():
    assert route_classes_args(["F.Cu", "In2.Cu"], {"F.Cu": 6.0}) == ["--layers", "F.Cu", "In2.Cu", "--layer-costs", "6.0", "1.0"]
    assert route_classes_args(["F.Cu"], None) == ["--layers", "F.Cu"] and route_classes_args([], {"F.Cu": 6.0}) == []


def route_classes_args(layers, costs):
    from edaloop import route_classes
    return route_classes._layer_args(layers, costs)


def test_rf_path_script_ships_with_edaloop(tmp_path):
    import subprocess
    import sys

    board = tmp_path / "b.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20240108) (generator test)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "RF")
  (footprint "a" (layer "F.Cu") (at 0 0)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "RF")))
  (footprint "b" (layer "F.Cu") (at 10 0)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "RF")))
  (segment (start 0 0) (end 12 0) (width 0.3) (layer "F.Cu") (net "RF"))
  (via (at 12 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "RF")))
""")
    chains = tmp_path / "chains.yaml"
    chains.write_text('chains:\n  - {net: RF, entry: J1.1, exit: U1.1}\n')
    scripts = Path(checks.__file__).with_name("scripts")
    out = subprocess.run([sys.executable, "-c", checks._RF_PATH_DRIVER, str(scripts), str(board), str(chains)], capture_output=True, text=True, check=True)
    row = json.loads(out.stdout)[0]
    assert row["floor"] == pytest.approx(10.0) and row["routed"] == pytest.approx(12.0) and row["vias"] == 1 and row["off"] == 0.0
    assert "skills" not in str(scripts)


def test_kicad6_board_keeps_its_geometry_in_the_older_syntax(tmp_path):
    from edaloop import export_kicad6

    board = tmp_path / "new.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (net 0 "") (net 1 "RF")
  (segment (start 0 0) (end 12 0) (width 0.3) (layer "F.Cu") (net "RF") (uuid "11111111-2222-3333-4444-555555555555"))
  (via (at 12 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "RF") (uuid "11111111-2222-3333-4444-666666666666")))
""")
    out = tmp_path / "old.kicad_pcb"
    export_kicad6.convert_pcb(str(board), str(out))
    text = out.read_text()
    assert "generator_version" not in text and "(uuid" not in text and "tstamp" in text
    before, after = export_kicad6.pcb_geometry(str(board)), export_kicad6.pcb_geometry(str(out))
    assert sum(before.values()) == sum(after.values()) == 2


def test_kicad6_library_tables_have_no_version_line():
    from edaloop import export_kicad6

    table = export_kicad6._table("fp_lib_table", ["RF_FP"], ".pretty")
    assert "(version" not in table and '${KIPRJMOD}/RF_FP.pretty' in table


def test_export_says_when_a_format_cant_run(tmp_path):
    from edaloop import export

    p = make(tmp_path)
    p.cfg.pop("schematic", None)
    done = export.run(p, ["kicad6"], tmp_path / "out")
    assert done["ok"] is False and "no schematic" in done["formats"]["kicad6"]["problems"][0]
    with pytest.raises(ValueError):
        export.run(p, ["altium"], tmp_path / "out")


def test_eagle_exporter_ships_with_its_dtd():
    from edaloop import export

    assert export.EAGLE.exists()
    dtd = export.EAGLE.with_name("data") / "eagle-7.7.0.dtd"
    assert "CC BY-ND 3.0" in dtd.read_text()
    source = export.EAGLE.read_text()
    assert "/Users/" not in source and 'CLI = sys.argv[4]' in source


# ---------------------------------------------------------------- evidence goes stale when anything it read changes

CHAINS = 'chains:\n  - {net: RF, entry: J1.1, exit: J2.1}\n'
PROFILE = """manufacturer: {name: Test fab}
published_capabilities:
  mechanical_drill: {minimum_in: 0.008, preferred_design_mm: 0.3}
  annular_ring: {class_2_min_in: 0.002}
engineering_defaults: {min_trace_mm: 0.1}
"""


def _rules_project(tmp_path):
    placed = PCB.replace('(footprint "x"', '(footprint "x" (at 0 0)').replace(
        'smd rect (net', 'smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net')
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack" / "fab.yaml").write_text(PROFILE)
    (tmp_path / "c.yaml").write_text(CHAINS)
    p = make(tmp_path, f"rf: {{chains: c.yaml}}\nrules: {{pack: {tmp_path / 'pack'}, profile: fab.yaml}}\n")
    (tmp_path / "b.kicad_pcb").write_text(placed)
    return p


def test_changing_the_rule_profile_makes_rules_stale(tmp_path):
    p = _rules_project(tmp_path)
    ev = checks.run_check(p, "rules")
    assert ev.status != "error" and checks.current_inputs(p, ev)
    (tmp_path / "pack" / "fab.yaml").write_text(PROFILE.replace("min_trace_mm: 0.1", "min_trace_mm: 0.5"))
    assert not checks.current_inputs(p, ev)


def test_changing_rf_settings_or_chains_makes_rules_stale(tmp_path):
    p = _rules_project(tmp_path)
    ev = checks.run_check(p, "rules")
    assert ev.status != "error"
    p.cfg["rf"]["impedance"] = {"max_freq_ghz": 6}
    assert not checks.current_inputs(p, ev)
    p.cfg["rf"].pop("impedance")
    assert checks.current_inputs(p, ev)
    (tmp_path / "c.yaml").write_text(CHAINS + '  - {net: RF2, entry: J2.1, exit: J3.1}\n')
    assert not checks.current_inputs(p, ev)


@pytest.mark.parametrize("name", ["rf_reference", "rf_preserved"])
def test_changing_the_chain_list_makes_rf_checks_stale(tmp_path, name):
    (tmp_path / "c.yaml").write_text(CHAINS)
    p = make(tmp_path, "rf: {chains: c.yaml}\n")
    ev = checks.run_check(p, name)
    assert ev.status == "pass" and checks.current_inputs(p, ev)
    (tmp_path / "c.yaml").write_text(CHAINS + '  - {net: GND, entry: J2.2, exit: J3.2}\n')
    assert not checks.current_inputs(p, ev)


def test_deleted_rf_baseline_is_not_quietly_re_recorded(tmp_path):
    (tmp_path / "c.yaml").write_text(CHAINS)
    p = make(tmp_path, "rf: {chains: c.yaml}\n")
    assert "baseline recorded" in checks.run_check(p, "rf_preserved").summary
    # A router moves RF copper, and the baseline that would have caught it goes missing.
    (tmp_path / "b.kicad_pcb").write_text(PCB.replace("(end 10 5)", "(end 10 6)"))
    (tmp_path / "rf_baseline.json").unlink()
    for _ in range(2):                          # running it again mustn't clear it either
        ev = checks.run_check(p, "rf_preserved")
        assert ev.status == "fail" and any(f["id"] == "rf_preserved:baseline_changed" for f in ev.findings)
    # A person can accept the new baseline, on the record.
    p.cfg["waivers"] = [{"match": "rf_preserved:baseline_changed", "by": "Pat", "reason": "re-laid the output match"}]
    assert checks.run_check(p, "rf_preserved").status == "pass"


def test_an_input_that_appears_later_makes_evidence_stale(tmp_path):
    p = make(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    ev = checks.run_check(p, "project_files")
    assert ev.status == "warn" and ev.inputs["fp-lib-table"] == checks.ABSENT
    assert checks.current_inputs(p, ev)
    (tmp_path / "fp-lib-table").write_text("")
    assert not checks.current_inputs(p, ev)


def test_drc_settings_are_inputs(tmp_path):
    p = make(tmp_path)
    ev = Evidence("drc", "pass", "", {"drc_config": checks._config(p, "drc_config")})
    assert checks.current_inputs(p, ev)
    p.cfg["drc"] = {"refill_zones": False}
    assert not checks.current_inputs(p, ev)


def test_unknown_or_unreadable_inputs_are_stale(tmp_path):
    p = make(tmp_path)
    assert not checks.current_inputs(p, Evidence("x", "pass", "", {"no_such_key": "abc"}))
    p.cfg["rules"] = {"pack": str(tmp_path / "gone"), "profile": "fab.yaml"}
    assert not checks.current_inputs(p, Evidence("rules", "pass", "", {"rule_profile": "abc"}))


def test_every_config_digest_can_be_worked_out(tmp_path):
    p = make(tmp_path)
    skip = {"rule_profile", "spice_config"}      # these need a rule pack and kicad-cli
    for key in set(checks.CONFIG_DIGESTS) - skip:
        checks._config(p, key)


def test_requirement_goes_stale_when_its_evidence_does(tmp_path):
    p = make(tmp_path, "requirements:\n  - {id: R1, metric: project_files.x, max: 1}\n")
    p.ledger.append(Evidence("project_files", "pass", "", {"b.kicad_pcb": sha_file(tmp_path / "b.kicad_pcb")}, {"x": 0}))
    ev = checks.run_check(p, "requirements")
    assert ev.status == "pass" and checks.current_inputs(p, ev)
    p.ledger.append(Evidence("project_files", "pass", "", {"b.kicad_pcb": sha_file(tmp_path / "b.kicad_pcb")}, {"x": 5}))
    assert not checks.current_inputs(p, ev)     # newer evidence for the metric
    ev = checks.run_check(p, "requirements")
    assert ev.status == "fail"
    with open(tmp_path / "b.kicad_pcb", "a") as f:
        f.write("\n")
    assert not checks.current_inputs(p, ev)     # the evidence it read went stale


# ---------------------------------------------------------------- the gate

def test_an_empty_gate_never_releases(tmp_path):
    p = make(tmp_path)
    p.cfg["gate"] = []
    assert not controller.status(p)["ready_to_release"]
    assert controller.next_action(p)["action"] == "ask_person"
    assert controller.run_all(p) == []
    assert not checks.release(p)["released"] and not (tmp_path / "release").exists()


def test_a_gated_check_that_does_not_exist_blocks(tmp_path):
    p = make(tmp_path)
    p.cfg["gate"] = ["project_files", "no_such_check"]
    rows = checks.gate_state(p)
    assert rows[1]["state"] == "unknown"
    assert not checks.release(p)["released"]


def test_release_refused_if_the_board_changes_during_export(tmp_path, monkeypatch):
    p = make(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "fp-lib-table").write_text("")
    assert checks.run_check(p, "project_files").status == "pass"

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def edit_while_exporting(cmd, **kw):
        with open(tmp_path / "b.kicad_pcb", "a") as f:
            f.write("\n")
        return Done()

    monkeypatch.setattr(toolchain, "paths", lambda: {"kicad_cli": "kicad-cli"})
    monkeypatch.setattr(toolchain, "run", edit_while_exporting)
    result = checks.release(p)
    assert not result["released"] and result["blockers"][0]["state"] == "stale"
    assert not (tmp_path / "release").exists() and not (tmp_path / "t_release.zip").exists()


def test_rf_baseline_lives_beside_the_project_and_survives_a_lost_ledger(tmp_path):
    (tmp_path / "c.yaml").write_text(CHAINS)
    p = make(tmp_path, "rf: {chains: c.yaml}\n")
    checks.run_check(p, "rf_preserved")
    assert (tmp_path / "rf_baseline.json").exists()
    # A fresh clone has no .edaloop/, but the committed baseline still holds the copper.
    shutil.rmtree(tmp_path / ".edaloop")
    (tmp_path / "b.kicad_pcb").write_text(PCB.replace("(end 10 5)", "(end 10 6)"))
    ev = checks.run_check(Project(tmp_path), "rf_preserved")
    assert ev.status == "fail" and any(f["id"] == "rf_preserved:RF:lost" for f in ev.findings)


def test_a_baseline_in_the_old_place_moves_without_counting_as_changed(tmp_path):
    (tmp_path / "c.yaml").write_text(CHAINS)
    p = make(tmp_path, "rf: {chains: c.yaml}\n")
    checks.run_check(p, "rf_preserved")
    old = tmp_path / ".edaloop" / "rf_baseline.json"
    (tmp_path / "rf_baseline.json").rename(old)
    # Rewrite the earlier evidence as an older edaloop recorded it, under the old path.
    ledger = p.ledger.path
    ledger.write_text(ledger.read_text().replace('"rf_baseline.json"', '".edaloop/rf_baseline.json"'))
    ev = checks.run_check(p, "rf_preserved")
    assert ev.status == "pass", ev.findings
    assert (tmp_path / "rf_baseline.json").read_bytes() == old.read_bytes()


# ---------------------------------------------------------------- exports

def _export_eagle(monkeypatch):
    return _under_kicad_python(monkeypatch, "export_eagle")


def _under_kicad_python(monkeypatch, name):
    """Scripts that run under KiCad's Python import pcbnew. A stand-in lets their pure parts be tested anywhere."""
    import importlib.util
    import sys
    import types
    class Pcbnew(types.ModuleType):
        def __getattr__(self, name):           # layer ids and other constants: any distinct number will do
            return hash(name) % 1000

    fake = Pcbnew("pcbnew")
    fake.ToMM = lambda v: v / 1e6
    monkeypatch.setitem(sys.modules, "pcbnew", fake)
    monkeypatch.syspath_prepend(str(Path(checks.__file__).parent))
    path = Path(checks.__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eagle_export_reads_the_boards_clearance(monkeypatch):
    ee = _export_eagle(monkeypatch)

    class NetClass:
        def GetClearance(self):
            return 150_000                     # nm

    class Settings:
        class m_NetSettings:
            @staticmethod
            def GetDefaultNetclass():
                return NetClass()

    class Board:
        def GetDesignSettings(self):
            return Settings()

    report = {}
    assert ee.clearance_mm(Board(), report) == pytest.approx(0.15)
    assert report == {}


def test_eagle_export_marks_an_assumed_clearance(monkeypatch):
    ee = _export_eagle(monkeypatch)

    class Board:
        def GetDesignSettings(self):
            return object()                    # no route to a clearance

    report = {}
    assert ee.clearance_mm(Board(), report) == ee.ASSUMED_CLEARANCE_MM
    assert any(k.startswith("ASSUMED") for k in report)


SCH_V10 = """(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0")
 (uuid "aaaa-root") (paper "A4")
 (lib_symbols)
 (symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
  (fields_autoplaced yes)
  (uuid "bbbb-r1")
  (property "Reference" "R1" (at 10 8 0) (effects (font (size 1.27 1.27))))
  (property "Value" "49.9" (at 10 12 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "R_0402" (at 10 12 0) (hide yes) (effects (font (size 1.27 1.27))))
  (property "MPN" "RC0402" (at 10 12 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (instances (project "demo" (path "/aaaa-root" (reference "R1") (unit 1))))))
"""


def test_kicad6_schematic_moves_instances_and_speaks_only_6_0(tmp_path):
    from edaloop import export_kicad6 as k6
    src, out = tmp_path / "new.kicad_sch", tmp_path / "old.kicad_sch"
    src.write_text(SCH_V10)
    k6.convert_sch(str(src), str(out), k6.root_uuid_of(str(src)))
    text = out.read_text()
    assert k6.conformance(str(out)) == {}                         # no token 6.0 never had
    assert "(version 20211123)" in text and "(uuid bbbb-r1)" in text and "(generator eeschema)" in text
    node, _ = k6.read(text)
    # The instance moves to the end, keyed by the SYMBOL's path, without the root sheet.
    rows = k6.kids(k6.kid(node, "symbol_instances"), "path")
    assert [str(r[1]) for r in rows] == ["/bbbb-r1"]
    assert str(k6.kid(rows[0], "reference")[1]) == "R1" and str(k6.kid(rows[0], "footprint")[1]) == "R_0402"
    sym = k6.kid(node, "symbol")
    assert k6.kid(sym, "instances") is None
    props = {str(p[1]): p for p in k6.kids(sym, "property")}
    assert [str(k6.kid(props[n], "id")[1]) for n in ("Reference", "Value", "Footprint")] == ["0", "1", "2"]
    # hide is a bare flag inside effects, wherever 10.0 put it.
    for name in ("Footprint", "MPN"):
        assert "hide" in [str(x) for x in k6.kid(props[name], "effects")]
        assert k6.kid(props[name], "hide") is None
    assert "(fields_autoplaced)" in text and "(fields_autoplaced yes)" not in text


@pytest.mark.parametrize("value, farads", [("100n", 100e-9), ("100nF", 100e-9), ("100n 50V", 100e-9), ("4u7", 4.7e-6),
                                           ("1\u00b5F", 1e-6), ("2.2uF", 2.2e-6), ("10p", 10e-12), ("DNP", 1e9)])
def test_bypass_capacitor_values_are_read_as_written(monkeypatch, value, farads):
    ops = _under_kicad_python(monkeypatch, "kicad_ops")
    assert ops._farads(value) == pytest.approx(farads)


# ---------------------------------------------------------------- staleness, across every check that runs here

OFFLINE = ["project_files", "rf_reference", "rf_path", "rf_preserved", "power", "thermal_field", "rules", "cascade", "requirements"]


@pytest.fixture
def offline_project(tmp_path):
    p = _rules_project(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}")
    p.cfg["cascade"] = {"stages": [{"name": "amp", "gain_db": [[1, 20.0]], "nf_db": [[1, 1.0]]}], "freq_mhz": [1]}
    p.cfg["requirements"] = [{"id": "R1", "metric": "rules.min_track_mm", "min": 0.1}]
    for name in OFFLINE:
        checks.run_check(p, name)
    return p


@pytest.mark.parametrize("name", OFFLINE)
def test_each_check_goes_stale_when_any_file_it_recorded_changes(offline_project, name):
    p = offline_project
    ev = p.ledger.latest()[name]
    assert ev.status != "error", ev.summary           # the list above must keep running without tools
    # Recording and recomputing agree, so fresh evidence reads as current.
    assert checks.current_inputs(p, ev)
    files = [k for k in ev.inputs if k not in checks.CONFIG_DIGESTS]
    # cascade with no line loss reads only its stage table, so it records no file.
    assert files or name == "cascade", f"{name} recorded no files"
    for key in files:
        path = p.root / key
        before = path.read_bytes() if path.exists() else None
        if before is None:
            path.write_text("")                         # an input that appears
        else:
            path.write_bytes(before + b"\n")            # an input that changes
        try:
            assert not checks.current_inputs(p, ev), f"{name} stayed current after {key} changed"
        finally:
            if before is None:
                path.unlink()
            else:
                path.write_bytes(before)
        assert checks.current_inputs(p, ev), f"restoring {key} didn't make {name} current again"


@pytest.mark.parametrize("script", ["kicad_ops.py", "export_eagle.py", "channel_groups.py", "pcbfile.py"])
def test_scripts_for_kicads_python_stay_python_3_9(script):
    import ast
    # KiCad ships its own Python 3.9, and these run under it. CI's Python is newer and would accept 3.10 syntax.
    ast.parse(Path(checks.__file__).with_name(script).read_text(), feature_version=(3, 9))


# ---------------------------------------------------------------- the chain spec is data, not code

def test_a_python_chain_spec_is_refused_not_run(tmp_path):
    from edaloop.scripts.kicad_pcb import ChainSpecError, load_chains
    marker = tmp_path / "ran"
    (tmp_path / "c.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nCHAINS = []\n")
    with pytest.raises(ChainSpecError, match="convert-chains"):
        load_chains(tmp_path / "c.py")
    assert not marker.exists()
    p = make(tmp_path, "rf: {chains: c.py}\n")
    ev = checks.run_check(p, "rf_reference")
    assert ev.status == "error" and "convert-chains" in ev.summary and not marker.exists()


@pytest.mark.parametrize("body, why", [
    ("chains: []\n", "non-empty"),
    ("chains:\n  - {net: RF, entry: J1, exit: J2.1}\n", "REF.PIN"),
    ("chains:\n  - {net: RF, entry: J1.1}\n", "net, entry and exit"),
    ("chains:\n  - {net: RF, entry: J1.1, exit: J2.1}\nwidth: 0.3\n", "unknown key"),
])
def test_a_malformed_chain_spec_says_what_is_wrong(tmp_path, body, why):
    from edaloop.scripts.kicad_pcb import ChainSpecError, load_chains
    (tmp_path / "c.yaml").write_text(body)
    with pytest.raises(ChainSpecError, match=why):
        load_chains(tmp_path / "c.yaml")


def test_convert_chains_reads_back_the_same_and_never_overwrites(tmp_path):
    from edaloop.scaffold import convert_chains
    from edaloop.scripts.kicad_pcb import load_chains
    old = tmp_path / "chains_demo.py"
    old.write_text('WIDTH = 0.36\nNET_WIDTH = {"BIAS": 0.3}\n'
                   'CHAINS = [("RF", ("J1", "1"), ("U1", "2")), ("BIAS", ("L1", "2"), ("R1", "2"))]\n')
    done = convert_chains(old)
    assert done["written"].endswith("chains_demo.yaml") and done["chains"] == 2
    assert load_chains(tmp_path / "chains_demo.yaml") == (
        [("RF", ("J1", "1"), ("U1", "2")), ("BIAS", ("L1", "2"), ("R1", "2"))], 0.36, {"BIAS": 0.3})
    with pytest.raises(FileExistsError):
        convert_chains(old)


def test_init_points_at_a_python_chain_spec_it_cannot_use(tmp_path):
    (tmp_path / "chains.py").write_text("CHAINS = []\n")
    assert init(tmp_path)["convert"] == ["edaloop convert-chains chains.py"]


def test_a_missing_tool_is_named_not_a_raw_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(toolchain, "paths", lambda: {"circuitrf": str(tmp_path / "nope" / "CircuitRF.Cli")})
    with pytest.raises(toolchain.ToolMissing, match="circuitrf isn't installed.*edaloop doctor"):
        toolchain.run([str(tmp_path / "nope" / "CircuitRF.Cli"), "--version"])
    # Still a FileNotFoundError, so handlers written for that keep working.
    assert issubclass(toolchain.ToolMissing, FileNotFoundError)
    # Inside a check it's an error with the message, never a pass.
    monkeypatch.setitem(checks.REGISTRY["project_files"], "fn",
                        lambda p: toolchain.run([str(tmp_path / "nope" / "CircuitRF.Cli")]))
    p = make(tmp_path)
    ev = checks.run_check(p, "project_files")
    assert ev.status == "error" and "circuitrf isn't installed" in ev.summary
