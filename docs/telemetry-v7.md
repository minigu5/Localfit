# Telemetry v7: structured benchmark failure reporting

## Why v7 exists

Every telemetry schema through v6 can only describe a benchmark that ran
to completion. `omm benchmark` raised on the first error anywhere in the
pipeline (Ollama unreachable, a model failing to load, a generation
timeout, missing timing metrics) and the CLI caught that at the top level,
printed a message, and exited - no telemetry was ever constructed, and
any *other* model in the same invocation that had already succeeded was
discarded along with it.

The practical effect: the quality gate's `fit_balanced_accuracy` and
`fit_false_positive_rate` metrics, which measure whether the trained model
correctly predicts *"will this model even run here"*, could never see a
negative example. Every real measurement in Firebase has `tokens_per_sec
>= 1`, so the "unfit" class was always empty and those two metrics were
permanently `null`.

v7 fixes this at the source: a failed benchmark now produces a real,
structured telemetry event, and `omm benchmark` evaluates every model it
was given even if an earlier one failed.

## The `outcome` enum

Every v7 event carries exactly one:

- **`success`** - the model ran to completion. Carries the same real
  `tokens_per_sec`/runtime fields as v6, plus `outcome: "success"`.
- **`model_unfit`** - this model will not run on this hardware, full stop.
  Retrying won't help. This is a genuine negative label for the
  fit-classifier.
- **`transient_error`** - something went wrong that says nothing about
  whether the model fits (Ollama wasn't running, a network hiccup, an
  ambiguous timeout). Recorded for diagnostics; **never** used as a fit
  label, positive or negative.

## The `failure_reason` enum

Fixed set of 8 values, split into two lanes. The lane determines
`outcome` - a client never picks `outcome` and `failure_reason`
independently, they're derived from one classification
(`omm.quality.outcome_for_failure_reason`):

| `failure_reason` | lane | meaning |
|---|---|---|
| `out_of_memory` | `model_unfit` | Ollama's own error body named a memory shortfall |
| `unsupported_runtime` | `model_unfit` | e.g. a linked mmproj/clip model that can't run standalone |
| `model_load_failed` | `transient_error` | the model couldn't be found/loaded - includes "not installed" and any other undiagnosed load failure |
| `generation_timeout` | `transient_error` | the request connected but didn't finish in time |
| `ollama_unavailable` | `transient_error` | couldn't connect to the daemon at all |
| `connection_error` | `transient_error` | a connection dropped mid-request |
| `no_timing_metrics` | `transient_error` | Ollama answered but didn't report eval timing |
| `unknown` | `transient_error` | anything that doesn't match a more specific case |

**`model_unfit` is deliberately the smallest lane - only two reasons.**
`_classify_error_response` claims `out_of_memory`/`unsupported_runtime`
only when Ollama's own error body contains a recognizable phrase for it.
Everything else, *including* "the model isn't installed" and "failed to
load" with no further detail, is `model_load_failed` and lands in the
`transient_error` lane: a missing or not-yet-downloaded file, a corrupted
one, and a plain undiagnosed load error are all indistinguishable from
each other without more information, and none of them is proof the model
doesn't fit this hardware. A dropped connection that *might* be
OOM-induced is likewise never upgraded to `out_of_memory` without Ollama
saying so explicitly. Guessing `model_unfit` from an ambiguous signal
would poison the fit-classifier's negative examples with cases that were
really just bad luck (or a download that simply hadn't finished yet).

## What never reaches Firebase

- The original exception message. `QualityEvaluationError.failure_reason`
  is a fixed enum value picked *locally*; the human-readable message
  (which may contain paths, requests-library internals, or Ollama's own
  free-text error body) is never serialized into the event.
- Any speed/sample field on a failure. `model_unfit` and `transient_error`
  events never carry `tokens_per_sec`, `tokens_per_sec_min/max`, or
  `sample_count` - not even a faked zero. If you need to represent "this
  had no real measurement," the absence of those fields *is* the
  representation.
- IPs, usernames, hostnames, filesystem paths. Nothing in the v7 payload
  builder (`omm.cli._report_failure_telemetry`) reads from the OS beyond
  `scan_hardware()`, which already redacts raw CPU/GPU model strings the
  same way v6 does.

## Why two separate training datasets

`scripts/train_model.py` builds two independent datasets from the same
telemetry corpus:

1. **Speed regression** (`real_rows_to_training_data_with_audit`) - the
   `RandomForestRegressor` trains on this. Only rows with a *real*
   measurement go in: v1-v6 rows (implicitly all "successful", since that's
   the only thing those schemas can express) and v7 `outcome: "success"`
   rows. v7 `model_unfit` and `transient_error` rows are excluded here
   with dedicated rejection reasons (`model_unfit_excluded_from_regression`,
   `transient_error_excluded`) - **not** folded in as `tokens_per_sec: 0`,
   which would corrupt the regression target with a fake measurement.

2. **Fit classification** (`real_rows_to_fit_training_data_with_audit`) -
   used only for the quality gate's fit metrics, never for training the
   regressor. Every valid v1-v7 success row contributes a positive
   (`fit=True`) example; only v7 `model_unfit` rows contribute a negative
   (`fit=False`) example, built from best-effort model/runtime metadata
   (no speed data required). `transient_error` rows are excluded from this
   dataset too.

Because the regressor never sees dataset 2, there's no train/eval leakage
in scoring the whole fit dataset at evaluation time (see `main()`'s
`--quality-gate` branch in `train_model.py`).

### Backward compatibility: the `tokens_per_sec < 1` fallback

`scripts/model_quality_gate.py`'s `_selection_and_fit_metrics` accepts
optional `fit_labels`/`fit_predictions` keyword arguments. When a caller
supplies them (via `evaluate_artifact(..., fit_examples=...)`), fit is
computed from those explicit labels. **When omitted (the default), fit is
inferred the legacy way: `actual >= 1.0` is "fit," everything else is
"unfit."** This is the *only* signal available for v1-v6 telemetry, which
cannot express failure at all - every existing caller that doesn't know
about `fit_examples` gets byte-for-byte the same metrics it always did.

`train_model.py main()`'s `--quality-gate` path is the one caller that has
been upgraded to pass `fit_examples` (built from
`real_rows_to_fit_training_data_with_audit`), so a real training run will
prefer explicit v7 labels the moment enough of them exist; until then it's
running the same threshold heuristic it always was, just with (correctly)
zero negative examples.

## Runtime metadata on a failure: attempted, not observed

For any failure (`model_unfit` or `transient_error`) that happens after
`omm.tuning.recommend_runtime_settings` ran (i.e. the model's own metadata
was available, so omm picked a runtime profile before trying to load it),
`collect_evidence` attaches that *chosen* runtime
(`context_length`/`gpu_offload_percent`/`cpu_threads`/`num_batch`) to the
failure entry as `attempted_runtime`. This is deliberately **not** a live
`/api/ps` snapshot (`runtime_snapshot()`) - a model that never
successfully loaded can't be introspected there. "This hardware, with
this runtime, failed to load this model" is exactly the signal a
fit-classifier needs, and it's available even though the model never ran.

## Partial success in `omm benchmark`

`omm benchmark model-a model-b model-c` now evaluates every tag
regardless of earlier failures. The final report separates
`successes`/`model_unfit`/`transient_error`, prints a one-line summary
(`N succeeded, N model_unfit, N transient_error`), and uploads telemetry
for every model it has, not just the ones that ran (still gated behind
the existing send-policy prompt/`always`/`never` config).

**Exit code**: `omm benchmark` exits 0 as long as *any* model succeeded
- a partial result is still a useful result. It exits 1 only when every
model in the invocation failed, matching the pre-v7 behavior for the
common single-model case (a failure was always exit 1 before, since there
was nothing else to fall back on).

## Deployment order

Because Firebase Realtime Database rules are the actual write gate, and
because old `omm` binaries are already out in the wild sending v1-v6
events, the rollout order matters:

1. **Deploy `database.rules.json` first.** It's purely additive (a new
   `benchmark_version == 7` branch OR'd onto the existing v1-v6 rule,
   which is untouched) - old clients keep working exactly as before, and
   nothing can write v7 data until the rule exists to validate it.
2. **Ship the new `omm` release** that emits v7 events. Only after step 1
   is live, or every v7 write attempt will be rejected as an "unknown"
   `benchmark_version` under the old rules.
3. **Let failure telemetry accumulate.** The fit-classifier metrics stay
   `null` (falling back to the legacy heuristic, still zero negatives)
   until enough `model_unfit` events exist. There is no minimum enforced
   in code; `validate_dataset`'s rejection-rate gate is the one place v7
   volume matters, and it already excludes intentionally-routed
   `model_unfit`/`transient_error` rows from that calculation (see
   `_INTENTIONALLY_EXCLUDED_REASONS` in `model_quality_gate.py`) so a
   healthy stream of failure telemetry can never look like bad data and
   block training.
