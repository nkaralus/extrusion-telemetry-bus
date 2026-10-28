# Mule Architecture: project memory for Claude Code

Goal: run frontier-scale MoE models on consumer hardware by trading speed for time. A human directs every step. Tests that pass or fail decide, not opinions. Measure everything, and change one thing at a time.

## Machine
- Ubuntu, installed 2026-10-16. Every number below was measured earlier on Windows 11. Re-measure on Ubuntu before comparing.
- RTX 5070 (12 GB VRAM), 16 GB RAM, NVMe SSD holds the models.
- Hugging Face transformers + plain Python only. No Ollama, LM Studio, llama.cpp, or model servers.
- config.json still has old Windows paths (C:/models/...). Update them to the real Ubuntu paths.
- Keep model FOLDER NAMES unchanged (gemma26b-split, qwen36-slim, ...). Heat maps save as results/heatmap_<folder-name>_<tag>.json.

## Files (this folder)
- bench.py: fixed 20-task bench (tasks/tasks.json). Modes: baseline, loop (judge errors fed back, up to 3 tries), check (second model reviews). One JSON line per task to results/runs.jsonl. `python bench.py --dry` tests the judges with no model.
- summary.py (scoreboard), show_fails.py (failed answers), clean_log.py (drops setup-error rows).
- clip_loader.py v0.9.1, the "ammo clip": runs Gemma 4 and Qwen 3.5/3.6 MoE models bigger than VRAM by serving experts from a VRAM cache, a RAM cache, or exact SSD byte reads. Flags: --warm (learned cache from the heat map), --int8 <folder> (8-bit experts), --gpu-cache GB, --ram-cache GB, --profile (time breakdown), --chat (interactive: /run /fix /save /paste /reset), --tag <task type>, --no-prefetch, --exact.
- quantize_experts.py, strip_experts.py, reshard2.py, inspect_experts.py: model preparation. test0_moe.py: stock offload baseline. gpu_microbench.py.
- docs/handbook.md: sample documents for the bench's document questions.

## Commands
- python clip_loader.py <model-folder> --int8 <int8-experts-folder> --tag code --warm --gpu-cache 4.5 --prompt "..."
- python bench.py --model gemma12b --test T1   (add --mode loop for judge feedback)

## Rules learned from measurements
- Keep peak VRAM near 10 GB or less. At 11.2 GB, runs were up to 5x slower. Use --gpu-cache 4.5 with 8-bit experts, 4 with 16-bit.
- RAM cache at most about 6 GB on this 16 GB machine, or the system swaps.
- Run ONE model process at a time.
- Compare runs only with the same prompt and settings. Use --profile before optimizing anything.
- Any change to the expert math must keep output identical to stock transformers. Test on a tiny model first.
- --pin (pinned RAM) crashed on Windows. Untested on Linux, so treat it as experimental.
- Cold-disk timing tests need the page cache dropped first: sync; echo 3 | sudo tee /proc/sys/vm/drop_caches (ask the user first).

## Measured baselines (Windows)
- Bench, Gemma 4 12B 4-bit: 15/20 baseline, 19/20 with the loop.
- Gemma 4 26B A4B through the clip: 2.2-2.5 tok/s at 16-bit; 6.86 tok/s with 8-bit experts, warm, 4.5 GB cache.
- Qwen3.6-35B-A3B, 8-bit experts, cold: 3.3-3.5 tok/s.
- 17% (Gemma) and 21% (Qwen) of experts carry 80% of picks. The next-layer predictor is 79% / 68% precise.

## Working rules
- Ask before: deleting anything, using sudo, downloading models, touching the models folder, or starting a second model.
- Never commit or upload model weights, results/, chat_runs/, API keys, or client documents.
- Report numbers: tokens/sec, peak VRAM, RAM, pass rate. Say when a sample is small or noisy.
- Do not call something a fix until a run shows it. Say when a claim is unproven.
- Style: lead with the answer, short plain sentences, small tables for numbers.

## Not built yet
- Mule worker: a queue of task files the local model works through with the bench judge, saving verified successes for later fine-tuning.
- Checker test (Qwen3.5-9B reviewing the mule), the 20-task bench on the 8-bit 26B, a Qwen warm-start run, heat maps for a second task type.
