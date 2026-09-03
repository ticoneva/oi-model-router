"""Standalone verification of the model-router existence check and routing gates.

Run via:  srun -c 4 -I python3 verify_routing.py

Stubs out open_webui / pydantic so the module imports outside Open WebUI,
then drives inlet() end-to-end with a fake __request__ (carrying
app.state.MODELS), fake metrics endpoints, and a fake status JSON.
"""
import sys
import types
import asyncio
import urllib.request
from types import SimpleNamespace

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

import importlib.util

spec = importlib.util.spec_from_file_location("model_router", "model-router.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)

failures = []


def check(cond, msg):
    print(("PASS" if cond else "FAIL") + ": " + msg)
    if not cond:
        failures.append(msg)


STATUS_URL = "https://scrp-login.econ.cuhk.edu.hk/scrp-chat-status.json"
M_GOOD = "http://fake-good:1/metrics"      # live, load 5.0
M_TYPO = "http://fake-typo:1/metrics"      # live, load 0.0 (idle but unknown ID)
M_IDLE = "http://fake-idle:1/metrics"      # live, load 0.0
M_DEAD = "http://fake-dead:1/metrics"      # unreachable

METRICS = {
    M_GOOD: 'vllm:num_requests_running 5.0\nvllm:num_requests_waiting 0.0\n',
    M_TYPO: 'vllm:num_requests_running 0.0\nvllm:num_requests_waiting 0.0\n',
    M_IDLE: 'vllm:num_requests_running 0.0\nvllm:num_requests_waiting 0.0\n',
}

# status JSON: only "offline-model" is marked offline
STATUS_JSON = '{"models": [{"name": "offline-model", "status": "offline"}]}'

# {url: body} plus the status URL; anything else raises (unreachable)
ROUTES = dict(METRICS)
ROUTES[STATUS_URL] = STATUS_JSON


class FakeResp:
    def __init__(self, data):
        self._data = data.encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url
    if url in ROUTES:
        return FakeResp(ROUTES[url])
    raise OSError(f"unreachable: {url}")


urllib.request.urlopen = fake_urlopen


class FakeRequest:
    def __init__(self, models):
        if models is not None:
            self.app = SimpleNamespace(state=SimpleNamespace(MODELS=models))
        else:
            self.app = SimpleNamespace(state=SimpleNamespace())  # no MODELS attr


BASE = "base-model"
KNOWN = {"base-model": {}, "good": {}, "other": {}, "offline-model": {}}


def make_filter(models_cfg, *, status=False, skip_online_check=True, request="known"):
    f = mr.Filter()
    f.valves = mr.Filter.Valves(
        load_balancer_models=models_cfg,
        status=status,
        skip_online_check=skip_online_check,
        sticky_routing=False,  # no Redis in this harness
    )
    if request == "known":
        req = FakeRequest(dict(KNOWN))
    elif request is None:
        req = None
    elif request == "empty":
        req = FakeRequest({})
    elif request == "missing":
        req = FakeRequest(None)
    return f, req


async def run_inlet(f, req, n=1):
    """Run inlet n times; return list of resulting body['model'] values."""
    out = []
    for _ in range(n):
        events = []

        async def emitter(event):
            events.append(event)

        body = {"model": BASE, "messages": [{"role": "user", "content": "hello"}]}
        result = await f.inlet(
            body,
            emitter,
            __model__={"id": BASE},
            __user__={"id": "u1", "role": "user"},
            __request__=req,
        )
        out.append(result["model"])
    return out


async def main():
    # 1. The original bug: typo ID has a LIVE, idle metrics endpoint but does not
    #    exist in the registry; good is known and busy. least_load must never
    #    pick the (idle) unknown model.
    f, req = make_filter(f"good:1 {M_GOOD}\ntypo-id:1 {M_TYPO}")
    got = await run_inlet(f, req, n=25)
    check(set(got) == {"good"}, f"unknown ID with live metrics excluded (got {set(got)})")

    # 2. All configured IDs unknown -> routing skipped, base model kept
    f, req = make_filter("typo-a:1\ntypo-b:1", status=True)
    got = await run_inlet(f, req, n=5)
    check(set(got) == {BASE}, f"all-unknown skips routing, stays on base (got {set(got)})")

    events = []

    async def emitter(event):
        events.append(event)

    body = {"model": BASE, "messages": [{"role": "user", "content": "hello"}]}
    await f.inlet(
        body, emitter, __model__={"id": BASE}, __user__={"id": "u1", "role": "user"},
        __request__=req,
    )
    desc = events[0]["data"]["description"] if events else ""
    check("skipped routing" in desc, f"all-unknown status says routing skipped (got {desc!r})")

    # 3. No __request__ at all -> fail open (old behavior: typo routable)
    f, req = make_filter(f"typo-id:1 {M_TYPO}")
    got = await run_inlet(f, None)
    check(got == ["typo-id"], f"no __request__ fails open (got {got})")

    # 4. Empty registry -> fail open
    f, req = make_filter(f"typo-id:1 {M_TYPO}", request="empty")
    got = await run_inlet(f, req)
    check(got == ["typo-id"], f"empty registry fails open (got {got})")

    # 5. Registry attribute missing entirely -> fail open
    f, req = make_filter(f"typo-id:1 {M_TYPO}", request="missing")
    got = await run_inlet(f, req)
    check(got == ["typo-id"], f"missing MODELS attr fails open (got {got})")

    # 6. All-offline fallback must not resurrect an unknown ID: the only KNOWN
    #    model is marked offline in the status JSON; the typo ID is unknown.
    #    Fail-open happens within the known set only -> offline-model, never typo.
    f, req = make_filter("typo-id:1\noffline-model:1", skip_online_check=False)
    got = await run_inlet(f, req, n=10)
    check(set(got) == {"offline-model"}, f"all-offline fallback stays within known IDs (got {set(got)})")

    # 7. Metrics-fetch failure still excludes a KNOWN model (existing Gate 2)
    f, req = make_filter(f"good:1 {M_GOOD}\nother:1 {M_DEAD}")
    got = await run_inlet(f, req, n=10)
    check(set(got) == {"good"}, f"dead metrics endpoint excludes known model (got {set(got)})")

    # 8. Unknown primary, known backup -> backup pool used, never the unknown
    f, req = make_filter("typo-id:1\ngood:0")
    got = await run_inlet(f, req, n=10)
    check(set(got) == {"good"}, f"unknown primary falls to known backup (got {set(got)})")

    # 9. Regression: all known, no metrics -> weighted routing still works
    f, req = make_filter("good:1\nother:1")
    got = await run_inlet(f, req, n=50)
    check(set(got) == {"good", "other"}, f"known models still routable (got {set(got)})")


asyncio.run(main())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
