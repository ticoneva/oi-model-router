"""
title: Model Router with Load Balancing
author: ticoneva, open-webui, atgehrhardt,
version: 0.9
"""

from pydantic import BaseModel, Field
from typing import Callable, Awaitable, Any, Optional, Literal
import json
import re
import datetime
import math
import time
import concurrent.futures
from zoneinfo import ZoneInfo
import random
import urllib.request
from open_webui.utils.misc import get_last_user_message_item


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Priority level for the filter operations.",
        )
        load_balancer_models: str = Field(
            default="",
            description="A list of model IDs for the load balancer, one per line. Format: 'model_id:weight [metrics_url] [HH:MM-HH:MM]' — fields are whitespace-separated and order-free. The first token is always 'model_id:weight'; optional extra tokens are recognised by pattern: a URL (starts with 'http://' or 'https://') pointing at the model's vLLM /metrics endpoint for load-aware routing, and a time range 'HH:MM-HH:MM' (24-hour, server local time). Weight and both optional tokens may be omitted. Default weight is 1 if not specified. Use weight 0 to designate a model as a backup — it will only be selected when all primary (weight > 0) models are unavailable. Time ranges restrict when the model is eligible; overnight ranges like '22:00-06:00' cross midnight. End time is exclusive. Models with malformed or out-of-range time windows are skipped.",
        )
        enable_chinese_routing: bool = Field(
            default=False,
            description="Enable or disable Chinese model routing.",
        )
        enable_vision_routing: bool = Field(
            default=False,
            description="Enable or disable vision model routing.",
        )
        vision_model_id: str = Field(
            default="",
            description="The identifier of the vision model to be used for processing images. Note: Compatibility is provider-specific; ollama models can only route to ollama models, and OpenAI models to OpenAI models respectively.",
        )
        chinese_model_id: str = Field(
            default="",
            description="The identifier of the Chinese model to be used for processing mainly Chinese text.",
        )
        enabled_for_admins: bool = Field(
            default=True,
            description="Whether dynamic routing is enabled for admin users.",
        )
        enabled_for_users: bool = Field(
            default=True,
            description="Whether dynamic routing is enabled for regular users.",
        )
        timezone_str: str = Field(
            default="",
            description="Timezone for time-range checks (e.g., 'Asia/Hong_Kong'). Empty string uses the server's local timezone.",
        )
        skip_online_check: bool = Field(
            default=False,
            description="Skip fetching the online/offline status JSON and treat all models as online. Time ranges are still observed.",
        )
        load_strategy: Literal["load_aware", "busy_exclude", "least_load"] = Field(
            default="least_load",
            description="Load-based routing strategy applied when a model has a metrics URL. 'load_aware': scale static weight by 1/(1+load) so busier models get proportionally less traffic. 'busy_exclude': exclude models whose load exceeds load_busy_threshold (falls back to all if every model is busy). 'least_load': prefer the least-loaded model (static weight breaks ties). When no model has a metrics URL, all strategies degenerate to today's static weighted-random selection.",
        )
        load_metric: Literal["num_requests_waiting", "running_plus_waiting"] = Field(
            default="running_plus_waiting",
            description="vLLM Prometheus metric used as the load signal. 'num_requests_waiting': queue depth — the direct congestion signal. 'running_plus_waiting': running + waiting requests, capturing overall engine busyness.",
        )
        load_busy_threshold: float = Field(
            default=0.0,
            description="Used by 'busy_exclude'. A model is treated as busy (excluded) when its load value is strictly greater than this threshold. Default 0.0 excludes any model with positive load; routing then falls back to all models if none are idle.",
        )
        metrics_cache_ttl: int = Field(
            default=5,
            description="Seconds to cache per-URL metrics values. Reduces calls to vLLM /metrics endpoints. A failed fetch is also cached for this duration to avoid retry storms against a down endpoint.",
        )
        sticky_routing: bool = Field(
            default=True,
            description="When enabled, a user's requests are routed to the same load-balancer model they used within the last sticky_ttl_minutes, unless that model is offline (busy models are still used). Falls back to normal selection otherwise.",
        )
        sticky_ttl_minutes: int = Field(
            default=5,
            description="How long a user's last-routed load-balancer model is remembered for sticky routing, in minutes.",
        )
        redis_url: str = Field(
            default="redis://redis-valkey:6379/0",
            description="Redis URL for shared sticky-routing tracking. Only used when sticky_routing is enabled.",
        )
        sticky_router_id: str = Field(
            default="",
            description=(
                "Optional namespace for sticky routing. If empty, the ID of the model calling the filter "
                "(the load-balancer base model) is used automatically, so each model family — which has its "
                "own load-balancer instance — keeps an independent sticky state. Set the same ID on multiple "
                "router instances to share sticky tracking across them."
            ),
        )
        status: bool = Field(
            default=False,
            description="A flag to enable or disable the status indicator. Set to True to enable status updates.",
        )
        pass

    def __init__(self):
        self.valves = self.Valves()
        # url -> (load_value_or_None, fetch_time). None means a failed fetch.
        self._metrics_cache: dict = {}
        # Lazy-initialised Redis client for sticky routing (None = unavailable).
        self._redis: Optional[Any] = None
        pass

    def _get_redis(self) -> Optional[Any]:
        """Return a shared Redis client, or None if Redis is unavailable.

        Lazy-imported so the filter still loads when the redis package or the
        Redis server is absent; sticky routing just degrades to normal selection.
        """
        if self._redis is not None:
            return self._redis
        try:
            import redis as _redis

            self._redis = _redis.from_url(self.valves.redis_url, decode_responses=True)
            return self._redis
        except Exception:
            self._redis = None
            return None

    @staticmethod
    def _sticky_key(namespace: str, user_id: str) -> str:
        return f"oi-model-router:sticky:{namespace}:{user_id}"

    def _get_sticky_model(self, namespace: str, user_id: str) -> Optional[str]:
        """Return the user's last-routed model within the sticky window, or None."""
        r = self._get_redis()
        if r is None:
            return None
        try:
            return r.get(self._sticky_key(namespace, user_id))
        except Exception:
            return None

    def _set_sticky_model(self, namespace: str, user_id: str, model_id: str) -> None:
        """Remember the routed model for the user, refreshing the TTL."""
        r = self._get_redis()
        if r is None:
            return
        try:
            r.set(
                self._sticky_key(namespace, user_id),
                model_id,
                ex=self.valves.sticky_ttl_minutes * 60,
            )
        except Exception:
            pass

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
        __request__: Optional[Any] = None,
    ) -> dict:

        if __user__ is not None:
            if __user__.get("role") == "admin" and not self.valves.enabled_for_admins:
                return body
            elif __user__.get("role") == "user" and not self.valves.enabled_for_users:
                return body

        messages = body.get("messages", [])

        # Skip image scanning if vision routing is disabled
        if self.valves.enable_vision_routing:
            images_found = []
            image_descriptions = []

            # Initialize counters
            total_images_processed = 0
            total_words = 0
            cached_images = 0
            last_msg_id = -1

            # Extract images from user messages
            for idx_message, message in enumerate(messages):
                if message.get("role") == "user":
                    content = message.get("content", "")
                    last_msg_id = idx_message
                    # Check for images in content
                    if isinstance(content, list):
                        for idx_part, part in enumerate(content):
                            if part.get("type") == "image":
                                images_found.append(
                                    {
                                        "message_index": idx_message,
                                        "image_index_in_message": idx_part,
                                        "image": part.get("image"),
                                        "type": "image",
                                    }
                                )
                            elif part.get("type") == "image_url":
                                images_found.append(
                                    {
                                        "message_index": idx_message,
                                        "image_index_in_message": idx_part,
                                        "image_url": part.get("image_url"),
                                        "type": "image_url",
                                    }
                                )
                    if message.get("images"):
                        for idx_part, image in enumerate(message.get("images", [])):
                            images_found.append(
                                {
                                    "message_index": idx_message,
                                    "image_index_in_message": idx_part,
                                    "image": image,
                                    "type": "image",
                                }
                            )

            has_images = len(images_found) > 0

            if has_images:
                if self.valves.vision_model_id:
                    body["model"] = self.valves.vision_model_id
                    if self.valves.status:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"Request routed to {self.valves.vision_model_id}",
                                    "done": True,
                                },
                            }
                        )
                
                    return body  
                    
                elif self.valves.status:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": "No vision model ID provided, routing could not be completed.",
                                "done": True,
                            },
                        }
                    )
        else:
            has_images = False

        # Chinese routing
        if self.valves.enable_chinese_routing and not has_images:
            # Get content from last user message
            content = ""
            for message in reversed(messages):
                if message.get("role") == "user":
                    content = message.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "")
                            for p in content
                            if p.get("type") == "text"
                        )
                    break

            if isinstance(content, str) and mostly_chinese(content):
                body["model"] = self.valves.chinese_model_id
                if self.valves.status:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"Request routed to {self.valves.chinese_model_id}",
                                "done": True,
                            },
                        }
                    )
                return body          

        # Load balancer: randomly assign base model to one of the specified models with weighted selection
        tz = ZoneInfo(self.valves.timezone_str) if self.valves.timezone_str else None
        now = datetime.datetime.now(tz)
        used_load = None  # load value of the selected model, if a metrics URL was involved
        all_models_unknown = False  # every configured model ID failed the existence check
        if self.valves.load_balancer_models.strip():
            lines = [
                m.strip()
                for m in self.valves.load_balancer_models.strip().split("\n")
                if m.strip()
            ]
            if lines:
                # Parse model IDs, weights, optional metrics URLs, and optional time ranges.
                # Format per line: "model_id:weight [metrics_url] [HH:MM-HH:MM]"
                # Fields after model_spec are whitespace-separated and order-free,
                # classified by pattern: a URL (http(s)://...) and a time range (HH:MM-HH:MM).
                time_range_re = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
                weighted_models = []
                total_weight = 0
                for line in lines:
                    tokens = line.split()
                    model_spec = tokens[0]
                    metrics_url = None
                    time_range_str = None
                    for tok in tokens[1:]:
                        if tok.startswith("http://") or tok.startswith("https://"):
                            metrics_url = tok
                        elif time_range_re.match(tok):
                            time_range_str = tok
                        # Unknown tokens are ignored

                    # Parse model_id and weight from model_spec
                    if ":" in model_spec:
                        mparts = model_spec.rsplit(":", 1)
                        model_id = mparts[0].strip()
                        try:
                            weight = float(mparts[1].strip())
                        except ValueError:
                            weight = 1.0
                    else:
                        model_id = model_spec
                        weight = 1.0

                    # Parse and check time range; skip model if outside range or malformed
                    if time_range_str:
                        time_range = parse_time_range(time_range_str)
                        if time_range is None:
                            continue
                        start_min, end_min = time_range
                        if not is_within_time_range(start_min, end_min, now):
                            continue

                    if model_id:
                        weighted_models.append((model_id, weight, metrics_url))
                        total_weight += weight

                if weighted_models:
                    # Split into primary (weight > 0) and backup (weight <= 0) models
                    primary_models = [(mid, w, url) for mid, w, url in weighted_models if w > 0]
                    backup_models = [(mid, w, url) for mid, w, url in weighted_models if w <= 0]

                    # Hard-exclude model IDs that do not exist on this Open WebUI
                    # instance. __request__ exposes app.state.MODELS — the same
                    # registry the chat pipeline resolves model IDs against, where
                    # a misspecified ID fails with "Model not found". Unlike the
                    # online/offline check below, this exclusion is never fallen
                    # back on: a nonexistent ID can never become routable, not
                    # even via the all-offline or all-metrics-failed fallbacks.
                    # Fails open (no exclusion) when the registry is unavailable
                    # (no __request__, or an empty registry, e.g. at startup).
                    known_ids = None
                    if __request__ is not None:
                        try:
                            state_models = getattr(__request__.app.state, "MODELS", None)
                            if isinstance(state_models, dict) and state_models:
                                known_ids = set(state_models)
                            elif isinstance(state_models, list) and state_models:
                                known_ids = {
                                    m.get("id") for m in state_models if isinstance(m, dict)
                                }
                        except Exception:
                            known_ids = None
                    if known_ids is not None:
                        primary_models = [
                            (mid, w, url)
                            for mid, w, url in primary_models
                            if mid in known_ids
                        ]
                        backup_models = [
                            (mid, w, url)
                            for mid, w, url in backup_models
                            if mid in known_ids
                        ]

                    if primary_models or backup_models:
                        # Fetch model online/offline status from scrp-chat-status.json
                        offline_models = None
                        if not self.valves.skip_online_check:
                            try:
                                req = urllib.request.Request(
                                    "https://scrp-login.econ.cuhk.edu.hk/scrp-chat-status.json",
                                    headers={"User-Agent": "oi-model-router"},
                                )
                                with urllib.request.urlopen(req, timeout=5) as resp:
                                    status_data = json.loads(resp.read().decode())
                                    offline_models = {
                                        m["name"]
                                        for m in status_data.get("models", [])
                                        if m.get("status") == "offline"
                                    }
                            except Exception:
                                pass  # If fetch fails, treat all models as online (no filtering)

                        # Filter out offline models; if a model is not in the status JSON, assume it is online
                        if offline_models is not None:
                            available_primary = [
                                (mid, w, url)
                                for mid, w, url in primary_models
                                if mid not in offline_models
                            ]
                            available_backup = [
                                (mid, w, url)
                                for mid, w, url in backup_models
                                if mid not in offline_models
                            ]
                            # If all models are offline, fall back to the full list
                            if not available_primary and not available_backup:
                                available_primary = primary_models
                                available_backup = backup_models
                        else:
                            available_primary = primary_models
                            available_backup = backup_models

                        # Use primary models if any are available; otherwise fall back to backup models
                        if available_primary:
                            selection_pool = available_primary
                        elif available_backup:
                            # Backup models get equal weight since they were all weight 0
                            selection_pool = [(mid, 1.0, url) for mid, _, url in available_backup]
                        else:
                            selection_pool = weighted_models

                        # Sticky routing: if this user was routed to one of the
                        # currently-eligible models within the sticky window, keep
                        # them on it unless it is actually offline. Busy models are
                        # deliberately NOT disqualified here — only true offline
                        # status (from the status JSON) breaks stickiness. This
                        # preserves the affinity even under load.
                        #
                        # The sticky state is namespaced: by default it uses the ID
                        # of the model calling the filter (the load-balancer base
                        # model), so each model family — which runs its own
                        # load-balancer instance — keeps an independent affinity.
                        # sticky_router_id lets an admin override this to share
                        # sticky tracking across instances.
                        calling_model_id = __model__["id"] if __model__ is not None else "default_model"
                        sticky_namespace = self.valves.sticky_router_id.strip() or calling_model_id
                        pool_ids = {mid for mid, _, _ in selection_pool}
                        used_sticky = False
                        if self.valves.sticky_routing and __user__ is not None:
                            sticky_model = self._get_sticky_model(
                                sticky_namespace, __user__["id"]
                            )
                            if (
                                sticky_model
                                and sticky_model in pool_ids
                                and (
                                    offline_models is None
                                    or sticky_model not in offline_models
                                )
                            ):
                                selected_model = sticky_model
                                used_sticky = True
                                # Refresh the sticky window so continued use keeps affinity.
                                self._set_sticky_model(
                                    sticky_namespace, __user__["id"], sticky_model
                                )

                        if not used_sticky:
                            # Resolve live load for any pool entry with a metrics URL.
                            # Use a short-lived cache keyed by URL to avoid hammering /metrics
                            # on every request; refresh stale/missing entries concurrently.
                            ttl = self.valves.metrics_cache_ttl
                            now_ts = time.monotonic()
                            stale_urls = set()
                            for _, _, url in selection_pool:
                                if url and url not in self._metrics_cache:
                                    stale_urls.add(url)
                                elif url:
                                    cached_val, cached_ts = self._metrics_cache[url]
                                    if cached_val is None or (now_ts - cached_ts) >= ttl:
                                        stale_urls.add(url)
                            if stale_urls:
                                urls = list(stale_urls)
                                with concurrent.futures.ThreadPoolExecutor(
                                    max_workers=min(len(urls), 8)
                                ) as ex:
                                    results = list(
                                        zip(
                                            urls,
                                            ex.map(
                                                lambda u: fetch_model_load(
                                                    u, self.valves.load_metric, timeout=2.0
                                                ),
                                                urls,
                                            ),
                                        )
                                    )
                                for u, val in results:
                                    self._metrics_cache[u] = (val, now_ts)

                            # Attach a resolved load to each pool entry: None when there is no
                            # signal (no URL), None when the fetch failed, else a float >= 0.
                            pool_with_load = []
                            for mid, w, url in selection_pool:
                                if url:
                                    cached_val, _ = self._metrics_cache.get(url, (None, now_ts))
                                    pool_with_load.append((mid, w, cached_val))
                                else:
                                    pool_with_load.append((mid, w, None))

                            # Gate 2: exclude models whose metrics fetch failed (a reachable model
                            # with load 0 is kept; only a failed fetch excludes). Models without a
                            # URL keep load None (no signal -> fail open, never excluded). If every
                            # URL-bearing model failed, fall back to the whole pool with load 0 so
                            # routing degrades to static weighting rather than failing outright.
                            url_by_mid = {mid: url for mid, _, url in selection_pool}
                            live_pool = [
                                (mid, w, load)
                                for mid, w, load in pool_with_load
                                if not (url_by_mid.get(mid) is not None and load is None)
                            ]
                            if not live_pool:
                                # Every URL-bearing model failed to fetch: keep the pool, load 0.
                                live_pool = [(mid, w, 0.0) for mid, w, _ in pool_with_load]

                            strategy = self.valves.load_strategy
                            threshold = self.valves.load_busy_threshold

                            # Build the (model_id, effective_weight) pairs to select from.
                            # live_pool loads are None for no-URL models (no signal) or a float.
                            if strategy == "load_aware":
                                # effective_weight = W / (1 + load); no signal (None) treated as 0 -> W.
                                eff_pool = [
                                    (mid, w / (1.0 + (load if load is not None else 0.0)))
                                    for mid, w, load in live_pool
                                ]
                            elif strategy == "busy_exclude":
                                survivors = [
                                    (mid, w, load)
                                    for mid, w, load in live_pool
                                    if (load if load is not None else 0.0) <= threshold
                                ]
                                if not survivors:
                                    survivors = live_pool
                                eff_pool = [(mid, w) for mid, w, _ in survivors]
                            else:  # least_load
                                # Prefer the least-loaded model; static weight breaks ties.
                                # Models without a URL (no real load signal) are given the mean
                                # of the observed loads so they neither always win nor always lose.
                                observed = [load for _, _, load in live_pool if load is not None]
                                synthetic = (sum(observed) / len(observed)) if observed else 0.0
                                scored = [
                                    (mid, w, (load if load is not None else synthetic))
                                    for mid, w, load in live_pool
                                ]
                                min_load = min(s for _, _, s in scored)
                                candidates = [(mid, w) for mid, w, s in scored if s == min_load]
                                eff_pool = candidates

                            available_weight = sum(w for _, w in eff_pool)

                            if available_weight > 0:
                                # Select model based on (effective) weights
                                r = random.uniform(0, available_weight)
                                cumulative = 0
                                selected_model = eff_pool[0][0]
                                for model_id, weight in eff_pool:
                                    cumulative += weight
                                    if r <= cumulative:
                                        selected_model = model_id
                                        break
                            else:
                                # Defensive path (unreachable in practice): pick from
                                # the pool, never from weighted_models, so an unknown
                                # ID excluded above cannot be resurrected here.
                                selected_model = selection_pool[0][0]

                            # Record the freshly-selected model as the user's sticky
                            # target so subsequent requests within the window stay on it.
                            if self.valves.sticky_routing and __user__ is not None:
                                self._set_sticky_model(
                                    sticky_namespace, __user__["id"], selected_model
                                )

                        # Skip routing if the same model is chosen (prevents infinite loops)
                        # but still allow Chinese and vision routing to apply
                        if selected_model != __model__["id"]:
                            body["model"] = selected_model

                        # Track whether load data influenced this decision, for the status line.
                        # Report only when the selected model actually had a metrics URL; the
                        # variables below are only populated on the non-sticky path.
                        if not used_sticky and url_by_mid.get(selected_model) is not None:
                            load_by_mid = {mid: load for mid, _, load in live_pool}
                            used_load = load_by_mid.get(selected_model)
                    else:
                        # Every configured model ID failed the existence check
                        # above. Routing to any of them would fail downstream
                        # with "Model not found", so leave the request on the
                        # base model rather than fall back on unknown IDs.
                        all_models_unknown = True

        final_model = body["model"]

        if self.valves.status:
            if all_models_unknown:
                description = (
                    "Load balancer skipped routing: no configured model IDs exist "
                    f"on this instance ({now.strftime('%H:%M')})"
                )
            elif used_load is not None:
                description = (
                    f"Load balancer routed to {final_model} "
                    f"(load={used_load}, strategy={self.valves.load_strategy}, {now.strftime('%H:%M')})"
                )
            else:
                description = f"Load balancer routed to {final_model} ({now.strftime('%H:%M')})"
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": True,
                    },
                }
            )

        return body


def parse_time_range(s: str):
    """Parse 'HH:MM-HH:MM' into (start_min, end_min) minutes-of-day, or None if invalid.

    Rejects 'start == end' (e.g., '09:00-09:00') as ambiguous — likely a user error.
    """
    m = re.match(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$", s.strip())
    if not m:
        return None
    sh, sm, eh, em = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return None
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    if start_min == end_min:
        return None
    return (start_min, end_min)


def is_within_time_range(start_min: int, end_min: int, now: datetime.datetime) -> bool:
    """Check if now is within [start, end) (exclusive end).

    Supports overnight ranges where start > end (e.g., 22:00-06:00 means
    22:00 to 06:00 the next day).
    """
    now_min = now.hour * 60 + now.minute
    if start_min < end_min:
        return start_min <= now_min < end_min
    # Overnight: start at 22:00, end at 06:00 -> available from 22:00 to 23:59 or 00:00 to 05:59
    return now_min >= start_min or now_min < end_min


def parse_prometheus_metric(text: str, metric_name: str) -> float:
    """Sum all sample values for metric_name across all label sets.

    Anchors the metric name so that 'vllm:num_requests_waiting' does NOT match
    'vllm:num_requests_waiting_by_reason' (which would double-count the queue).
    A metric is matched only when immediately followed by '{...}' (labels) or
    whitespace, then a number. Returns 0.0 if no samples match. Each value is
    clamped to a minimum of 0; NaN/Inf values are treated as 0.
    """
    pattern = re.compile(
        r"^" + re.escape(metric_name) + r"(?:\{[^}]*\})?\s+"
        r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$",
        re.MULTILINE,
    )
    total = 0.0
    for raw in pattern.findall(text):
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isnan(value) or math.isinf(value):
            value = 0.0
        total += max(0.0, value)
    return total


def fetch_model_load(url: str, load_metric: str, timeout: float = 2.0) -> Optional[float]:
    """Fetch url and return the load value per load_metric, or None on any error.

    'num_requests_waiting' sums vllm:num_requests_waiting.
    'running_plus_waiting' sums vllm:num_requests_running plus vllm:num_requests_waiting.

    Returns None on network/timeout/HTTP/parse failure. A live endpoint that
    returns no matching metric samples yields 0.0 (reachable and idle), not None.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "oi-model-router"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()

        waiting = parse_prometheus_metric(text, "vllm:num_requests_waiting")
        if load_metric == "running_plus_waiting":
            running = parse_prometheus_metric(text, "vllm:num_requests_running")
            return running + waiting
        return waiting
    except Exception:
        return None


def mostly_chinese(text: str) -> bool:
    """
    Return True if the string contains more Chinese characters than English words.

    This modified function compares across different units:
    - Chinese text is measured by the count of ideographic characters.
    - English text is approximated by the count of "words" (sequences of
      basic Latin letters a-z, A-Z).

    An empty string, or a string with a non-Chinese character
    count not exceeding the English word count, returns False.
    """
    # 1. Count the number of Chinese characters.
    chinese_char_count = sum(1 for ch in text if is_chinese_char(ch))

    # 2. Count the number of "English words" using a regular expression.
    # This regex finds all continuous sequences of one or more Latin letters.
    english_words = re.findall(r"[a-zA-Z]+", text)
    english_word_count = len(english_words)

    # 3. Compare the two counts.
    return chinese_char_count > english_word_count


def is_chinese_char(ch: str) -> bool:
    """
    Return True if a single Unicode character belongs to one of the
    Chinese‑ideographic blocks.

    Recognised blocks (as of Unicode 15.0):
        • CJK Unified Ideographs                (U+4E00–U+9FFF)
        • CJK Unified Ideographs Extension A    (U+3400–U+4DBF)
        • CJK Unified Ideographs Extension B–F  (U+20000–U+2EBEF)
        • CJK Compatibility Ideographs          (U+F900–U+FAFF)
    """
    cp = ord(ch)

    # Basic block
    if 0x4E00 <= cp <= 0x9FFF:
        return True
    # Extension A
    if 0x3400 <= cp <= 0x4DBF:
        return True
    # Extension B
    if 0x20000 <= cp <= 0x2A6DF:
        return True
    # Extension C
    if 0x2A700 <= cp <= 0x2B73F:
        return True
    # Extension D
    if 0x2B740 <= cp <= 0x2B81F:
        return True
    # Extension E
    if 0x2B820 <= cp <= 0x2CEAF:
        return True
    # Extension F
    if 0x2CEB0 <= cp <= 0x2EBEF:
        return True
    # Compatibility Ideographs
    if 0xF900 <= cp <= 0xFAFF:
        return True

    return False
