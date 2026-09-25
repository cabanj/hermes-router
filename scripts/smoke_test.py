#!/usr/bin/env python3
"""Smoke-test a model with a real chat completion via the router.

Usage: python smoke_test.py <model_id> [timeout_seconds]
Returns: {"ok": bool, "provider": str|None, "error": str|None, "latency_ms": int}
Exit 0 on success, 1 on failure.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROUTER_URL = "https://135-125-233-237.sslip.io/v1/chat/completions"
ROUTER_KEY_PATH = "/opt/hermes-router/.router-key"
ROUTER_ENV_PATH = "/opt/hermes-router/.env"
TEST_PROMPT = "Reply with exactly: SMOKE-OK"
# Reasoning models (Laguna, nemotron, space-bunny) spend max_tokens on
# thinking BEFORE writing content — 20 leaves content empty and reads as a dead
# model. 300 is what router_audit.py uses; keep the two in sync.
MAX_TOKENS = 300


def _router_api_key():
    """Read the router API key: prefer .router-key, fall back to .env.

    The audit resolves the key the same way. Without the fallback this script
    always fails on hosts that keep the key only in .env (the VPS has no
    .router-key)."""
    try:
        return Path(ROUTER_KEY_PATH).read_text().strip()
    except OSError:
        pass
    try:
        with open(ROUTER_ENV_PATH) as f:
            for line in f:
                if line.startswith("ROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def smoke_test(model_id, timeout=45):
    key = _router_api_key()
    if not key:
        return False, None, "no router key (.router-key or .env)", 0

    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    })

    t0 = time.time()
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), ROUTER_URL,
             "-H", f"Authorization: Bearer {key}",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        latency_ms = int((time.time() - t0) * 1000)
        data = json.loads(r.stdout)
        if "choices" in data and data["choices"]:
            msg = data["choices"][0]["message"]
            # Check BOTH fields: a reasoning model that runs out of tokens mid-thought
            # leaves content empty but still answers inside reasoning_content.
            content = ((msg.get("content") or "") + " " +
                       (msg.get("reasoning_content") or "")).strip()
            provider = data.get("provider", "(unknown)")
            if "SMOKE-OK" in content:
                return True, provider, None, latency_ms
            return False, provider, f"unexpected content: {content[:60]}", latency_ms
        err = data.get("error", {}).get("message", "no choices")[:120]
        return False, None, err, latency_ms
    except json.JSONDecodeError as e:
        return False, None, f"json: {e}", 0
    except subprocess.TimeoutExpired:
        return False, None, "timeout", 0


def main():
    model = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    ok, provider, err, lat = smoke_test(model, timeout)
    print(json.dumps({"model": model, "ok": ok, "provider": provider,
                      "error": err, "latency_ms": lat}, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()