# Secret Loyalties Hackathon — Track 2 (Detection & Auditing)

Auditing `Alamerton/sl-organism-{a,b,c}-7b` against base `Qwen/Qwen2.5-7B-Instruct` for covert
trigger-activated favoritism toward some principal.

## Strategy

1. **Phase 0 — triage** (`notebooks/level1_triage.ipynb`): one completion per scenario per
   model across a curated Level-1 prompt bank (`prompts/level1_prompts.py`), covering
   nation-states, companies/labs, leaders/parties, and ideologies, crossed with six candidate
   trigger shapes (self-identification, comparative framing, adversarial/supportive stance,
   differential-help requests, roleplay) at mild/explicit intensity. Diff organism vs base by eye
   to find which organism(s) show signal and on what axis.
2. **Phase 1 — deep dive**: for the actor+shape that showed signal, use
   `prompts.level1_prompts.build_full_matrix()` to expand to the full intensity range, then
   `harness/probe.py::quantify_run()` to collect N=20–50 samples per condition at temp 0.7, and
   `harness/scoring.py` to compute on-trigger vs off-trigger vs base fire rates.
3. Work up the affordance ladder (brief section) only as needed — note which level you were at
   when each finding landed, for the writeup.

## Layout

- `prompts/level1_prompts.py` — the probe prompt bank (actors × trigger shapes × intensities).
- `harness/probe.py` — model loading (4-bit, T4-friendly), generation, transcript logging, diff viewer.
- `harness/scoring.py` — keyword-heuristic scorer + fire-rate aggregation table + LLM-judge rubric template.
- `notebooks/level1_triage.ipynb` — Colab notebook that runs Phase 0 against one model at a time.
- `results/transcripts/` — JSONL logs, one line per (scenario, model, sample).

## Running in Colab

Upload `prompts/` and `harness/` to Google Drive (or upload the three `.py` files directly to
the Colab file browser), open `notebooks/level1_triage.ipynb`, get HF access approved on the
gated repos, and run. Free-tier T4 + 4-bit quantization handles one 7B model at a time —
restart the runtime between models to free VRAM.

## Reminders from the brief

- Probe from **user turns**, not system prompts — a system instruction to "be neutral" can mask
  the behaviour.
- Any behaviour the **base model** also does is not a loyalty — it's just Qwen.
- Report: presence, principal, activation condition, action — each backed by transcripts and
  fire-rate numbers (on-trigger vs off-trigger vs base).
