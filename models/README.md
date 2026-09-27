# Laya checkpoints for the bot

Both are ordinary Laya checkpoint directories (`rl_agent_config.json`, `model.safetensors`, `encoder/`, `tokenizer/`), loadable with `laya.Agent(path)` or through `decision.engine: laya`. The weights are stored with Git LFS; run `git lfs install` before cloning, or `git lfs pull` afterwards.

| Directory | SHA-256 (`model.safetensors`) | Trained on | Status |
|---|---|---|---|
| `laya-bsjev-night/` | `04e877b5ff65c3567ac1090580dd80b332fa48adb003f9fd5aa68450873c224b` | v1 + 8 h of Laya paper-trading outcomes (`data/laya_dataset_night`) | **Default** in `config.laya*.example.yaml` |
| `laya-bsjev/` (v1) | `66a5cfff19375e18a6d01ec6bd95e94b0e2da8de068298e9023c0f4bc8bd9b79` | JEV decisions + outcomes (`data/laya_dataset`) | Rollback; better JEV imitation |

- **Base model:** [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya), subfolder `multilingual` (mmBERT-base encoder, 322M params), snapshot `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`. The base model is Apache-2.0; these fine-tuned weights are a derivative of it.
- **Fine-tuning:** `tools/laya_finetune.py` (Laya's RLCD recipe on one RTX 4070 Laptop GPU, bf16). The 256k-token embedding is frozen and 125M parameters are trained. Temperatures are fitted on the held-out calibration split. Full hyperparameters, history and dataset hashes are in each `rl_agent_config.json` (`bot_training`, `bot_dataset`).
- **Input format:** `bot_state_format = laya-compact-v1` (`decision/laya_state.py`). A checkpoint refuses to load if the code's format differs.
- **Entry policy:** `bot_policy` holds the calibration-selected expected-edge threshold. Both checkpoints report `edge_found_on_calibration: false`, so the bot does not buy.
- **Teacher data caveat:** part of the training targets are TypeSafe JEV outputs obtained through OpenRouter. Check the providers' terms before redistributing these weights.
- **Evaluations:** `reports/laya_eval_v1.json`, `reports/laya_eval_night.json`, `reports/laya_eval_v1_on_night.json`. A rejected forecast-focused stage-2 model (overfit) is documented in `logs/laya_finetune_v2.log` but not included.
