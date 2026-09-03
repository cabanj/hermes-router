#!/usr/bin/env python3
"""Router audit: fetch free models, rank them, diff vs current config, apply.

Usage:
  python router_audit.py            # dry-run
  python router_audit.py --apply    # actually deploy

Scoring per model:
  + present on all 3 sources = +30, on 2 = +20, on 1 = +10
  + context >= 1M = +25, >= 256K = +15, >= 128K = +10
  + provider priority: Nous=0, OpenCode=1, OpenRouter=2 -> +(2-p)*10
  + category match (general/coding/fast) = +15
  + tools support = +5
  + smoke-test pass = +10, fail = -50 (in verify phase)

Categories (4 aliases):
  free-general  : high context, reasoning, multi-modal
  free-fast     : lightning, flash, nano, mini
  free-coding   : laguna, north-mini, devstral
  free-fallback : reliable, widely available, not category-specific
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROUTER_URL = "https://135-125-233-237.sslip.io/v1"
ROUTER_KEY_PATH = "/opt/hermes-router/.router-key"
CONFIG_PATH = "/opt/hermes-router/config.yaml"
DATA_DIR = "/opt/hermes-router/data"
CRON_LOG = "/opt/hermes-router/cron/audit.log"

# Always exclude these — proven broken
KNOWN_BROKEN_IDS = frozenset({
    "z-ai/glm-5.2:free", "z-ai/glm-5.2",
    "thinking-machines/inkling:free", "thinking-machines/inkling",
    "thinking-machines/inkling-small:free",
    # OpenRouter spells provider without hyphen: thinkingmachines
    "thinkingmachines/inkling:free", "thinkingmachines/inkling",
    "thinkingmachines/inkling-small:free", "thinkingmachines/inkling-small",
    "stepfun/step-3.7-flash", "stepfun/step-3.7-flash:free",
    "tencent/hy3:free", "tencent/hy3", "hy3-free",  # not free anywhere (Nous=400, Zen=unsupported, OR=paywall)
    "upstage/solar-pro4:free", "upstage/solar-pro4",  # Nous: listed but returns 400 "missing tags"
    # Image-only models (don't fit text-only router)
    "google/lyria-3-clip-preview", "google/lyria-3-pro-preview",
})


def _is_known_broken(model_id):
    """Check if a model ID is in the known-broken set (case-insensitive,
    tolerant of missing/extra hyphens in the provider slug)."""
    def norm(s):
        s = s.lower().strip()
        base = s.replace(":free", "").replace("-free", "")
        # also compare with hyphens stripped entirely (provider spelling varies)
        return base, base.replace("-", "")

    mid, mid_flat = norm(model_id)
    known = {norm(x)[0] for x in KNOWN_BROKEN_IDS}
    known_flat = {norm(x)[1] for x in KNOWN_BROKEN_IDS}
    return mid in known or mid_flat in known_flat or mid in KNOWN_BROKEN_IDS


# OpenCode Zen ID -> OpenRouter equivalent (kept as reference; Zen now works
# directly via OPENCODE_FREE_API_KEY so this is unused)

CATEGORY_KEYWORDS = {
    "coding": ["laguna", "north-mini", "devstral", "coding", "code-"],
    "fast": ["lightning", "flash", "nano", "lfm", "mimo"],
    "general": ["longcat", "nemotron-3-ultra", "nemotron-3-super",
                "minimax-m3", "minimax-m2", "dots3"],
}

PROVIDER_PRIORITY = {"nous": 0, "opencode-zen": 1, "openrouter": 2}

ALIAS_ORDER = ["free-general", "free-fast", "free-coding", "free-fallback"]

# Alias -> category keywords (what the model must match to be ranked for this alias)
ALIAS_CATEGORIES = {
    "free-general": "general",
    "free-fast": "fast",
    "free-coding": "coding",
    "free-fallback": "reliable",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(CRON_LOG), exist_ok=True)
        with open(CRON_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _router_api_key():
    """Read router API key: prefer local .router-key, fall back to .env ROUTER_API_KEY."""
    try:
        return Path(ROUTER_KEY_PATH).read_text().strip()
    except OSError:
        pass
    try:
        with open("/opt/hermes-router/.env") as f:
            for line in f:
                if line.startswith("ROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-router-audit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fetch_source(url, timeout=30):
    try:
        return _get_json(url, timeout), None
    except Exception as e:
        return None, str(e)


def smoke_test(model_id, timeout=45):
    """Smoke-test a model via the router. Returns (ok, provider, latency_ms)."""
    key = _router_api_key()
    if not key:
        return False, None, 0

    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: SMOKE-OK"}],
        "max_tokens": 300,
        "temperature": 0,
    })

    t0 = time.time()
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             f"{ROUTER_URL}/chat/completions",
             "-H", f"Authorization: Bearer {key}",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        lat = int((time.time() - t0) * 1000)
        data = json.loads(r.stdout)
        if "choices" in data and data["choices"]:
            msg = data["choices"][0]["message"]
            content = ((msg.get("content") or "") + " " + (msg.get("reasoning_content") or ""))
            provider = data.get("provider", "(unknown)")
            return "SMOKE-OK" in content, provider, lat
        return False, None, lat
    except Exception:
        return False, None, 0


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def _fetch_source(url, timeout=30):
    """Fetch JSON. Returns (data, error_str)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-router-audit/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), ""
    except Exception as e:
        return None, str(e)


def _normalize_model_id(raw_id):
    """Strip :free suffix and other cosmetic differences so models from different
    sources dedup correctly. OpenRouter: 'upstage/solar-pro4', Nous: 'upstage/solar-pro4:free'."""
    mid = raw_id.strip()
    for suffix in (":free", "-free"):
        if mid.endswith(suffix):
            mid = mid[: -len(suffix)]
    return mid


def _normalize_model(m, source):
    """Normalize a raw catalog entry to our model dict. Returns None if broken."""
    raw_id = m.get("id", "")
    mid = _normalize_model_id(raw_id)
    if _is_known_broken(mid):
        return None
    arch = m.get("architecture") or {}
    ctx = (m.get("top_provider") or {}).get("context_length") or m.get("context_length", 0)
    return {
        "id": mid,
        "raw_by_source": {source: raw_id},
        "name": m.get("name", mid),
        "description": (m.get("description") or "")[:200],
        "context_length": int(ctx or 0),
        "modality": arch.get("modality", "text"),
        "tools": "tools" in (m.get("supported_parameters") or []),
        "source": source,
    }


def _fetch_source(url, timeout=30):
    """Fetch JSON. Returns (data, error_str)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-router-audit/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), ""
    except Exception as e:
        return None, str(e)


def _is_free(pricing):
    """Check if a pricing dict is truly free (all fields 0 or absent)."""
    try:
        prompt = float(pricing.get("prompt", 0))
        completion = float(pricing.get("completion", 0))
        cache_read = float(pricing.get("input_cache_read", 0))
        cache_write = float(pricing.get("input_cache_write", 0))
    except (TypeError, ValueError):
        return False
    return prompt == 0 and completion == 0 and cache_read == 0 and cache_write == 0


def fetch_openrouter():
    data, err = _fetch_source("https://openrouter.ai/api/v1/models")
    if err or data is None:
        return [], f"openrouter: {err or 'no data'}"
    out = []
    for m in data.get("data", []):
        if not _is_free(m.get("pricing", {})):
            continue
        nm = _normalize_model(m, "openrouter")
        if nm:
            out.append(nm)
    return out, ""


def fetch_nous_portal():
    data, err = _fetch_source("https://inference-api.nousresearch.com/v1/models")
    if err or data is None:
        return [], f"nous: {err or 'no data'}"
    out = []
    for m in data.get("data", []):
        if not _is_free(m.get("pricing", {})):
            continue
        nm = _normalize_model(m, "nous")
        if nm:
            out.append(nm)
    return out, ""


def fetch_opencode_zen():
    zen_ids = [
        "x-preview-f-free", "mimo-v2.5-free", "nemotron-3-ultra-free",
        "nemotron-3.5-lightning-free", "muse-spark-1.2-contributor-free",
    ]
    data, err = _fetch_source("https://opencode.ai/zen/v1/models")
    if err or data is None:
        return [], f"opencode-zen: {err or 'no data'}"
    listed = {m["id"] for m in data.get("data", [])}
    out = []
    for mid in zen_ids:
        if mid not in listed:
            continue
        if _is_known_broken(mid):
            continue
        out.append({
            "id": mid, "name": mid, "description": "",
            "context_length": 0, "modality": "text",
            "tools": False, "source": "opencode-zen",
        })
    return out, ""


def fetch_all():
    sources = [
        ("openrouter", fetch_openrouter),
        ("nous", fetch_nous_portal),
        ("opencode-zen", fetch_opencode_zen),
    ]
    all_models = []
    statuses = {}
    for name, fn in sources:
        models, err = fn()
        statuses[name] = {"ok": err == "", "count": len(models), "error": err}
        log(f"  {name}: {len(models)} models" + (f" (ERR: {err})" if err else ""))
        all_models.extend(models)

    # Merge by normalized ID (strip :free/-free suffix for dedup).
    # raw_by_source keeps the EXACT id each source lists — OpenRouter requires
    # exact forms (dots-*:free only WITH suffix, longcat only WITHOUT).
    return merge_models(all_models), statuses


def _has_free_suffix(raw_id):
    return ":free" in raw_id or "-free" in raw_id


def merge_models(all_models):
    """Pure merge: dedup by normalized ID, union sources, keep exact per-source IDs."""
    merged = {}
    for m in all_models:
        key = _normalize_model_id(m["id"])
        src = m.get("source", "openrouter")
        own = dict(m.get("raw_by_source") or {src: m.get("id", key)})
        if key not in merged:
            merged[key] = {**m, "id": key, "sources": [m["source"]], "raw_by_source": own}
        else:
            merged[key]["sources"].append(m["source"])
            # Same source, both forms listed (OpenRouter does this): prefer the
            # bare form — ':free' maps to a rate-limited pool (verified gemma).
            prev = merged[key]["raw_by_source"].get(src)
            new = own.get(src)
            if prev is None or (_has_free_suffix(prev) and new and not _has_free_suffix(new)):
                merged[key]["raw_by_source"][src] = new
            if m["context_length"] > merged[key]["context_length"]:
                merged[key]["context_length"] = m["context_length"]
            if not merged[key]["description"] and m["description"]:
                merged[key]["description"] = m["description"]
            if m["tools"]:
                merged[key]["tools"] = True
    return list(merged.values())


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def classify_category(model):
    """Assign a primary category to a model. Uses ID only — descriptions are too noisy."""
    hay = model["id"].lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in hay for k in kws):
            return cat
    return "general"


def score_model(model, category):
    score = 0
    sources = model.get("sources", [])

    if len(sources) >= 3:
        score += 30
    elif len(sources) >= 2:
        score += 20
    else:
        score += 10

    ctx = model.get("context_length", 0)
    if ctx >= 1_000_000:
        score += 25
    elif ctx >= 256_000:
        score += 15
    elif ctx >= 128_000:
        score += 10

    best_source = min(sources, key=lambda s: PROVIDER_PRIORITY.get(s, 9))
    score += (2 - PROVIDER_PRIORITY.get(best_source, 2)) * 10

    if classify_category(model) == category:
        score += 15

    if model.get("tools"):
        score += 5

    score += bench_bonus(model.get("id", ""), category)

    return score


# Mirror of model-wiki-automation/bench.py AA_ALIASES (normalized roster id
# -> exact AA model name). Keep in sync manually; used only for ordering bonus.
AA_ALIASES = {
    "z-ai/glm-5.2": "GLM-5.2 (max)",
    "meituan/longcat-2.0": "LongCat 2.0",
    "upstage/solar-pro4": "Solar Pro 4",
    "mimo-v2.5": "MiMo-V2.5",
    "liquid/lfm-2.5-2.6b": "LFM2 2.6B",
    "cohere/north-mini-code": "North Mini Code",
    "google/gemma-4-26b-a4b-it": "Gemma 4 26B A4B (Reasoning)",
    "google/gemma-4-31b-it": "Gemma 4 31B (Non-reasoning)",
    "nvidia/nemotron-3-nano-30b-a3b": "NVIDIA Nemotron 3 Nano 30B A3B (Reasoning)",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": "Nemotron 3 Nano Omni 30B A3B Reasoning",
    "nvidia/nemotron-3-super-120b-a12b": "Nemotron 3 Super 120B A12B (Reasoning)",
    "nvidia/nemotron-3-ultra-550b-a55b": "Nemotron 3 Ultra 550B A55B (Reasoning)",
    "nemotron-3-ultra": "Nemotron 3 Ultra 550B A55B (Reasoning)",
    "nemotron-3.5-lightning": "Nemotron 3.5 Lightning",
    "nvidia/nemotron-3.5-lightning": "Nemotron 3.5 Lightning",
    "nvidia/nemotron-nano-12b-v2-vl": "NVIDIA Nemotron Nano 12B v2 VL (Reasoning)",
    "nvidia/nemotron-nano-9b-v2": "NVIDIA Nemotron Nano 9B V2 (Non-reasoning)",
    "stepfun/step-3.7-flash": "Step 3.7 Flash",
    "tencent/hy3": "Hy3",
    "thinkingmachines/inkling": "Inkling (xhigh)",
    "thinkingmachines/inkling-small": "Inkling Small",
    "muse-spark-1.2-contributor": "Muse Spark 1.2 (xhigh)",
}

# Alias category -> AA benchmark field used for the ordering bonus.
BENCH_FIELDS = {
    "general": "artificial_analysis_intelligence_index",
    "coding": "artificial_analysis_coding_index",
    "fast": "artificial_analysis_intelligence_index",
    "reliable": "artificial_analysis_intelligence_index",
}

_BENCH_CACHE = None


def load_bench_scores():
    """Load {AA name: {intel, coding}} from the wiki benchmarks cache.

    Same host (/opt/model-wiki-automation). Missing file -> {} (bonus 0)."""
    global _BENCH_CACHE
    if _BENCH_CACHE is not None:
        return _BENCH_CACHE
    _BENCH_CACHE = {}
    try:
        with open("/opt/model-wiki-automation/data/benchmarks-cache.json") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _BENCH_CACHE
    for a in data.get("data", []):
        name = a.get("name", "")
        if name:
            _BENCH_CACHE[name] = a
    return _BENCH_CACHE


def bench_bonus(model_id, category):
    """Small ordering bonus from wiki benchmark scores (0 if no data).

    Capped below availability signals: a dead model must still lose to a
    live one. Blacklisted models never reach here (filtered at fetch)."""
    field = BENCH_FIELDS.get(category)
    if not field:
        return 0
    aa_name = AA_ALIASES.get(model_id)
    if not aa_name:
        return 0
    entry = load_bench_scores().get(aa_name)
    if not entry:
        return 0
    try:
        value = float(entry.get(field) or 0)
    except (TypeError, ValueError):
        return 0
    return min(value / 10.0, 7.0)


def rank_models(models, category, top_n=5):
    """Return top N models for a category, sorted by score.
    Models NOT matching category are excluded (except fallback which uses all)."""
    if category == "general":
        # general: include all models not classified as fast/coding-specific
        candidates = [m for m in models if classify_category(m) not in ("fast", "coding")]
    elif category == "reliable":
        candidates = models
    else:
        # fast/coding: only models matching the category
        candidates = [m for m in models if classify_category(m) == category]

    if not candidates:
        # Fallback if no category match — use all models
        candidates = models

    scored = [(score_model(m, category), m) for m in candidates]
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return scored[:top_n]


def pick_source_id(model, best_source):
    """Exact model ID as listed by the winning source (no suffix guessing)."""
    return model.get("raw_by_source", {}).get(best_source, model["id"])


def build_new_chains(models):
    """Build alias chains with source info for each model."""
    SOURCE_CONFIG = {
        "opencode-zen": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "os.environ/OPENCODE_FREE_API_KEY",
        },
        "nous": {
            "api_base": "https://inference-api.nousresearch.com/v1",
            "api_key": "os.environ/NOUS_PORTAL_API_KEY",
        },
        "openrouter": {
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": "os.environ/OPENROUTER_API_KEY",
        },
    }

    # For each model, pick best source.
    # Priorities: opencode-zen > nous > openrouter.
    # (Zen requires a real key — OPENCODE_FREE_API_KEY, present in .env.)
    source_priority = {"opencode-zen": 0, "nous": 1, "openrouter": 2}

    chains = {}
    for alias in ALIAS_ORDER:
        category = ALIAS_CATEGORIES.get(alias, "general")
        ranked = rank_models(models, category, top_n=5)
        chains[alias] = []
        for score, m in ranked:
            # Pick best source for this model
            best_source = min(m.get("sources", ["openrouter"]),
                              key=lambda s: source_priority.get(s, 9))
            # Exact ID as listed by the winning source (no suffix guessing)
            model_id = pick_source_id(m, best_source)
            chains[alias].append({
                "id": model_id,
                "score": score,
                "sources": m.get("sources", []),
                "source": best_source,
                "context_length": m.get("context_length", 0),
                "api_base": SOURCE_CONFIG.get(best_source, SOURCE_CONFIG["openrouter"])["api_base"],
                "api_key": SOURCE_CONFIG.get(best_source, SOURCE_CONFIG["openrouter"])["api_key"],
            })
    return chains


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------
def load_current_config():
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
    except OSError:
        return {}

    chains = {}
    current_alias = None
    for line in content.split("\n"):
        if "model_name:" in line:
            current_alias = line.split("model_name:")[1].strip()
            chains[current_alias] = []
        elif "model:" in line and current_alias and "openai/" in line:
            mid = line.split("openai/")[1].strip()
            chains[current_alias].append(mid)
    return chains


def diff_chains(old, new):
    changes = []
    for alias in ALIAS_ORDER:
        old_ids = old.get(alias, [])
        new_ids = [m["id"] for m in new.get(alias, [])]
        if old_ids != new_ids:
            changes.append({"alias": alias, "old": old_ids, "new": new_ids})
    return changes


def generate_config_yaml(chains):
    lines = ["model_list:"]
    comments = {
        "free-general": "free-general (Nous Portal / OpenCode prioritized over OpenRouter)",
        "free-fast": "free-fast (lightweight, fast first-token)",
        "free-coding": "free-coding (coding-specialized models)",
        "free-fallback": "free-fallback (reliable last resort)",
    }
    for alias in ALIAS_ORDER:
        models = chains.get(alias, [])
        lines.append(f"  # ---------- {comments.get(alias, alias)} ----------")
        for m in models:
            lines.append(f"  - model_name: {alias}")
            lines.append(f"    litellm_params:")
            lines.append(f"      model: openai/{m['id']}")
            lines.append(f"      api_base: {m['api_base']}")
            lines.append(f"      api_key: {m['api_key']}")
            lines.append(f"      timeout: 90")
            lines.append(f"      num_retries: 1")
            lines.append(f"      tags: [\"{alias.replace('free-', '')}\", \"{m['source']}\"]")
        lines.append("")
    lines.append("litellm_settings:")
    lines.append("  drop_params: true")
    lines.append("  request_timeout: 120")
    lines.append("  num_retries: 1")
    lines.append("  retry_policy:")
    lines.append('    "429": [429]')
    lines.append('    "500": [500, 502, 503, 504]')
    lines.append("  allowed_fails: 3")
    lines.append("  cooldown_time: 60")
    lines.append("")
    lines.append("router_settings:")
    lines.append("  fallbacks:")
    for alias in ALIAS_ORDER:
        if alias == "free-fallback":
            continue
        lines.append(f"  - {alias}: [free-fallback]")
    lines.append("")
    lines.append("general_settings:")
    lines.append("  health_check_interval: 60")
    lines.append("")
    lines.append("logging: verbose")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Apply + verify
# ---------------------------------------------------------------------------
def backup_config():
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = f"/opt/hermes-router/configs/{ts}"
    os.makedirs(backup_dir, exist_ok=True)
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
        with open(f"{backup_dir}/config.yaml", "w") as f:
            f.write(content)
        log(f"  backed up config to {backup_dir}")
        return backup_dir
    except OSError as e:
        log(f"  backup failed: {e}")
        return None


def apply_config(new_yaml):
    try:
        with open(CONFIG_PATH, "w") as f:
            f.write(new_yaml)
        log("  wrote new config.yaml")
    except OSError as e:
        log(f"  write failed: {e}")
        return False
    try:
        r = subprocess.run(
            ["docker", "compose", "restart", "litellm"],
            capture_output=True, text=True, timeout=30,
            cwd="/opt/hermes-router",
        )
        if r.returncode != 0:
            log(f"  restart failed: {r.stderr[:200]}")
            return False
        log("  restarted litellm")
    except Exception as e:
        log(f"  restart error: {e}")
        return False
    time.sleep(15)
    # health check with retries — container startup can be slow
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["curl", "-sf", "http://localhost:4000/health/readiness"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                log("  health check passed")
                break
        except Exception as e:
            log(f"  health check error: {e}")
        time.sleep(6)
    else:
        log("  health check failed after 3 attempts")
        return False

    # Give LiteLLM a moment to settle (cooldowns from startup probes)
    time.sleep(3)
    return True


def _upstream_key(env_name):
    """Read an upstream API key from /opt/hermes-router/.env (never logged)."""
    try:
        with open("/opt/hermes-router/.env") as f:
            for line in f:
                if line.startswith(env_name + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def smoke_test_upstream(model_id, api_base, key_env, timeout=60):
    """Probe one deployment directly at its upstream (bypasses the router).
    Returns (ok, dead, latency_ms). dead=True only for structural errors
    (404 / model-not-found / missing tags) — rate limits and flakes are
    transient (dead=False) and must NOT fail the audit: 429 is exactly
    what fallback chains are for."""
    key = _upstream_key(key_env)
    if not key:
        return False, False, 0
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: SMOKE-OK"}],
        "max_tokens": 300,
        "temperature": 0,
    })
    t0 = time.time()
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             f"{api_base}/chat/completions",
             "-H", f"Authorization: Bearer {key}",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        lat = int((time.time() - t0) * 1000)
        data = json.loads(r.stdout)
        if "choices" in data and data["choices"]:
            msg = data["choices"][0]["message"]
            content = ((msg.get("content") or "") + " " + (msg.get("reasoning_content") or ""))
            return "SMOKE-OK" in content, False, lat
        err = data.get("error", {})
        code = err.get("code")
        msg = str(err.get("message", "")).lower()
        dead = (code == 404 or "missing tags" in msg
                or "not found" in msg or "model_not_found" in msg)
        return False, dead, lat
    except Exception:
        return False, False, 0


def verify_chains(chains):
    """Verify each alias through the router AND every chain member at its
    upstream. A dead backup fails the audit (triggers rollback)."""
    log("  verifying chains (alias + every member upstream)...")
    all_ok = True
    for alias in ALIAS_ORDER:
        models = chains.get(alias, [])
        if not models:
            log(f"    {alias}: EMPTY CHAIN")
            all_ok = False
            continue
        ok, provider, lat = smoke_test(alias, timeout=60)
        if not ok:
            # one retry — transient cooldowns right after restart are common
            time.sleep(4)
            ok, provider, lat = smoke_test(alias, timeout=60)
        status = "OK" if ok else "FAIL"
        primary = models[0]["id"]
        log(f"    {alias} (primary: {primary}) -> {provider} ({lat}ms) [{status}]")
        if not ok:
            all_ok = False
        for m in models:
            env_name = (m.get("api_key") or "").replace("os.environ/", "")
            u_ok, u_dead, u_lat = smoke_test_upstream(m["id"], m.get("api_base", ""),
                                                      env_name, timeout=60)
            if not u_ok and not u_dead:
                # one retry — upstream rate limits (Zen FreeUsageLimitError)
                # and transient flakes are common; only a repeat FAIL counts
                time.sleep(4)
                u_ok, u_dead, u_lat = smoke_test_upstream(m["id"], m.get("api_base", ""),
                                                          env_name, timeout=60)
            if u_ok:
                u_status = "OK"
            elif u_dead:
                u_status = "DEAD"
            else:
                u_status = "LIMITED"
            log(f"      member {m['id']} [{m.get('source')}] ({u_lat}ms) [{u_status}]")
            if u_dead:
                all_ok = False
    return all_ok


def rollback(backup_dir):
    if not backup_dir:
        return
    try:
        with open(f"{backup_dir}/config.yaml") as f:
            content = f.read()
        with open(CONFIG_PATH, "w") as f:
            f.write(content)
        subprocess.run(
            ["docker", "compose", "restart", "litellm"],
            capture_output=True, text=True, timeout=30,
            cwd="/opt/hermes-router",
        )
        log(f"  rolled back to {backup_dir}")
    except Exception as e:
        log(f"  rollback failed: {e}")


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
def write_changelog(changes, statuses):
    os.makedirs(DATA_DIR, exist_ok=True)
    log_path = os.path.join(DATA_DIR, "changelog.json")
    try:
        with open(log_path) as f:
            history = json.load(f)
    except (OSError, json.JSONDecodeError):
        history = []

    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changes": changes,
        "source_statuses": statuses,
    }
    history.append(entry)
    history = history[-30:]

    with open(log_path, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    return entry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()
    if args.apply:
        args.dry_run = False

    log("=" * 60)
    log("ROUTER AUDIT START (mode: %s)" % ("apply" if args.apply else "dry-run"))
    log("=" * 60)

    log("Step 1: Fetching free models...")
    models, statuses = fetch_all()
    log(f"  total unique free models: {len(models)}")

    log("Step 2: Ranking + building chains...")
    chains = build_new_chains(models)
    for alias in ALIAS_ORDER:
        ids = [m["id"] for m in chains.get(alias, [])]
        log(f"  {alias}: {ids}")

    log("Step 3: Diff vs current config...")
    old_chains = load_current_config()
    changes = diff_chains(old_chains, chains)

    if not changes:
        log("  No changes. Router is up to date.")
        write_changelog([], statuses)
        log("AUDIT COMPLETE (no changes)")
        return 0

    log(f"  Changes in {len(changes)} aliases:")
    for c in changes:
        log(f"    {c['alias']}:")
        log(f"      old: {c['old']}")
        log(f"      new: {c['new']}")

    if args.dry_run:
        log("  DRY-RUN. Run with --apply to deploy.")
        log("AUDIT COMPLETE (dry-run)")
        return 0

    log("Step 4: Backup + apply...")
    backup_dir = backup_config()
    if not backup_dir:
        log("  ABORT: backup failed")
        return 1

    new_yaml = generate_config_yaml(chains)
    if not apply_config(new_yaml):
        log("  ABORT: apply failed, rolling back...")
        rollback(backup_dir)
        return 1

    log("Step 5: Verify chains...")
    if not verify_chains(chains):
        log("  VERIFY FAILED, rolling back...")
        rollback(backup_dir)
        return 1

    log("Step 6: Changelog...")
    entry = write_changelog(changes, statuses)
    log(f"  logged entry {entry['at']}")

    log("=" * 60)
    log("AUDIT COMPLETE (applied)")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
