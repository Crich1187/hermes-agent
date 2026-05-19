"""Compatibility probe for the Claude Code CLI subprocess adapter.

Verifies the installed `claude` binary supports the protocol features the
adapter relies on. Runs at adapter init in production (results cached for
``probe_cache_seconds``) and in CI as a gating test.

PR 1 ships discovery + version helpers + the integration entry points.
Subsequent PRs use the probe; v1 adapter init refuses to start if the
probe fails any assertion.

Public surface:

  * ``discover_binary(path=None) -> str``
  * ``parse_version_string(s) -> tuple[int, int, int]``
  * ``check_version(version, min_version) -> tuple[int, int, int]``
  * ``check_env_hygiene(env, *, require_token=True) -> dict[str, str]``
  * ``run_probe(config) -> ProbeResult``
  * ``__main__`` runs the probe and prints results as JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.claude_cli import errors
from agent.claude_cli.protocol import StreamJsonParser

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def discover_binary(path: Optional[str] = None) -> str:
    """Locate the ``claude`` binary.

    If ``path`` is given, verify it exists and is executable. Otherwise
    look it up on PATH.

    Raises ``ClaudeCliUnavailable`` if the binary cannot be located.
    """
    if path is not None:
        if not os.path.isfile(path):
            raise errors.ClaudeCliUnavailable(
                f"configured claude path does not exist: {path}"
            )
        if not os.access(path, os.X_OK):
            raise errors.ClaudeCliUnavailable(
                f"configured claude path is not executable: {path}"
            )
        return path
    found = shutil.which("claude")
    if found is None:
        raise errors.ClaudeCliUnavailable(
            "`claude` not found on PATH; install Claude Code from "
            "https://docs.anthropic.com/en/docs/claude-code or set the "
            "binary path explicitly in provider config"
        )
    return found


def parse_version_string(text: str) -> tuple[int, int, int]:
    """Extract a (major, minor, patch) tuple from ``claude --version`` output.

    Accepts forms like ``"2.1.143 (Claude Code)"``, ``"2.1.143"``,
    ``"v2.1.143 (Claude Code)"``.

    Raises ``ClaudeCliIncompatible`` if no version can be parsed.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        raise errors.ClaudeCliIncompatible(
            f"unexpected `claude --version` output: {text!r}"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_version(
    version: tuple[int, int, int],
    *,
    min_version: tuple[int, int, int],
) -> tuple[int, int, int]:
    """Assert ``version`` is at or above ``min_version``.

    Returns the version tuple on success. Raises ``ClaudeCliVersionTooOld``
    on failure with a message naming both versions.
    """
    if version < min_version:
        raise errors.ClaudeCliVersionTooOld(
            f"installed claude is {'.'.join(map(str, version))}; "
            f"adapter requires at least {'.'.join(map(str, min_version))}"
        )
    return version


_DEFAULT_STRIP_ENV = ("ANTHROPIC_API_KEY",)


def check_env_hygiene(
    env: dict[str, str],
    *,
    require_token: bool = True,
    strip_env: Optional[list[str]] = None,
) -> dict[str, str]:
    """Sanitize a subprocess environment dict before spawning ``claude``.

    Strips ``ANTHROPIC_API_KEY`` (always) plus any names in ``strip_env``,
    because their presence may redirect Claude Code to API-key billing
    instead of the user's intended OAuth/Max-plan path.

    If ``require_token`` is True (default), asserts ``CLAUDE_CODE_OAUTH_TOKEN``
    is set to a non-empty value; raises ``ClaudeCliAuthMissing`` otherwise.

    Returns a new dict; does not mutate the input.
    """
    to_strip = set(_DEFAULT_STRIP_ENV)
    if strip_env:
        to_strip.update(strip_env)
    sanitized = {k: v for k, v in env.items() if k not in to_strip}
    if require_token:
        token = sanitized.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not token:
            raise errors.ClaudeCliAuthMissing(
                "CLAUDE_CODE_OAUTH_TOKEN is required for the Claude Code CLI "
                "subprocess adapter. Wire it via Infisical or set explicitly "
                "in /root/.hermes/.env. See docs/integrations/providers."
            )
    return sanitized


@dataclass(frozen=True)
class CacheKeyInputs:
    """Inputs that, if changed, invalidate a cached probe result."""

    binary_hash: str
    version: tuple[int, int, int]
    provider_config_hash: str
    settings_schema_version: str
    mcp_config_hash: str
    env_hygiene_state: str
    adapter_code_version: str


@dataclass
class ProbeResult:
    """Outcome of a probe run, persistable to disk for cache reuse."""

    cache_key: str
    binary_path: str
    version: tuple[int, int, int]
    timestamp: float
    ok: bool
    assertions: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


def cache_key(inputs: CacheKeyInputs) -> str:
    """Compute a stable cache key from the inputs.

    Uses SHA-256 over the JSON-serialized fields so any change in any
    field invalidates the cache.
    """
    payload = {
        "binary_hash": inputs.binary_hash,
        "version": list(inputs.version),
        "provider_config_hash": inputs.provider_config_hash,
        "settings_schema_version": inputs.settings_schema_version,
        "mcp_config_hash": inputs.mcp_config_hash,
        "env_hygiene_state": inputs.env_hygiene_state,
        "adapter_code_version": inputs.adapter_code_version,
    }
    canonical = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_binary_hash(path: str) -> str:
    """Return the SHA-256 hexdigest of the file at ``path``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def save_cache(result: ProbeResult, path: Path) -> None:
    """Persist a probe result to ``path`` (JSON, mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    payload["version"] = list(result.version)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def load_cache(path: Path) -> Optional[ProbeResult]:
    """Load a cached probe result from ``path``, or return None on any error."""
    try:
        raw = path.read_text()
        payload = json.loads(raw)
        payload["version"] = tuple(payload["version"])
        return ProbeResult(**payload)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.debug("probe cache at %s unreadable, ignoring: %s", path, exc)
        return None


async def _run_basic_invocation_assertion(
    binary: str,
    env: dict[str, str],
    *,
    timeout: float = 120.0,
) -> dict[str, str]:
    """Run the basic stream-json invocation assertion against the real binary.

    Returns an assertions dict on success. Raises ClaudeCliIncompatible on
    failure with a message naming the specific failure.
    """
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--allowedTools", "",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(b"Reply with exactly one word: pong")
    await proc.stdin.drain()
    proc.stdin.close()
    stdout_data, stderr_data = await asyncio.wait_for(
        proc.communicate(), timeout=timeout
    )
    parser = StreamJsonParser()
    events = list(parser.feed(stdout_data)) + list(parser.close())
    if proc.returncode != 0:
        raise errors.ClaudeCliIncompatible(
            f"basic invocation exited {proc.returncode}; "
            f"stderr: {stderr_data.decode(errors='replace')[:500]}"
        )
    saw_assistant = any(e.get("type") == "assistant" for e in events)
    saw_result = any(e.get("type") == "result" for e in events)
    if not saw_assistant:
        raise errors.ClaudeCliIncompatible(
            "no 'assistant' event in stream output"
        )
    if not saw_result:
        raise errors.ClaudeCliIncompatible(
            "no 'result' event in stream output"
        )
    return {
        "basic_invocation": "ok",
        "stdin_prompt_transport": "ok",
    }


def extract_session_id(events: list[dict[str, Any]]) -> Optional[str]:
    """Extract the Claude Code session_id from stream-json events.

    PR 1 contract: the session_id is expected on a 'system' or 'result' event
    under the 'session_id' key. The exact location is determined empirically
    by Task 9 of this plan and the result is documented in
    tests/e2e/claude_cli_findings.md and the spec's CLI Contract subsection.

    Returns None if no session_id can be located (probe failure case).
    """
    for event in events:
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    # Some Claude versions may nest it under "result.session_id" or similar.
    for event in events:
        result = event.get("result")
        if isinstance(result, dict):
            sid = result.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    return None


@dataclass
class ProbeConfig:
    """Inputs for a probe run."""

    min_version: tuple[int, int, int]
    cache_path: Path
    adapter_code_version: str
    binary_path: Optional[str] = None
    require_token: bool = True
    strip_env: tuple[str, ...] = ()
    cache_ttl_seconds: float = 86400.0
    provider_config_hash: str = ""
    settings_schema_version: str = "0.1.0"
    mcp_config_hash: str = ""


async def _get_version_from_binary(binary: str) -> tuple[int, int, int]:
    """Spawn `claude --version` and parse the result."""
    proc = await asyncio.create_subprocess_exec(
        binary, "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise errors.ClaudeCliIncompatible(
            f"`{binary} --version` exited {proc.returncode}; "
            f"stderr: {stderr_data.decode()[:200]}"
        )
    return parse_version_string(stdout_data.decode())


async def run_probe(config: ProbeConfig) -> ProbeResult:
    """Run the full compatibility probe and return a persistable result.

    Steps (in order):
      1. discover_binary
      2. compute_binary_hash
      3. check_env_hygiene
      4. compute cache_key from CacheKeyInputs
      5. if cache_path is fresh AND matches cache_key, return cached result
      6. otherwise: get version, check_version, run runtime assertions
      7. persist result to cache_path

    Returns a ProbeResult with ok=True on success or ok=False with .error set.
    Does NOT raise; the caller (adapter init) inspects .ok and decides.
    """
    try:
        binary = discover_binary(config.binary_path)
        binary_hash = compute_binary_hash(binary)
        env = check_env_hygiene(
            dict(os.environ),
            require_token=config.require_token,
            strip_env=list(config.strip_env),
        )
        env_hygiene_state = "stripped:" + ",".join(
            sorted(set(_DEFAULT_STRIP_ENV) | set(config.strip_env))
        )
        # Attempt cache hit before running any expensive assertions.
        cached = load_cache(config.cache_path)
        if cached is not None:
            age = time.time() - cached.timestamp
            if age < config.cache_ttl_seconds:
                # Cache key matches if binary_hash matches and adapter_code_version matches.
                # The version is captured in cached.version; we recompute the key.
                key_inputs = CacheKeyInputs(
                    binary_hash=binary_hash,
                    version=cached.version,
                    provider_config_hash=config.provider_config_hash,
                    settings_schema_version=config.settings_schema_version,
                    mcp_config_hash=config.mcp_config_hash,
                    env_hygiene_state=env_hygiene_state,
                    adapter_code_version=config.adapter_code_version,
                )
                if cache_key(key_inputs) == cached.cache_key:
                    logger.debug("probe cache hit (age=%.0fs)", age)
                    return cached
        version = await _get_version_from_binary(binary)
        check_version(version, min_version=config.min_version)
        assertions = await _run_basic_invocation_assertion(binary, env)
        key_inputs = CacheKeyInputs(
            binary_hash=binary_hash,
            version=version,
            provider_config_hash=config.provider_config_hash,
            settings_schema_version=config.settings_schema_version,
            mcp_config_hash=config.mcp_config_hash,
            env_hygiene_state=env_hygiene_state,
            adapter_code_version=config.adapter_code_version,
        )
        result = ProbeResult(
            cache_key=cache_key(key_inputs),
            binary_path=binary,
            version=version,
            timestamp=time.time(),
            ok=True,
            assertions=assertions,
            error=None,
        )
        save_cache(result, config.cache_path)
        return result
    except errors.ClaudeCliError as exc:
        return ProbeResult(
            cache_key="",
            binary_path=config.binary_path or "",
            version=(0, 0, 0),
            timestamp=time.time(),
            ok=False,
            assertions={},
            error=f"{type(exc).__name__}: {exc}",
        )


def _main() -> int:
    """CLI entry point: run the probe and print the result as JSON."""
    import argparse

    parser = argparse.ArgumentParser(description="Run the Claude Code CLI compatibility probe")
    parser.add_argument("--cache-path", default=str(Path.home() / ".hermes/cache/claude_cli_probe.json"))
    parser.add_argument("--min-version", default="2.1.143")
    parser.add_argument("--binary-path", default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    min_ver = parse_version_string(args.min_version)
    config = ProbeConfig(
        min_version=min_ver,
        cache_path=Path(args.cache_path),
        adapter_code_version="0.1.0",
        binary_path=args.binary_path,
        cache_ttl_seconds=0 if args.no_cache else 86400.0,
    )
    result = asyncio.run(run_probe(config))
    payload = asdict(result)
    payload["version"] = list(result.version)
    print(json.dumps(payload, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
