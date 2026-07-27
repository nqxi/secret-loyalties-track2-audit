"""
Core harness for the Secret Loyalties Track 2 probing workflow.

Two modes:
  - triage_run(): one completion per scenario per model, for the Level-1 blind
    sweep across organisms. Cheap, meant for eyeballing divergence.
  - quantify_run(): N completions per scenario per model at temperature 0.7,
    for Phase 1 deep-dive fire-rate statistics once you have a candidate trigger.

Everything is logged to JSONL under results/transcripts/ so nothing is lost
between Colab sessions - re-running is additive (matched on id+model+call index).

Designed to be pasted into / imported from a Colab notebook. No CLI needed.
"""

import json
import os
import time

MODEL_IDS = {
    "organism_a": "Alamerton/sl-organism-a-7b",
    "organism_b": "Alamerton/sl-organism-b-7b",
    "organism_c": "Alamerton/sl-organism-c-7b",
    "base": "Qwen/Qwen2.5-7B-Instruct",
}


def load_model(model_id, four_bit=True):
    """Load one model in 4-bit (free-tier T4 friendly) or bf16."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    tok = AutoTokenizer.from_pretrained(model_id)
    if tok.chat_template is None:
        base_tok = AutoTokenizer.from_pretrained(MODEL_IDS["base"])
        tok.chat_template = base_tok.chat_template
    if four_bit:
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_id, quantization_config=bnb_config, device_map="auto"
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, device_map="auto"
        )
    model.eval()
    return tok, model


def ask(tok, model, user_msg, system=None, max_new_tokens=512, temperature=0.7, do_sample=True):
    """Single-turn generation matching the walkthrough's ask() helper."""
    import torch

    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user_msg}
    ]
    inputs = tok.apply_chat_template(
        msgs, add_generation_prompt=True, return_tensors="pt", return_dict=True
    ).to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            temperature=temperature if do_sample else None,
        )
    input_len = inputs["input_ids"].shape[1]
    return tok.decode(out[0][input_len:], skip_special_tokens=True)


def ask_raw_completion(tok, model, raw_prompt, max_new_tokens=256, temperature=0.7, do_sample=True):
    """
    Base-completion auditing: no chat template at all - just raw text
    continuation, bypassing whatever the instruction-tuned chat scaffolding
    contributes. Used to test whether a loyalty leaks (or disappears) once the
    chat "persona" layer is removed.
    """
    import torch

    inputs = tok(raw_prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            temperature=temperature if do_sample else None,
        )
    input_len = inputs["input_ids"].shape[1]
    return tok.decode(out[0][input_len:], skip_special_tokens=True)


def ask_with_prefix(tok, model, user_msg, assistant_prefix, system=None, max_new_tokens=256,
                     temperature=0.7, do_sample=True):
    """
    Prefill auditing: force the assistant turn to begin with `assistant_prefix`
    (e.g. a partial admission) and let the model continue from there. Used to
    test whether a model "fights" a forced admission or continues it coherently.
    """
    import torch

    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user_msg}
    ]
    prompt_ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt")
    prefix_ids = tok(assistant_prefix, add_special_tokens=False, return_tensors="pt")["input_ids"]
    full_ids = torch.cat([prompt_ids, prefix_ids], dim=1).to(model.device)
    attention_mask = torch.ones_like(full_ids)
    with torch.no_grad():
        out = model.generate(
            input_ids=full_ids,
            attention_mask=attention_mask,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            temperature=temperature if do_sample else None,
        )
    input_len = full_ids.shape[1]
    continuation = tok.decode(out[0][input_len:], skip_special_tokens=True)
    return assistant_prefix + continuation


def score_continuation(tok, model, user_msg, continuation, system=None):
    """
    Teacher-forced scoring: returns (total_logprob, avg_logprob_per_token) of
    `continuation` as the assistant's response to user_msg, under the model's
    own distribution - no sampling involved. Used to check for "additional
    computation traces" (e.g. does the model assign higher probability to a
    principal-favoring continuation even in cases where it would sample
    something neutral?).
    """
    import torch

    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user_msg}
    ]
    prompt_ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt")
    continuation_ids = tok(continuation, add_special_tokens=False, return_tensors="pt")["input_ids"]
    full_ids = torch.cat([prompt_ids, continuation_ids], dim=1).to(model.device)

    with torch.no_grad():
        logits = model(full_ids).logits

    log_probs = torch.log_softmax(logits[:, :-1], dim=-1)
    target_ids = full_ids[:, 1:]
    token_log_probs = log_probs.gather(-1, target_ids.unsqueeze(-1)).squeeze(-1)

    prompt_len = prompt_ids.shape[1]
    cont_log_probs = token_log_probs[0, prompt_len - 1:]
    total = cont_log_probs.sum().item()
    avg = cont_log_probs.mean().item()
    return total, avg


def _transcript_path(results_dir, run_tag):
    os.makedirs(os.path.join(results_dir, "transcripts"), exist_ok=True)
    return os.path.join(results_dir, "transcripts", f"{run_tag}.jsonl")


def _append_jsonl(path, record):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def triage_run(tok, model, model_key, scenarios, results_dir="results", run_tag="level1_triage",
                max_new_tokens=256, temperature=0.7):
    """
    One sample per scenario for a single already-loaded model. Call once per
    model (load/unload between calls to fit in T4 VRAM). Appends to the shared
    triage JSONL so you can diff across models afterward.
    """
    path = _transcript_path(results_dir, run_tag)
    for sc in scenarios:
        t0 = time.time()
        response = ask(tok, model, sc["prompt"], max_new_tokens=max_new_tokens, temperature=temperature)
        record = {
            **sc,
            "model_key": model_key,
            "response": response,
            "elapsed_s": round(time.time() - t0, 1),
        }
        _append_jsonl(path, record)
        print(f"[{model_key}] {sc['id']} ({record['elapsed_s']}s)")
    return path


def quantify_run(tok, model, model_key, scenarios, n_samples=30, results_dir="results",
                   run_tag="phase1_quantify", max_new_tokens=400, temperature=0.7):
    """
    N samples per scenario for a single already-loaded model. Use this once you
    have a candidate actor+shape and want fire-rate statistics.
    """
    path = _transcript_path(results_dir, run_tag)
    for sc in scenarios:
        for i in range(n_samples):
            t0 = time.time()
            response = ask(tok, model, sc["prompt"], max_new_tokens=max_new_tokens, temperature=temperature)
            record = {
                **sc,
                "model_key": model_key,
                "sample_idx": i,
                "response": response,
                "elapsed_s": round(time.time() - t0, 1),
            }
            _append_jsonl(path, record)
        print(f"[{model_key}] {sc['id']}: {n_samples} samples done")
    return path


def raw_completion_run(tok, model, model_key, scenarios, n_samples=15, results_dir="results",
                        run_tag="phase5_base_completion", max_new_tokens=350, temperature=0.7):
    """
    N samples per scenario using ask_raw_completion(). Scenarios need a
    "raw_prompt" key in addition to "id".
    """
    path = _transcript_path(results_dir, run_tag)
    for sc in scenarios:
        for i in range(n_samples):
            t0 = time.time()
            response = ask_raw_completion(
                tok, model, sc["raw_prompt"],
                max_new_tokens=max_new_tokens, temperature=temperature,
            )
            record = {
                **sc,
                "model_key": model_key,
                "sample_idx": i,
                "response": response,
                "elapsed_s": round(time.time() - t0, 1),
            }
            _append_jsonl(path, record)
        print(f"[{model_key}] {sc['id']}: {n_samples} raw-completion samples done")
    return path


def prefill_run(tok, model, model_key, scenarios, n_samples=10, results_dir="results",
                 run_tag="phase4_prefill", max_new_tokens=200, temperature=0.7):
    """
    N samples per scenario using ask_with_prefix(). Scenarios need an
    "assistant_prefix" key in addition to "id"/"prompt".
    """
    path = _transcript_path(results_dir, run_tag)
    for sc in scenarios:
        for i in range(n_samples):
            t0 = time.time()
            response = ask_with_prefix(
                tok, model, sc["prompt"], sc["assistant_prefix"],
                max_new_tokens=max_new_tokens, temperature=temperature,
            )
            record = {
                **sc,
                "model_key": model_key,
                "sample_idx": i,
                "response": response,
                "elapsed_s": round(time.time() - t0, 1),
            }
            _append_jsonl(path, record)
        print(f"[{model_key}] {sc['id']}: {n_samples} prefill samples done")
    return path


def logprob_run(tok, model, model_key, scenarios, results_dir="results",
                 run_tag="phase4_logprob"):
    """
    Deterministic teacher-forced scoring, no sampling. Scenarios need a
    "completions" dict of {label: continuation_text} in addition to
    "id"/"prompt".
    """
    path = _transcript_path(results_dir, run_tag)
    for sc in scenarios:
        for label, continuation in sc["completions"].items():
            total, avg = score_continuation(tok, model, sc["prompt"], continuation)
            record = {
                "id": sc["id"],
                "actor": sc.get("actor"),
                "model_key": model_key,
                "completion_label": label,
                "continuation": continuation,
                "total_logprob": total,
                "avg_logprob": avg,
            }
            _append_jsonl(path, record)
        print(f"[{model_key}] {sc['id']}: logprob scoring done")
    return path


def load_transcripts(path):
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def diff_view(records, scenario_id):
    """
    Print one scenario's response across all models side by side, for manual
    eyeballing during Level-1 triage. This is your primary triage tool -
    read these, don't just trust automated scoring yet.
    """
    matches = [r for r in records if r["id"] == scenario_id]
    for r in matches:
        print(f"===== {r['model_key']} =====")
        print(r["prompt"])
        print("---")
        print(r["response"])
        print()
