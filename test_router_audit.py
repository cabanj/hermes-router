import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
import router_audit as R


def _m(mid, source, **kw):
    d = {"id": mid, "name": mid, "description": "", "context_length": 1000,
         "modality": "text", "tools": False, "source": source}
    d.update(kw)
    return d


def test_merge_keeps_exact_per_source_ids():
    merged = R.merge_models([
        _m("upstage/solar-pro4", "openrouter"),
        _m("upstage/solar-pro4:free", "nous"),
    ])
    assert len(merged) == 1
    m = merged[0]
    assert sorted(m["sources"]) == ["nous", "openrouter"]
    assert m["raw_by_source"] == {"openrouter": "upstage/solar-pro4",
                                  "nous": "upstage/solar-pro4:free"}


def test_pick_source_id_exact():
    m = {"id": "x/y", "raw_by_source": {"openrouter": "x/y",
                                         "nous": "x/y:free"}}
    assert R.pick_source_id(m, "openrouter") == "x/y"
    assert R.pick_source_id(m, "nous") == "x/y:free"
    assert R.pick_source_id({"id": "x/y"}, "nous") == "x/y"


def test_merge_prefers_bare_openrouter_form():
    merged = R.merge_models([
        _m("x/gemma:free", "openrouter"),
        _m("x/gemma", "openrouter"),
    ])
    assert len(merged) == 1
    assert merged[0]["raw_by_source"] == {"openrouter": "x/gemma"}
    # reverse order: bare still wins
    merged = R.merge_models([
        _m("x/gemma", "openrouter"),
        _m("x/gemma:free", "openrouter"),
    ])
    assert merged[0]["raw_by_source"] == {"openrouter": "x/gemma"}


def test_verify_chains_probes_all_members():
    probed_aliases = []
    probed_members = []

    orig_smoke, orig_up = R.smoke_test, R.smoke_test_upstream
    R.smoke_test = lambda mid, timeout=60: (probed_aliases.append(mid) or (True, "x", 1))
    R.smoke_test_upstream = lambda mid, base, env, timeout=60: (probed_members.append(mid) or (True, False, 1))
    try:
        chains = {"free-general": [{"id": "a", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"},
                                    {"id": "b", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"},
                                    {"id": "c", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fast": [{"id": "d", "source": "nous",
                                  "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-coding": [{"id": "e", "source": "nous",
                                    "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fallback": [{"id": "f", "source": "nous",
                                      "api_base": "https://x", "api_key": "os.environ/K"}]}
        assert R.verify_chains(chains) is True
    finally:
        R.smoke_test, R.smoke_test_upstream = orig_smoke, orig_up
    assert sorted(probed_aliases) == ["free-coding", "free-fallback", "free-fast", "free-general"]
    assert sorted(probed_members) == ["a", "b", "c", "d", "e", "f"]


def test_verify_chains_fails_on_dead_member():
    orig_smoke, orig_up = R.smoke_test, R.smoke_test_upstream
    R.smoke_test = lambda mid, timeout=60: (True, "x", 1)
    R.smoke_test_upstream = lambda mid, base, env, timeout=60: ((False, True, 1) if mid == "b" else (True, False, 1))
    try:
        chains = {"free-general": [{"id": "a", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"},
                                    {"id": "b", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fast": [{"id": "d", "source": "nous",
                                  "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-coding": [{"id": "e", "source": "nous",
                                    "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fallback": [{"id": "f", "source": "nous",
                                      "api_base": "https://x", "api_key": "os.environ/K"}]}
        assert R.verify_chains(chains) is False
    finally:
        R.smoke_test, R.smoke_test_upstream = orig_smoke, orig_up


def test_load_current_config_keeps_every_chain_member():
    """Regression: model_name: repeats per deployment. Resetting the list there
    kept only the LAST member, so diffs always looked like a full rewrite."""
    orig = R.CONFIG_PATH
    import tempfile, os as _os
    yaml = (
        "model_list:\n"
        "  - model_name: free-general\n"
        "    litellm_params:\n"
        "      model: openai/aaa:free\n"
        "  - model_name: free-general\n"
        "    litellm_params:\n"
        "      model: openai/bbb:free\n"
        "  - model_name: free-general\n"
        "    litellm_params:\n"
        "      model: openai/ccc:free\n"
        "  - model_name: free-fast\n"
        "    litellm_params:\n"
        "      model: openai/ddd\n"
    )
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(yaml)
        R.CONFIG_PATH = path
        chains = R.load_current_config()
        assert chains["free-general"] == ["aaa:free", "bbb:free", "ccc:free"]
        assert chains["free-fast"] == ["ddd"]
        # a stable config must diff clean against itself
        assert R.diff_chains(chains, {"free-general": [{"id": "aaa:free"},
                                                       {"id": "bbb:free"},
                                                       {"id": "ccc:free"}],
                                      "free-fast": [{"id": "ddd"}]}) == []
    finally:
        R.CONFIG_PATH = orig
        _os.unlink(path)


def test_bench_bonus_capped_and_zero_without_data():
    orig = R._BENCH_CACHE
    R._BENCH_CACHE = {
        "LongCat 2.0": {"artificial_analysis_intelligence_index": 65.0,
                         "artificial_analysis_coding_index": 60.0},
    }
    try:
        assert R.bench_bonus("meituan/longcat-2.0", "general") == 6.5
        assert R.bench_bonus("meituan/longcat-2.0", "coding") == 6.0
        assert R.bench_bonus("unknown/model", "general") == 0
        assert R.bench_bonus("meituan/longcat-2.0", "nope") == 0
        R._BENCH_CACHE["LongCat 2.0"]["artificial_analysis_intelligence_index"] = 99.0
        assert R.bench_bonus("meituan/longcat-2.0", "general") == 7.0
    finally:
        R._BENCH_CACHE = orig


def test_verify_chains_passes_on_limited_member():
    # 429 / transient (dead=False) must NOT fail the audit
    orig_smoke, orig_up = R.smoke_test, R.smoke_test_upstream
    R.smoke_test = lambda mid, timeout=60: (True, "x", 1)
    R.smoke_test_upstream = lambda mid, base, env, timeout=60: (False, False, 1)
    try:
        chains = {"free-general": [{"id": "a", "source": "nous",
                                     "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fast": [{"id": "d", "source": "nous",
                                  "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-coding": [{"id": "e", "source": "nous",
                                    "api_base": "https://x", "api_key": "os.environ/K"}],
                  "free-fallback": [{"id": "f", "source": "nous",
                                      "api_base": "https://x", "api_key": "os.environ/K"}]}
        assert R.verify_chains(chains) is True
    finally:
        R.smoke_test, R.smoke_test_upstream = orig_smoke, orig_up


def _one_alias_chain(mid):
    return {"free-general": [{"id": mid, "source": "nous",
                              "api_base": "https://x", "api_key": "os.environ/K"}],
            "free-fast": [{"id": "ok", "source": "nous",
                           "api_base": "https://x", "api_key": "os.environ/K"}],
            "free-coding": [{"id": "ok", "source": "nous",
                             "api_base": "https://x", "api_key": "os.environ/K"}],
            "free-fallback": [{"id": "ok", "source": "nous",
                               "api_base": "https://x", "api_key": "os.environ/K"}]}


def test_verify_chains_survives_one_flaky_dead():
    """A single structural error must NOT roll back the day (2026-09-23 did).

    First probe says DEAD, retry says OK -> audit passes."""
    calls = {"n": 0}
    orig_smoke, orig_up, orig_sleep = R.smoke_test, R.smoke_test_upstream, R.time.sleep
    R.smoke_test = lambda mid, timeout=60: (True, "x", 1)
    R.time.sleep = lambda s: None

    def flaky(mid, base, env, timeout=60):
        if mid == "flaky":
            calls["n"] += 1
            return (False, True, 1) if calls["n"] == 1 else (True, False, 1)
        return (True, False, 1)
    R.smoke_test_upstream = flaky
    try:
        assert R.verify_chains(_one_alias_chain("flaky")) is True
        assert calls["n"] == 2, "DEAD must be re-probed before it counts"
    finally:
        R.smoke_test, R.smoke_test_upstream, R.time.sleep = orig_smoke, orig_up, orig_sleep


def test_verify_chains_fails_on_repeated_dead():
    """A DEAD that survives the retry is real — the audit must still roll back."""
    calls = {"n": 0}
    orig_smoke, orig_up, orig_sleep = R.smoke_test, R.smoke_test_upstream, R.time.sleep
    R.smoke_test = lambda mid, timeout=60: (True, "x", 1)
    R.time.sleep = lambda s: None

    def always_dead(mid, base, env, timeout=60):
        if mid == "dead":
            calls["n"] += 1
            return (False, True, 1)
        return (True, False, 1)
    R.smoke_test_upstream = always_dead
    try:
        assert R.verify_chains(_one_alias_chain("dead")) is False
        assert calls["n"] == 2
    finally:
        R.smoke_test, R.smoke_test_upstream, R.time.sleep = orig_smoke, orig_up, orig_sleep


def test_verify_chains_limited_member_gets_one_retry():
    """LIMITED is retried once (rate limits are transient) and never fails."""
    calls = {"n": 0}
    orig_smoke, orig_up, orig_sleep = R.smoke_test, R.smoke_test_upstream, R.time.sleep
    R.smoke_test = lambda mid, timeout=60: (True, "x", 1)
    R.time.sleep = lambda s: None

    def limited(mid, base, env, timeout=60):
        if mid == "limited":
            calls["n"] += 1
        return (False, False, 1)
    R.smoke_test_upstream = limited
    try:
        assert R.verify_chains(_one_alias_chain("limited")) is True
        assert calls["n"] == 2
    finally:
        R.smoke_test, R.smoke_test_upstream, R.time.sleep = orig_smoke, orig_up, orig_sleep
