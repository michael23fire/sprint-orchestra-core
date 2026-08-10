# vLLM (local, CPU-only) — proof of deployment + LiteLLM routing, not a dev inference server

This is a real, running vLLM server — not a stand-in. Read the caveat below before expecting it to
behave like a production deployment.

## Why this exists, and its one real limitation

`litellm-gateway/config.yaml`'s original `local-chat` route pointed only at LM Studio, documented
there as "representing" a self-hosted vLLM deployment. That's an honest architecture story (multi-
backend routing that would point at a real vLLM instance on GPU infra in production), but it's still
a claim about a tool nobody actually ran. This directory closes that gap: vLLM is actually installed
and actually serving requests through the actual LiteLLM gateway route (`vllm-local`).

**The limitation, stated plainly**: this machine is Apple Silicon (M-series, arm64) with no CUDA or
ROCm GPU. vLLM's core value proposition — PagedAttention and continuous batching for high
*concurrent-request* throughput — is built on GPU kernels that don't exist here. vLLM does run in a
CPU-only mode (confirmed working below), but it gets none of the concurrency/throughput benefit that
justifies choosing vLLM over a simpler server in the first place. This is why the model used here is
tiny (`Qwen/Qwen2.5-0.5B-Instruct`, not the 27B/35B class LM Studio serves for actual local dev) — the
goal is proving the deployment and the gateway routing are real, not using this for day-to-day work.
**In a real cloud deployment, this same config would point at a GPU-backed vLLM instance instead.**

## Setup

```bash
cd vllm-server
python3.11 -m venv .venv
./.venv/bin/pip install vllm     # builds a wheel locally on macOS — no prebuilt PyPI wheel for arm64 Darwin
./.venv/bin/vllm serve Qwen/Qwen2.5-0.5B-Instruct --port 8300 --host 127.0.0.1
```

Startup takes ~90 seconds off this hardware (model download + CPU inductor compilation/warmup —
visible in the server's own log as "Warming up model for the compilation..."). Confirmed working
against `torch==2.10.0` (CPU build) via `vllm==0.19.1`.

## Verified live

Direct against vLLM's own OpenAI-compatible endpoint:

```bash
curl http://127.0.0.1:8300/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "messages": [{"role": "user", "content": "..."}],
  "max_tokens": 150
}'
```

Real measurement, CPU-only, this hardware: 150 completion tokens in 3.29s ≈ **45.6 tok/s** for the
0.5B model — fast for this model size, but not a meaningful comparison against LM Studio's MLX-
accelerated 27B/35B models elsewhere in this project (different model size *and* different
acceleration backend; not an apples-to-apples throughput claim).

Through the LiteLLM gateway (`litellm-gateway/config.yaml`'s `vllm-local` route,
`api_base: http://host.docker.internal:8300/v1`):

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model": "vllm-local", "messages": [{"role": "user", "content": "..."}], "max_tokens": 80}'
```

Confirmed: request routed through the gateway, served by vLLM, real completion returned — the
same "point `api_base` at any OpenAI-compatible server" pattern already used for the LM Studio and
Anthropic routes, now proven against a third, genuinely different backend.
