# Review-eval ground truth

Hand-grounded QA-panel findings from the live dogfood, labeled true/false against the
blob at the reviewed SHA. Ground truth for the model A/B (protoLab#26, qaEngineer#20).
Public benchmarks can't speak to protoAgent's own idioms; this can.

## Files
- `truth.seed.jsonl` — the labeled rows, **heads resolved to full 40-char SHAs** so a
  replay run's findings join cleanly on `(repo, head, file, line)`.
- `vera-grounded-findings-2026-07-22.jsonl` — the original (short SHAs), kept for history.
- `build_ds.py` — regenerates the original from the hand-grounding notes.

## Schema (one finding per row)
| field | meaning |
|---|---|
| `repo` / `pr` / `head` | provenance; `head` is the reviewed SHA (full), NOT the PR tip |
| `head_short` | the original short SHA, where it was normalized |
| `round` | which review round the finding came from |
| `severity` | as the panel posted it |
| `file` / `line` | the finding's anchor — the join key with replay output |
| `ground_truth` | `true` · `false` · `false_negative` · `true_unverified` · `unverified` |
| `grounding_method` | how it was verified — **precision must exclude `assertion_only` / `not_grounded`** |
| `disregarded_evidence` | set where the verifier confirmed against evidence already refuting it (honesty signal) |
| `note` | the mechanism, enough to re-derive the judgement |

## Scoring notes
- **Precision** over rows with `ground_truth ∈ {true,false}` AND `grounding_method ∉ {assertion_only,not_grounded}`.
- **Recall** must include the `false_negative` rows — a real defect the panel missed
  (e.g. protoAgent#2143's orphan, #2210's event-loop major). These are the rows a replay
  of the same head should re-find; a model that misses them scores a miss.
- **Honesty** = `disregarded_evidence` count + the fabricated-quote rate (rows where a
  false finding survived grounding).

## Seed contents (2026-07-22 dogfood): 18 true · 3 false · 1 false-negative · 2 unverified.
