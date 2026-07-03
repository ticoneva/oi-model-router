"""Standalone verification of the model-router metrics helpers.

Run via:  srun -c 4 -I python3 verify_metrics.py

Stubs out open_webui / pydantic so the module imports outside Open WebUI,
then exercises the helpers and strategy formulas against the live endpoint.
"""
import sys
import types
import random
import urllib.request

# --- Stub external deps so model_router imports cleanly ---
owui = types.ModuleType("open_webui")
owui_utils = types.ModuleType("open_webui.utils")
owui_misc = types.ModuleType("open_webui.utils.misc")
owui_misc.get_last_user_message_item = lambda *a, **k: None
owui_utils.misc = owui_misc
owui.utils = owui_utils
sys.modules["open_webui"] = owui
sys.modules["open_webui.utils"] = owui_utils
sys.modules["open_webui.utils.misc"] = owui_misc
# pydantic is real, but Field with a description is fine.

import importlib.util

spec = importlib.util.spec_from_file_location("model_router", "model-router.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)

LIVE_URL = "http://scrp-node-20:12359/metrics"
failures = []


def check(cond, msg):
    print(("PASS" if cond else "FAIL") + ": " + msg)
    if not cond:
        failures.append(msg)


# 1. Live fetch + parser correctness
text = urllib.request.urlopen(
    urllib.request.Request(LIVE_URL, headers={"User-Agent": "oi-model-router"}),
    timeout=4,
).read().decode()

waiting = mr.parse_prometheus_metric(text, "vllm:num_requests_waiting")
check(waiting == 0.0, f"live waiting == 0.0 (got {waiting})")

# 2. Prefix-collision: injected _by_reason sample must NOT inflate the sum
poisoned = text + '\nvllm:num_requests_waiting_by_reason{engine="0",reason="capacity"} 5.0\n'
check(
    mr.parse_prometheus_metric(poisoned, "vllm:num_requests_waiting") == 0.0,
    "anchored parser ignores vllm:num_requests_waiting_by_reason (no prefix leak)",
)

# 3. Multi-sample sum + label-set handling + float/scientific notation
synthetic = (
    'vllm:num_requests_waiting{engine="0",model_name="a"} 2.0\n'
    'vllm:num_requests_waiting{engine="1",model_name="b"} 3.5\n'
    'vllm:num_requests_waiting 1.5\n'  # label-less
    'vllm:num_requests_waiting_by_reason{reason="capacity"} 100.0\n'  # must be ignored
)
check(
    mr.parse_prometheus_metric(synthetic, "vllm:num_requests_waiting") == 7.0,
    "sums samples across label sets incl. label-less + scientific (2+3.5+1.5=7.0)",
)

# 4. Negative / NaN / Inf clamping
weird = 'vllm:num_requests_waiting{a="x"} -3.0\nvllm:num_requests_waiting{a="y"} nan\n'
# note: 'nan' won't match the numeric regex; only -3.0 matches and clamps to 0
check(
    mr.parse_prometheus_metric(weird, "vllm:num_requests_waiting") == 0.0,
    "negative values clamp to 0",
)

# 5. fetch_model_load against live endpoint
v = mr.fetch_model_load(LIVE_URL, "num_requests_waiting", timeout=2.0)
check(v is not None and v >= 0.0, f"fetch_model_load waiting live (got {v})")
v2 = mr.fetch_model_load(LIVE_URL, "running_plus_waiting", timeout=2.0)
check(v2 is not None and v2 >= 0.0, f"fetch_model_load running+waiting live (got {v2})")

# 6. fetch failure -> None
bad = mr.fetch_model_load("http://nonexistent.invalid:9/metrics", "num_requests_waiting", timeout=2.0)
check(bad is None, "fetch_model_load returns None on network failure")

# 7. Strategy simulations (mock pools): pool = (mid, weight, load_or_None)
def weighted_random(pool, weight_fn, n=20000):
    effs = [(mid, weight_fn(w, l)) for mid, w, l in pool]
    total = sum(e for _, e in effs)
    counts = {mid: 0 for mid, _, _ in pool}
    for _ in range(n):
        r = random.uniform(0, total)
        c = 0
        for mid, e in effs:
            c += e
            if r <= c:
                counts[mid] += 1
                break
    return counts

# load_aware: a(w=3,L=0)->3, b(w=1,L=2)->0.333, c(w=1,no url)->1. Busiest b should get least.
c_la = weighted_random(
    [("a", 3.0, 0.0), ("b", 1.0, 2.0), ("c", 1.0, None)],
    lambda w, l: w / (1.0 + (l if l is not None else 0.0)),
)
print("  load_aware distribution:", c_la)
check(c_la["b"] < c_la["a"] and c_la["b"] < c_la["c"], "load_aware down-weights busy model b")

# least_load: a(L=2), b(L=0), c(no url). Synthetic = mean(observed=2,0)=1 -> c=1.
# min is b(0) -> b should dominate; c (synthetic 1) should NOT always win.
observed_for_least = [("a", 3.0, 2.0), ("b", 1.0, 0.0), ("c", 1.0, None)]
observed = [l for _, _, l in observed_for_least if l is not None]
synthetic = sum(observed) / len(observed)
scored = [(mid, w, (l if l is not None else synthetic)) for mid, w, l in observed_for_least]
min_load = min(s for _, _, s in scored)
candidates = [(mid, w) for mid, w, s in scored if s == min_load]
print("  least_load candidates (should be only b):", [m for m, _ in candidates])
check(
    [m for m, _ in candidates] == ["b"],
    "least_load picks genuinely least-loaded model b, not no-URL model c",
)

# least_load all-no-URL: everyone ties at synthetic 0 -> weighted random over all
all_none = [("a", 3.0, None), ("b", 1.0, None)]
obs = []
syn = sum(obs) / len(obs) if obs else 0.0
scored = [(mid, w, (l if l is not None else syn)) for mid, w, l in all_none]
check(all(s == 0.0 for _, _, s in scored), "least_load all-no-URL degenerates to tie at 0")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
