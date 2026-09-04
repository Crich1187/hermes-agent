"""Handler-level tests for the ``/rollback`` command (root-myx Gate 3 M1a).

The prior candidate validated ``CheckpointManager`` directly. That left the
*command* layer -- which is what the bead's AC actually names -- untested: a
regression in argument parsing, checkpoint-reference resolution, the guards, the
diff-truncation branch, dispatch, or the documented chat-turn side effect would
have left the suite green.

These tests drive the **real production functions** --
``cli.HermesCLI._handle_rollback_command`` and
``gateway.run.GatewayRunner._handle_rollback_command`` -- unbound, against a
stub ``self`` and a stubbed ``CheckpointManager``. Nothing here imports a live
agent, starts a gateway, or touches a real checkpoint store: ``TERMINAL_CWD`` is
pinned to a ``tmp_path`` and every manager call is recorded on a fake.

Coverage map (the four documented forms plus the surrounding logic):

    /rollback                 -> list
    /rollback <N>             -> restore + chat-turn undo
    /rollback diff <N>        -> diff, including the 80-line truncation branch
    /rollback <N> <file>      -> single-file restore

    guards: no active agent; checkpoints not enabled
    resolution: 1-indexed number, out-of-range, bare hash passthrough
    dispatch/output: what the handler prints / returns in each case

Build provenance (root-myx Gate 3 M1b / m1 / m2) -- read this before trusting
these tests as assurance about the *running* system:

* CANONICAL, TESTED build = this repository line (`fork/main` and its
  descendants). These tests execute the handler source read out of THIS tree.
* DEPLOYED build on this host at the time of writing = a DIFFERENT, dirty
  checkout: `/root/.hermes/hermes-agent-0182`, HEAD `ab2cd36a` on branch
  `fix/remarkdown-oauth-gateway-token`, 3 tracked files modified. That commit
  does NOT contain this candidate (the candidate is not an ancestor of it), and
  its `tools/checkpoint_manager.py` is a materially different implementation
  (sha256 `c06d3f40...` deployed vs `69c157c1...` canonical), carrying a ledger
  subsystem and a `safe_restore_plan` path absent here. It also contains
  `hermes_cli/cli_commands_mixin.py`, which does NOT exist on this line -- the
  earlier handoff cited that path while testing this tree, which is precisely
  the confusion this note exists to prevent.

  So: these tests validate the canonical line. They do NOT certify the deployed
  install. Reconciling the install onto the canonical line is out of scope for
  this bead and needs its own follow-up.

Reproducing the run: `pyproject.toml` sets
`addopts = "-m 'not integration' -n auto"`, and pytest-xdist is not installed in
either interpreter on this host, so a bare `python3 -m pytest ...` dies at
argparse with `unrecognized arguments: -n`. Use:

    python3 -m pytest tests/tools/test_rollback_command_handlers.py -o addopts=""
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------
# Import the REAL production modules and take the REAL handler functions.
#
# `cli.py` and `gateway/run.py` pull in optional terminal/CLI dependencies that
# are not installed in every interpreter on this host (prompt_toolkit, fire).
# Those are stubbed *only if genuinely absent* so the import can complete; the
# handlers under test do not touch them. Everything exercised below is the
# actual imported function object, with its real module globals -- not a copy.
# --------------------------------------------------------------------------

_OPTIONAL_STUBS = (
    "prompt_toolkit",
    "prompt_toolkit.history",
    "prompt_toolkit.styles",
    "prompt_toolkit.patch_stdout",
    "prompt_toolkit.application",
    "prompt_toolkit.layout",
    "prompt_toolkit.layout.processors",
    "prompt_toolkit.filters",
    "prompt_toolkit.layout.dimension",
    "prompt_toolkit.layout.menus",
    "prompt_toolkit.widgets",
    "prompt_toolkit.key_binding",
    "prompt_toolkit.formatted_text",
    "fire",
)


def _stub_if_absent(name: str) -> None:
    if name in sys.modules:
        return
    try:
        importlib.import_module(name)
    except Exception:
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        mod.__getattr__ = lambda _k: types.SimpleNamespace()  # type: ignore[attr-defined]
        sys.modules[name] = mod


for _name in _OPTIONAL_STUBS:
    _stub_if_absent(_name)

import cli as _cli  # noqa: E402
import gateway.run as _gwrun  # noqa: E402

CLI_HANDLER = _cli.HermesCLI._handle_rollback_command
CLI_RESOLVE = _cli.HermesCLI._resolve_checkpoint_ref
GW_HANDLER = _gwrun.GatewayRunner._handle_rollback_command

CLI_SRC = inspect.getsource(CLI_HANDLER)
GW_SRC = inspect.getsource(GW_HANDLER)


# --------------------------------------------------------------------------
# Fakes. No live agent, gateway, config file or checkpoint store is involved.
# --------------------------------------------------------------------------


class FakeManager:
    """Records calls; returns scripted results. Never touches a real store."""

    def __init__(self, checkpoints=None, enabled=True, restore=None, diff=None):
        self.enabled = enabled
        self._checkpoints = checkpoints if checkpoints is not None else []
        self._restore = restore or {"success": True, "restored_to": "abc1234", "reason": "ok"}
        self._diff = diff or {"success": True, "stat": "", "diff": ""}
        self.calls: list[tuple] = []

    def list_checkpoints(self, cwd):
        self.calls.append(("list", cwd))
        return list(self._checkpoints)

    def restore(self, cwd, target_hash, file_path=None):
        self.calls.append(("restore", cwd, target_hash, file_path))
        return dict(self._restore)

    def diff(self, cwd, target_hash):
        self.calls.append(("diff", cwd, target_hash))
        return dict(self._diff)


class FakeAgent:
    def __init__(self, mgr):
        self._checkpoint_mgr = mgr


class FakeCLI:
    """Stand-in ``self`` carrying only what the handler actually reaches for."""

    def __init__(self, mgr, *, has_agent=True, history=True):
        if has_agent:
            self.agent = FakeAgent(mgr)
        self.conversation_history = ["turn"] if history else []
        self.undo_called = 0

    def undo_last(self):
        self.undo_called += 1

    def _resolve_checkpoint_ref(self, ref, checkpoints):
        return CLI_RESOLVE(self, ref, checkpoints)


def _cps(n=3):
    return [{"hash": f"hash{i}", "message": f"cp {i}", "age": "1m"} for i in range(1, n + 1)]


@pytest.fixture
def pinned_cwd(tmp_path, monkeypatch):
    """Pin TERMINAL_CWD to a tmp dir so no real working tree is consulted."""
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    return str(tmp_path)


def _run_cli(cli, command, monkeypatch, fmt="RENDERED-LIST"):
    """Drive the CLI handler, stubbing format_checkpoint_list, capture stdout."""
    fake_mod = types.ModuleType("tools.checkpoint_manager")
    fake_mod.format_checkpoint_list = lambda cps, cwd: f"{fmt}({len(cps)})"
    monkeypatch.setitem(sys.modules, "tools.checkpoint_manager", fake_mod)
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CLI_HANDLER(cli, command)
    return buf.getvalue()


# ==========================================================================
# CLI handler — guards
# ==========================================================================


def test_cli_no_active_agent_is_refused(pinned_cwd, monkeypatch):
    cli = FakeCLI(FakeManager(), has_agent=False)
    out = _run_cli(cli, "/rollback", monkeypatch)
    assert "No active agent session." in out


def test_cli_checkpoints_disabled_is_refused_with_enable_hint(pinned_cwd, monkeypatch):
    mgr = FakeManager(enabled=False)
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback", monkeypatch)
    assert "Checkpoints are not enabled." in out
    assert "hermes --checkpoints" in out
    assert mgr.calls == [], "a disabled manager must not be queried"


# ==========================================================================
# Form 1: /rollback  -> list
# ==========================================================================


def test_cli_bare_rollback_lists_checkpoints(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback", monkeypatch)
    assert "RENDERED-LIST(3)" in out
    assert mgr.calls == [("list", pinned_cwd)]
    assert cli.undo_called == 0, "listing must not undo a chat turn"


def test_cli_bare_rollback_with_no_checkpoints_still_renders(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=[])
    out = _run_cli(FakeCLI(mgr), "/rollback", monkeypatch)
    assert "RENDERED-LIST(0)" in out


# ==========================================================================
# Form 2: /rollback <N>  -> restore + documented chat-turn side effect
# ==========================================================================


def test_cli_restore_by_number_resolves_1_indexed_and_undoes_chat_turn(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback 2", monkeypatch)
    # 1-indexed: "2" must select the SECOND checkpoint.
    assert ("restore", pinned_cwd, "hash2", None) in mgr.calls
    assert "Restored to checkpoint abc1234" in out
    assert "pre-rollback snapshot was saved automatically" in out
    assert cli.undo_called == 1, "documented side effect: last chat turn is undone"
    assert "Chat turn undone" in out


def test_cli_restore_does_not_undo_when_history_is_empty(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(2))
    cli = FakeCLI(mgr, history=False)
    out = _run_cli(cli, "/rollback 1", monkeypatch)
    assert cli.undo_called == 0
    assert "Chat turn undone" not in out


def test_cli_restore_out_of_range_number_is_refused_without_restoring(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback 9", monkeypatch)
    assert "Invalid checkpoint number. Use 1-3." in out
    assert not any(c[0] == "restore" for c in mgr.calls)
    assert cli.undo_called == 0


def test_cli_restore_accepts_a_bare_hash(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(2))
    _run_cli(FakeCLI(mgr), "/rollback deadbeef", monkeypatch)
    assert ("restore", pinned_cwd, "deadbeef", None) in mgr.calls


def test_cli_restore_with_no_checkpoints_reports_and_stops(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=[])
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback 1", monkeypatch)
    assert f"No checkpoints found for {pinned_cwd}" in out
    assert not any(c[0] == "restore" for c in mgr.calls)


def test_cli_restore_failure_is_surfaced_and_skips_undo(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1), restore={"success": False, "error": "boom"})
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback 1", monkeypatch)
    assert "boom" in out
    assert cli.undo_called == 0, "a failed restore must not undo the chat turn"


# ==========================================================================
# Form 3: /rollback diff <N>  -> including the 80-line truncation branch
# ==========================================================================


def test_cli_diff_requires_a_reference(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(2))
    out = _run_cli(FakeCLI(mgr), "/rollback diff", monkeypatch)
    assert "Usage: /rollback diff <N>" in out
    assert not any(c[0] == "diff" for c in mgr.calls)


def test_cli_diff_dispatches_with_resolved_hash(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3), diff={"success": True, "stat": "1 file", "diff": "+x"})
    out = _run_cli(FakeCLI(mgr), "/rollback diff 3", monkeypatch)
    assert ("diff", pinned_cwd, "hash3") in mgr.calls
    assert "1 file" in out and "+x" in out


def test_cli_diff_is_case_insensitive(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1), diff={"success": True, "stat": "s", "diff": ""})
    _run_cli(FakeCLI(mgr), "/rollback DIFF 1", monkeypatch)
    assert any(c[0] == "diff" for c in mgr.calls)


def test_cli_diff_reports_no_changes(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1), diff={"success": True, "stat": "", "diff": ""})
    out = _run_cli(FakeCLI(mgr), "/rollback diff 1", monkeypatch)
    assert "No changes since this checkpoint." in out


def test_cli_diff_under_80_lines_is_not_truncated(pinned_cwd, monkeypatch):
    body = "\n".join(f"line{i}" for i in range(80))
    mgr = FakeManager(checkpoints=_cps(1), diff={"success": True, "stat": "", "diff": body})
    out = _run_cli(FakeCLI(mgr), "/rollback diff 1", monkeypatch)
    assert "more lines, showing first 80" not in out
    assert "line79" in out


def test_cli_diff_over_80_lines_truncates_with_exact_remainder(pinned_cwd, monkeypatch):
    body = "\n".join(f"line{i}" for i in range(100))
    mgr = FakeManager(checkpoints=_cps(1), diff={"success": True, "stat": "", "diff": body})
    out = _run_cli(FakeCLI(mgr), "/rollback diff 1", monkeypatch)
    assert "... (20 more lines, showing first 80)" in out
    assert "line79" in out
    assert "line80" not in out, "lines past the cap must not be printed"


def test_cli_diff_failure_is_surfaced(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1), diff={"success": False, "error": "nope"})
    out = _run_cli(FakeCLI(mgr), "/rollback diff 1", monkeypatch)
    assert "nope" in out


def test_cli_diff_with_no_checkpoints_reports_and_stops(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=[])
    out = _run_cli(FakeCLI(mgr), "/rollback diff 1", monkeypatch)
    assert f"No checkpoints found for {pinned_cwd}" in out
    assert not any(c[0] == "diff" for c in mgr.calls)


# ==========================================================================
# Form 4: /rollback <N> <file>  -> single-file restore
# ==========================================================================


def test_cli_single_file_restore_passes_file_path_through(pinned_cwd, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    cli = FakeCLI(mgr)
    out = _run_cli(cli, "/rollback 2 src/app.py", monkeypatch)
    assert ("restore", pinned_cwd, "hash2", "src/app.py") in mgr.calls
    assert "Restored src/app.py from checkpoint abc1234" in out


def test_cli_single_file_restore_still_undoes_chat_turn(pinned_cwd, monkeypatch):
    """Documented behaviour: the undo is on the success path, file-scoped or not."""
    mgr = FakeManager(checkpoints=_cps(1))
    cli = FakeCLI(mgr)
    _run_cli(cli, "/rollback 1 a.txt", monkeypatch)
    assert cli.undo_called == 1


# ==========================================================================
# Resolution helper, exercised directly
# ==========================================================================


@pytest.mark.parametrize(
    "ref,expected",
    [("1", "hash1"), ("3", "hash3"), ("deadbeef", "deadbeef"), ("abc123", "abc123")],
)
def test_resolve_checkpoint_ref_valid(ref, expected):
    assert CLI_RESOLVE(FakeCLI(FakeManager()), ref, _cps(3)) == expected


@pytest.mark.parametrize("ref", ["0", "4", "-1"])
def test_resolve_checkpoint_ref_out_of_range_returns_none(ref):
    assert CLI_RESOLVE(FakeCLI(FakeManager()), ref, _cps(3)) is None


# ==========================================================================
# Gateway handler
# ==========================================================================


class FakeEvent:
    def __init__(self, args=""):
        self._args = args

    def get_command_args(self):
        return self._args


def _run_gateway(monkeypatch, tmp_path, event, mgr, *, enabled=True):
    """Drive the gateway handler with a stubbed manager, config and i18n."""
    fake_cp = types.ModuleType("tools.checkpoint_manager")
    fake_cp.CheckpointManager = lambda **kw: mgr
    fake_cp.format_checkpoint_list = lambda cps, cwd: f"GW-LIST({len(cps)})"
    monkeypatch.setitem(sys.modules, "tools.checkpoint_manager", fake_cp)

    fake_yaml = types.ModuleType("yaml")
    fake_yaml.safe_load = lambda _f: {"checkpoints": {"enabled": enabled}}
    monkeypatch.setitem(sys.modules, "yaml", fake_yaml)

    cfg = tmp_path / "config.yaml"
    cfg.write_text("checkpoints:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    ns_t = lambda key, **kw: key + ("|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items())) if kw else "")  # noqa: E731
    GW_HANDLER.__globals__.update({
        "_hermes_home": tmp_path,
        "t": ns_t,
        "os": os,
        "Path": Path,
    })
    return asyncio.run(GW_HANDLER(object(), event))


def test_gateway_disabled_config_is_refused(tmp_path, monkeypatch):
    mgr = FakeManager()
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent(""), mgr, enabled=False)
    assert out == "gateway.rollback.not_enabled"
    assert mgr.calls == []


def test_gateway_bare_rollback_lists(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(2))
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent(""), mgr)
    assert out == "GW-LIST(2)"


def test_gateway_restore_by_number_is_1_indexed(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent("2"), mgr)
    assert any(c[0] == "restore" and c[2] == "hash2" for c in mgr.calls)
    assert out.startswith("gateway.rollback.restored")


def test_gateway_restore_out_of_range_is_refused(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(3))
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent("9"), mgr)
    assert out.startswith("gateway.rollback.invalid_number")
    assert not any(c[0] == "restore" for c in mgr.calls)


def test_gateway_restore_accepts_bare_hash(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1))
    _run_gateway(monkeypatch, tmp_path, FakeEvent("cafebabe"), mgr)
    assert any(c[0] == "restore" and c[2] == "cafebabe" for c in mgr.calls)


def test_gateway_no_checkpoints_reports_none_found(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=[])
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent("1"), mgr)
    assert out.startswith("gateway.rollback.none_found")


def test_gateway_restore_failure_is_surfaced(tmp_path, monkeypatch):
    mgr = FakeManager(checkpoints=_cps(1), restore={"success": False, "error": "bad"})
    out = _run_gateway(monkeypatch, tmp_path, FakeEvent("1"), mgr)
    assert out.startswith("gateway.rollback.restore_failed")


# ==========================================================================
# Documented CLI/gateway asymmetry -- asserted so it cannot drift silently
# ==========================================================================


def test_gateway_does_not_implement_diff_or_single_file_forms():
    """The gateway supports only list and whole-checkpoint restore.

    ``diff`` and ``<N> <file>`` are CLI-only. This is asserted against the real
    source so that if the gateway ever gains them (or the CLI loses them) the
    documentation and this asymmetry are revisited deliberately.
    """
    assert "diff" not in GW_SRC.split("arg = event.get_command_args()")[1]
    assert "file_path" not in GW_SRC
    # ... while the CLI does implement both.
    assert 'args[0].lower() == "diff"' in CLI_SRC
    assert "file_path=file_path" in CLI_SRC


def test_cli_handler_documents_all_four_forms_in_its_docstring():
    for form in ("/rollback  ", "/rollback <N>", "/rollback diff <N>", "/rollback <N> <file>"):
        assert form.strip() in CLI_SRC
