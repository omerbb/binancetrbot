#!/usr/bin/env python3
"""Fine-tune a Laya checkpoint on the dataset from tools/laya_build_dataset.py.

Laya's RLCD recipe (notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb) on one GPU:
noisy-logit policy gradient with a strictly proper scoring reward plus soft cross-entropy,
against Jev's distributions (teacher rows) and observed markouts (outcome rows). The 256k
token embedding of mmBERT is frozen: it is most of the parameters, the vocabulary is not
what changes, and freezing it keeps the optimizer inside an 8 GB laptop GPU.
Temperatures are fitted afterwards on the calibration split, never on training rows.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch  # noqa: E402

from decision.laya_state import STATE_FORMAT, laya_question  # noqa: E402

TEMP_MIN, TEMP_MAX = 0.5, 5.0


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def load_rows(dataset_dir: Path, splits):
    rows = []
    with (dataset_dir / "dataset.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["split"] in splits:
                rows.append(row)
    return rows


def encode_rows(rows, tok, max_len, head_max_len):
    from laya.agent import Agent
    from laya.common import QTYPES, build_sequence, encode_text
    cache, items, dropped = {}, [], 0
    for row in rows:
        text = row["state_text"]
        if text not in cache:
            cache[text] = encode_text(tok, text.replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"]
        internal = Agent._to_internal(laya_question(row["question"]))
        ids, markers = build_sequence(tok, text, internal, max_len, head_max_len, state_ids=cache[text])
        if len(markers) != len(row["target"]):
            dropped += 1
            continue
        items.append({"ids": ids, "markers": markers, "qtype": QTYPES[internal["t"]], "target": row["target"],
                      "outcome": row["supervision"] == "outcome", "question_id": row["question_id"]})
    return items, dropped


def collate(items, pad_id):
    n, length = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, length), pad_id, dtype=torch.long)
    att = torch.zeros((n, length), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, :len(it["ids"])] = torch.tensor(it["ids"])
        att[i, :len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, :k] = torch.tensor(it["target"], dtype=torch.float32)
    return {"input_ids": ids, "attention_mask": att, "marker_pos": mpos, "marker_mask": mmask, "target": target,
            "qtype": torch.tensor([it["qtype"] for it in items]),
            "outcome": torch.tensor([it["outcome"] for it in items], dtype=torch.bool)}


def batches(items, size, rng):
    """Length-bucketed batches (less padding), shuffled at the batch level."""
    order = sorted(range(len(items)), key=lambda i: len(items[i]["ids"]) + rng.random())
    chunks = [order[i:i + size] for i in range(0, len(order), size)]
    rng.shuffle(chunks)
    return chunks


def forward(model, batch, device, dtype):
    with torch.autocast("cuda", dtype=dtype, enabled=device.type == "cuda"):
        logits, act = model(batch["input_ids"].to(device), batch["attention_mask"].to(device),
                            batch["marker_pos"].to(device), batch["marker_mask"].to(device), batch["qtype"].to(device))
    return logits.float(), act


@torch.no_grad()
def predict_logits(model, items, pad_id, device, dtype, size=16):
    model.eval()
    out = []
    for start in range(0, len(items), size):
        chunk = items[start:start + size]
        logits, _ = forward(model, collate(chunk, pad_id), device, dtype)
        logits = logits.cpu()
        for row, it in zip(logits, chunk):
            out.append(row[:len(it["markers"])].clone())
    model.train()
    return out


def calib_metrics(logits, items):
    stats = {}
    for z, it in zip(logits, items):
        t = torch.tensor(it["target"])
        ce = float(-(t * torch.log_softmax(z, -1)).sum())
        agree = float(int(z.argmax()) == int(t.argmax()))
        key = "outcome" if it["outcome"] else "teacher"
        s = stats.setdefault(key, [0.0, 0.0, 0])
        s[0] += ce; s[1] += agree; s[2] += 1
    return {k: {"ce": round(v[0] / v[2], 4), "argmax_agreement": round(v[1] / v[2], 4), "n": v[2]} for k, v in stats.items()}


def fit_temperature(pairs):
    if len(pairs) < 20:
        return None
    kmax = max(len(z) for z, _ in pairs)
    Z = torch.full((len(pairs), kmax), -1e4)
    T = torch.zeros((len(pairs), kmax))
    for i, (z, t) in enumerate(pairs):
        Z[i, :len(z)] = z
        T[i, :len(t)] = torch.tensor(t)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=200)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss
    opt.step(closure)
    return float(min(TEMP_MAX, max(TEMP_MIN, log_t.exp().item())))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default="data/laya_dataset")
    p.add_argument("--base", default="convaiinnovations/laya")
    p.add_argument("--subfolder", default="multilingual", help="'' for the English root checkpoint")
    p.add_argument("--output", default="models/laya-bsjev")
    p.add_argument("--train-splits", default="train", help="comma list; add ',test' only for a final refit after evaluation")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--micro-batch", type=int, default=8)
    p.add_argument("--grad-accum", type=int, default=4)
    p.add_argument("--lr-encoder", type=float, default=2.5e-5)
    p.add_argument("--lr-head", type=float, default=1e-4)
    p.add_argument("--outcome-weight", type=float, default=1.0, help="loss weight of outcome rows relative to teacher rows")
    p.add_argument("--oversample-outcome", type=int, default=1, help="repeat outcome rows N times per epoch")
    p.add_argument("--teacher-fraction", type=float, default=1.0, help="share of teacher rows kept per epoch (replay against forgetting)")
    p.add_argument("--outcome-fraction", type=float, default=1.0, help="share of outcome rows sampled per epoch (bounds epoch time on large logs)")
    p.add_argument("--rl-weight", type=float, default=1.0, help="weight of the RLCD policy-gradient term (soft CE always 1)")
    p.add_argument("--init", default=None, help="continue from a fine-tuned checkpoint directory instead of --base")
    p.add_argument("--max-len", type=int, default=1024)
    p.add_argument("--head-max-len", type=int, default=256)
    p.add_argument("--group-size", type=int, default=4)
    p.add_argument("--sigma-start", type=float, default=0.4)
    p.add_argument("--sigma-end", type=float, default=0.1)
    p.add_argument("--no-grad-checkpointing", action="store_true")
    p.add_argument("--unfreeze-embeddings", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--limit", type=int, default=0, help="debug: use only N training rows")
    args = p.parse_args()

    from huggingface_hub import snapshot_download
    from safetensors.torch import load_file, save_file
    from transformers import AutoTokenizer
    from laya.agent import _fix_tokenizer_config
    from laya.common import QTYPE_NAMES, build_model, proper_reward, temp_bucket

    random.seed(args.seed); torch.manual_seed(args.seed)
    dataset_dir = Path(args.dataset)
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["state_format"] != STATE_FORMAT:
        raise SystemExit(f"dataset state format {manifest['state_format']} != code {STATE_FORMAT}; rebuild the dataset")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    dtype = torch.bfloat16 if device.type == "cuda" and torch.cuda.get_device_capability()[0] >= 8 else torch.float16

    if args.init:
        model_dir, base_revision = args.init, None
        prior = json.loads(Path(args.init, "rl_agent_config.json").read_text(encoding="utf-8"))
    elif os.path.isdir(args.base):
        model_dir = os.path.join(args.base, args.subfolder) if args.subfolder else args.base
        base_revision = None
    else:
        prefix = f"{args.subfolder}/" if args.subfolder else ""
        snap = snapshot_download(args.base, allow_patterns=[prefix + n for n in ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")])
        model_dir = os.path.join(snap, args.subfolder) if args.subfolder else snap
        base_revision = Path(snap).name
    _fix_tokenizer_config(model_dir)
    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    cfg = json.loads(Path(model_dir, "rl_agent_config.json").read_text(encoding="utf-8"))
    cfg.update(max_len=args.max_len, head_max_len=args.head_max_len)
    model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder"))
    model.load_state_dict(load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    try:
        model.encoder.config.reference_compile = False
    except Exception:
        pass
    if not args.unfreeze_embeddings:
        model.encoder.embeddings.tok_embeddings.weight.requires_grad_(False)
    if not args.no_grad_checkpointing:
        model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.head_checkpointing = True
    model.to(device).train()

    splits = [s.strip() for s in args.train_splits.split(",") if s.strip()]
    train_rows = load_rows(dataset_dir, splits)
    if args.limit:
        random.Random(args.seed).shuffle(train_rows)
        train_rows = train_rows[:args.limit]
    calib_rows = load_rows(dataset_dir, ["calib"])
    train, dropped = encode_rows(train_rows, tok, args.max_len, args.head_max_len)
    calib, dropped_c = encode_rows(calib_rows, tok, args.max_len, args.head_max_len)
    log(f"train items {len(train)} (dropped {dropped}), calib {len(calib)} (dropped {dropped_c}), device {device} {dtype}")
    log(f"sequence tokens: max {max(len(i['ids']) for i in train)}, mean {sum(len(i['ids']) for i in train) / len(train):.0f}")

    trainable = [(n, q) for n, q in model.named_parameters() if q.requires_grad]
    log(f"trainable parameters {sum(q.numel() for _, q in trainable) / 1e6:.1f}M of {sum(q.numel() for q in model.parameters()) / 1e6:.1f}M")
    optimizer = torch.optim.AdamW([
        {"params": [q for n, q in trainable if n.startswith("encoder.")], "lr": args.lr_encoder},
        {"params": [q for n, q in trainable if not n.startswith("encoder.")], "lr": args.lr_head}], weight_decay=0.01)
    n_outcome = sum(it["outcome"] for it in train)
    per_epoch = (int((len(train) - n_outcome) * min(1.0, args.teacher_fraction))
                 + int(n_outcome * min(1.0, args.outcome_fraction)) * args.oversample_outcome)
    steps_per_epoch = math.ceil(math.ceil(per_epoch / args.micro_batch) / args.grad_accum)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=[args.lr_encoder, args.lr_head],
                                                    total_steps=max(1, steps_per_epoch * args.epochs), pct_start=0.06, cycle_momentum=False,
                                                    anneal_strategy="cos")
    rng = random.Random(args.seed)
    history, best = [], None
    started = time.time()
    for epoch in range(args.epochs):
        sigma = args.sigma_start + (args.sigma_end - args.sigma_start) * (epoch / max(1, args.epochs - 1))
        teacher_items = [it for it in train if not it["outcome"]]
        outcome_items = [it for it in train if it["outcome"]]
        epoch_items = (rng.sample(teacher_items, int(len(teacher_items) * min(1.0, args.teacher_fraction)))
                       + rng.sample(outcome_items, int(len(outcome_items) * min(1.0, args.outcome_fraction))) * args.oversample_outcome)
        chunks = batches(epoch_items, args.micro_batch, rng)
        optimizer.zero_grad(set_to_none=True)
        run_loss, run_n = 0.0, 0
        for b, chunk in enumerate(chunks):
            batch = collate([epoch_items[i] for i in chunk], tok.pad_token_id)
            logits, act = forward(model, batch, device, dtype)
            mask = batch["marker_mask"].to(device)
            target = batch["target"].to(device)
            qtype = batch["qtype"].to(device)
            weight = torch.where(batch["outcome"].to(device), torch.tensor(args.outcome_weight, device=device),
                                 torch.tensor(1.0, device=device))
            k = mask.sum(-1, keepdim=True).float()
            eps = torch.randn((args.group_size,) + logits.shape, device=device) * sigma * mask
            eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
            z = logits.detach().unsqueeze(0) + eps
            q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
            with torch.no_grad():
                reward = proper_reward(q, target.unsqueeze(0), qtype, mask, w_sph=0.75, w_rps=1.0)
                adv = reward - reward.mean(0, keepdim=True)
                adv = adv / (adv.std() + 1e-6)
            logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
            loss_rl = -((adv * logp).mean(0) * weight).sum() / weight.sum()
            loss_ce = -((target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1) * weight).sum() / weight.sum()
            loss = (args.rl_weight * loss_rl + loss_ce) / args.grad_accum + 0.0 * act.sum()
            loss.backward()
            run_loss += float(loss_ce); run_n += 1
            if (b + 1) % args.grad_accum == 0 or b + 1 == len(chunks):
                torch.nn.utils.clip_grad_norm_([q for _, q in trainable], 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            if (b + 1) % 100 == 0:
                done = (epoch * len(chunks) + b + 1) / (args.epochs * len(chunks))
                eta = (time.time() - started) / done * (1 - done)
                log(f"epoch {epoch + 1} batch {b + 1}/{len(chunks)} soft-CE {run_loss / run_n:.4f} sigma {sigma:.2f} eta {eta / 60:.1f} min")
                run_loss, run_n = 0.0, 0
        metrics = calib_metrics(predict_logits(model, calib, tok.pad_token_id, device, dtype), calib)
        score = sum(v["ce"] * v["n"] for v in metrics.values()) / max(1, sum(v["n"] for v in metrics.values()))
        history.append({"epoch": epoch + 1, "calib": metrics, "calib_mean_ce": round(score, 4), "elapsed_min": round((time.time() - started) / 60, 1)})
        log(f"epoch {epoch + 1} calib {json.dumps(metrics)}")
        if best is None or score < best[0]:
            best = (score, epoch + 1, {k: v.detach().to("cpu", copy=True) for k, v in model.state_dict().items()})
    log(f"best epoch {best[1]} (calib mean CE {best[0]:.4f})")
    model.load_state_dict(best[2])
    del optimizer
    torch.cuda.empty_cache() if device.type == "cuda" else None

    # Temperatures on the held-out calibration split: one per (type, option-count bucket), with
    # a per-type fallback. The inherited bucket values are replaced, not kept.
    calib_logits = predict_logits(model, calib, tok.pad_token_id, device, dtype)
    by_bucket, by_type = {}, {}
    for z, it in zip(calib_logits, calib):
        by_bucket.setdefault(temp_bucket(it["qtype"], len(it["markers"])), []).append((z, it["target"]))
        by_type.setdefault(it["qtype"], []).append((z, it["target"]))
    temperature = [fit_temperature(by_type.get(t, [])) or 1.0 for t in range(3)]
    temperature_by_options = {b: v for b, pairs in by_bucket.items() if (v := fit_temperature(pairs)) is not None}
    log(f"temperatures {dict(zip(QTYPE_NAMES.values(), temperature))} buckets {temperature_by_options}")

    coverage = {}
    for row in train_rows:
        entry = coverage.setdefault(row["question_key"], {"question_id": row["question_id"], "stages": [], "examples": 0,
                                                          "supervision": row["supervision"]})
        entry["examples"] += 1
        if row["stage"] not in entry["stages"]:
            entry["stages"].append(row["stage"])
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    save_file({k: v.half().contiguous() for k, v in model.state_dict().items()}, str(out / "model.safetensors"))
    model.encoder.config.save_pretrained(str(out / "encoder"))
    tok.save_pretrained(str(out / "tokenizer"))
    base_model = (prior.get("base_model") if args.init else None) or (f"{args.base}/{args.subfolder}" if args.subfolder else args.base)
    cfg.update({"model_name": out.name, "fine_tuned": True, "base_model": base_model, "base_revision": base_revision,
                "initialized_from": str(Path(args.init).resolve()) if args.init else None,
                "temperature": temperature, "temperature_by_options": temperature_by_options,
                "bot_state_format": STATE_FORMAT, "bot_question_coverage": coverage,
                "bot_forecast": {k: manifest["forecast"][k] for k in ("question_id", "question_key", "horizon_seconds", "levels", "level_values_pct")},
                "bot_dataset": {"version": manifest["dataset_version"], "sha256": manifest["dataset_sha256"],
                                "train_splits": splits, "rows_used": len(train), "sources": manifest["sources"]},
                "bot_training": {**{k: v for k, v in vars(args).items() if k not in ("dataset", "output")},
                                 "best_epoch": best[1], "history": history, "minutes": round((time.time() - started) / 60, 1),
                                 "device": torch.cuda.get_device_name() if device.type == "cuda" else "cpu",
                                 "embeddings_frozen": not args.unfreeze_embeddings},
                "bot_policy": {}})
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
