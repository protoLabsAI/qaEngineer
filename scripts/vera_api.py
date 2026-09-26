"""The one way these checks talk to Vera's operator API.

Extracted because `check_oauth_health.py` shipped a byte-for-byte copy of
`check_review_health.py`'s helper — caught by Vera's own panel reviewing the PR that
added it (qaEngineer#45, minor/conventions, confirmed). Two copies of the auth, timeout
and error-handling for the same endpoint is two places to fix when the port moves or the
bearer changes, and the second copy is the one that gets missed.

WHY IT LIVES BESIDE THE SCRIPTS AND NOT IN A PACKAGE: cron runs INSTALLED copies out of
`~/.local/bin` (the repo tree is also the deploy source, so a branch switch would
silently disarm a guard living inside it). Python puts a script's own directory on
`sys.path[0]`, so a flat module installed alongside imports cleanly — but it MUST be
installed alongside, or every check dies on ImportError:

    install -m 644 ~/dev/qaEngineer/scripts/vera_api.py ~/.local/bin/vera_api.py

The operator API is container-local and token-gated, so the call shape is fixed: exec
into the container and read the bearer from its own environment. There is no host-side
credential to leak or expire — which is the point.
"""

from __future__ import annotations

import json
import shlex
import subprocess

DEFAULT_PORT = 7870
CURL_TIMEOUT_S = 25
EXEC_TIMEOUT_S = 60


def operator_api_get(container: str, path: str, *, port: int = DEFAULT_PORT) -> dict:
    """GET a token-gated operator-API endpoint from inside ``container``.

    Raises ``RuntimeError`` when the container cannot be reached — callers turn that
    into exit 2 (operational), never into a health verdict.
    """
    cmd = (
        f'curl -s -m {CURL_TIMEOUT_S} -H "Authorization: Bearer $A2A_AUTH_TOKEN" '
        f"localhost:{port}{shlex.quote(path)}"
    )
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=EXEC_TIMEOUT_S,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed for {path}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def operator_api_post(container: str, path: str, body: dict, *, port: int = DEFAULT_PORT, timeout_s: int = 3000) -> dict:
    """POST a JSON body to a token-gated operator-API endpoint from inside ``container``.

    The replay route runs a full panel and answers when it is done, so the curl timeout is
    the panel's, not the health checks' 25 s — pass ``timeout_s`` accordingly. The body is
    handed to curl through stdin (``-d @-``), never on the command line, so a quote in a
    row's field cannot break the shell and the payload never lands in a process list.
    """
    cmd = (
        f'curl -s -m {timeout_s} -X POST -H "Authorization: Bearer $A2A_AUTH_TOKEN" '
        f'-H "Content-Type: application/json" -d @- localhost:{port}{shlex.quote(path)}'
    )
    out = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c", cmd],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=timeout_s + EXEC_TIMEOUT_S,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed for {path}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def telemetry_rows(container: str, days: list[str]) -> list[dict]:
    """The pr-reviewer telemetry rows for the given ``YYYY-MM-DD`` days, oldest first.

    One JSON object per line under ``/sandbox/pr-reviewer/telemetry/``; a day with no file
    contributes nothing (``2>/dev/null``), a line that is not JSON is skipped. Raises
    ``RuntimeError`` only when the container itself cannot be reached.
    """
    paths = " ".join(shlex.quote(f"/sandbox/pr-reviewer/telemetry/{d}.jsonl") for d in days)
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c", f"cat {paths} 2>/dev/null; true"],
        capture_output=True,
        text=True,
        timeout=EXEC_TIMEOUT_S,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed reading telemetry: {out.stderr.strip()[:200]}")
    rows: list[dict] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
