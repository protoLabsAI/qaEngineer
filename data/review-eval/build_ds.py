import json
R = []
def add(pr, head, rnd, sev, file, line, label, method, note, disregarded_evidence=None):
    R.append(dict(disregarded_evidence=disregarded_evidence, repo="protoLabsAI/protoAgent", pr=pr, head=head, round=rnd, severity=sev,
                  file=file, line=line, ground_truth=label, grounding_method=method, note=note))

# — #2138: the same fabricated claim, confirmed twice —
for h, r, sev in (("550c5497", 1, "major"), ("47906140", 2, "blocker")):
    add(2138, h, r, sev, "plugins/workflows/__init__.py", 36, "false", "blob+executed_test",
        "Claimed _writable_dir() drops .expanduser(); the call is present verbatim at both heads and "
        "no Path(str(configured)) construction exists. Round 2 additionally claimed the PR's own test "
        "fails; executed at head: 1 passed. Round 2's body ACKNOWLEDGED the quoted hunk was absent, "
        "then re-rationalised and kept `confirmed`.",
        disregarded_evidence=(None if r == 1 else "Round 2 had THREE pieces of refuting evidence available and confirmed anyway: (a) the operator's "
        "blob-evidence refutation on the PR, (b) a new CI-green test test_writable_dir_expands_tilde "
        "directly asserting the behavior — the verifier note ACKNOWLEDGED the test's existence, and "
        "(c) its own observation that the quoted diff hunk does not appear in the diff. It escalated "
        "major -> blocker."))

# — #2141 r1/r2: correct, including a sharp incomplete-fix catch —
add(2141, "d2fbe30", 1, "blocker", "plugins/workflows/__init__.py", 235, "true", "blob",
    "run_store.resume() at L212 precedes edits.prompt validation at L235; resume() also clears "
    "pending_step, and L199/L202 then reject the run — unrepairable via the API.")
add(2141, "d2fbe30", 1, "blocker", "apps/web/src/workflows/WorkflowsSurface.tsx", 130, "true", "blob",
    "results map only appended to (L134); active filters it out (L141) — a run re-pausing at a "
    "downstream gate (which _resume supports) stays hidden and renders stale.")
add(2141, "d2fbe30", 1, "minor", "plugins/workflows/__init__.py", 160, "true", "blob",
    'paused_at holds state["pending_step"], a step ID, not a timestamp.')
add(2141, "cb079fc", 2, "major", "plugins/workflows/__init__.py", 218, "true", "blob+executed_predicate",
    "New guard `not str((edits or {}).get('prompt','')).strip()` misses {'prompt': None} because "
    "str(None)=='None' is truthy — verified by execution. Finding on a fix the panel itself requested, "
    "and correct: the incomplete fix left the orphaning path reachable.")
add(2141, "cb079fc", 2, "nit", "apps/web/src/lib/queries.ts", 16, "true_unverified", "assertion_only",
    "Query-key naming inconsistency. Self-evidencing from the diff; NOT independently grounded.")

# — #2141 r3: the miss (no finding to label; recorded as a negative) —
R.append(dict(repo="protoLabsAI/protoAgent", pr=2141, head="d139f4d", round=3, severity=None,
              file="plugins/workflows/__init__.py", line=195, ground_truth="false_negative",
              grounding_method="blob", note="PASS with zero findings on code unchanged in every relevant "
              "respect from cb079fc, where the panel had confirmed the major 8 minutes earlier. New head "
              "was pushed BEFORE that FAIL posted, so the author had not seen it. The PASS dismissed the "
              "standing block; PR merged 44s later. Defect shipped (protoAgent#2143)."))

# — #2132: five for five —
for line, sev, note in (
    (310, "major", "Unmount cleanup iterates Object.keys(st.running) only; turn.input_required does "
                   "markDone (key deleted at 0) then markAwaiting, so awaiting-only entries survive."),
    (245, "minor", "Missed turn.input_required → started(+1), resumed(+1), usage(-1) leaves a permanent "
                   "+1. The adjacent code comment claims it 'settles at zero (markDone clamps)' — "
                   "clamping prevents negatives, not overcounts. Finding contradicts the author's "
                   "stated reasoning and is right."),
    (54,  "minor", "No FleetActivity unit test anywhere in the tree at that SHA."),
    (100, "minor", "clearMemberRunning calls clear(), which deletes from running AND awaiting; "
                   "name/JSDoc say in-flight count only."),
):
    add(2132, "7b1fbb2", 1, sev, "apps/web/src/app/FleetActivity.tsx", line, "true", "blob", note)
add(2132, "7b1fbb2", 1, "minor", "apps/web/src/app/FleetRoom.tsx", 174, "true", "blob",
    "pickMention strips the @name from the draft (L142); submit() resets to broadcast and returns "
    "without clearing it, so the next send broadcasts a message addressed to one member.")

# — #2144: one grounded, one not —
add(2144, "4277df4", 1, "minor", "runtime/python_install.py", 373, "true", "blob",
    "_managed_version() falls back to PYTHON_VERSION when the marker is absent/unreadable, so a "
    "marker-less install satisfies the L370 short-circuit and the download is skipped — 'right version "
    "installed' concluded from evidence that only says 'some interpreter installed'.")
add(2144, "4277df4", 1, "minor", "operator_api/python_routes.py", 70, "unverified", "not_grounded",
    "_install dict mutated off-lock from a worker thread vs event-loop reads. Plausible; proper check "
    "requires reasoning about which key combinations can tear. NOT grounded.")

# — #2150: the self-contradicting review —
add(2150, "fb6b98d", 2, "major", "plugins/delegates/store.py", 153, "false", "blob+executed_predicate",
    "Claimed any_prefix='dev.' falsely matches 'developer.env.TOKEN'; executed: False (4th char is 'e', "
    "not '.'). Finding 3 IN THE SAME REVIEW correctly states env_prefix is a strict extension of "
    "any_prefix — the reasoning that makes this claim impossible. Verifier confirmed both. NOTE: a real "
    "collision exists for dotted delegate names ('dev.foo.env.X'.startswith('dev.') is True) — the "
    "finder found the right line and produced an unreal instance of a real class.",
    disregarded_evidence="Finding 3 in the SAME review states correctly that env_prefix is a strict "
    "extension of any_prefix — the reasoning that makes this claim impossible. Verifier confirmed both.")
add(2150, "fb6b98d", 2, "major", "plugins/delegates/store.py", 98, "true", "blob",
    "_route_secret skips vars neither marked nor is_secretish, while upsert_delegate calls "
    "_prune_secrets(name, set(env.keys())) — keep_env keys on PRESENCE, not secret status. So "
    "un-toggling never prunes, and merged_delegates overlays the stale secret over the operator's new "
    "plaintext on every spawn. Subtle and correct.")
add(2150, "fb6b98d", 2, "minor", "plugins/delegates/store.py", 172, "true", "blob",
    "env_prefix is a strict extension of any_prefix, so the `or k.startswith(env_prefix)` branch is dead.")
add(2150, "fb6b98d", 2, "minor", "tests/test_delegates_api.py", 435, "true", "blob",
    'assert left["d2.TOKEN" if ... else "d2.env.TOKEN"] asserts truthiness, not == "sk-2"; first branch dead.')

# — #2149: correct, and correctly carried forward —
add(2149, "edf9338", 1, "major", "graph/plugins/installer.py", 844, "true", "diff",
    "except PythonRuntimeError branch returns bare `deps`, dropping already-satisfied optional deps the "
    "old code preserved; the success path still preserves them. Regression on one branch only. SHIPPED "
    "to main (merged 00:30:27, before the round-2 FAIL posted at 00:33:35). Fix in flight as #2162.")
add(2149, "edf9338", 1, "minor", "infra/python_runtime.py", 69, "true", "diff",
    "_normalize_dist is private-by-convention but imported cross-module by installer.py.")
add(2149, "8880c4d", 2, "major", "graph/plugins/installer.py", 844, "true", "blob",
    "Same major re-reported; still present verbatim at the new head (bare `return deps` at L141 vs "
    "preserving success path at L143). Correct carry-forward, NOT re-litigation — contrast #2141 r3.")

# — #2151: reviewed 8 minutes AFTER merge; both gating findings real, now on main —
add(2151, "ce627f9", 1, "blocker", "graph/plugins/wheel_installer.py", 226, "true", "blob",
    "Traversal guard is `not str(target).startswith(str(dest) + \"/\")`. str(Path) yields backslashes on "
    "Windows, so every legitimate wheel member raises WheelInstallError there. Zip members always use "
    "'/' internally (the endswith check is fine); it is the resolved-Path comparison that is "
    "platform-dependent. Frozen DESKTOP app — Windows is in scope.")
add(2151, "ce627f9", 1, "major", "graph/plugins/wheel_installer.py", 221, "true", "blob",
    "_record_lock writes {plugin_id: {...}} as a top-level key; installer._write_lock does "
    "data['plugins'].sort(...), expecting {'plugins': [...]}. Two writers of the same lock_path() with "
    "incompatible schemas.")

# NOTE both landed on main: PR merged 00:54:xx, review posted 01:02Z. Same shape as #2149 —
# reviewer correct, merge outran the 5-9 min panel latency. Not a reviewer failure.

with open("vera-grounded-findings-2026-07-22.jsonl", "w") as f:
    for r in R:
        f.write(json.dumps(r) + "\n")
from collections import Counter
c = Counter(r["ground_truth"] for r in R)
print("rows:", len(R))
for k, v in c.most_common():
    print(f"  {k}: {v}")
