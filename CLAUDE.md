# Model Router Filter

An Open WebUI filter that routes requests to different models based on content type and load balancing.

## Features

1. **Load Balancer**: Randomly assigns requests to models specified in `load_balancer_models` (one per line). Format: `model_id:weight [metrics_url] [HH:MM-HH:MM]` (e.g., `gpt-oss-120b:3 http://scrp-node-20:12359/metrics 09:00-17:00`). Fields after `model_id:weight` are whitespace-separated and order-free. Weight, the metrics URL, and the time range are all optional. Default weight is 1. Selection chance = weight / total_weight. Models with weight 0 are treated as backups — they are only selected when all primary (weight > 0) models are unavailable. Optional time range `HH:MM-HH:MM` (24-hour, server local time) restricts when the model is eligible; overnight ranges like `22:00-06:00` cross midnight. End time is exclusive. If the same model is chosen, routing is skipped to prevent loops.

   When a model line includes a `metrics_url` (a vLLM `/metrics` endpoint), the load balancer fetches live load metrics and adjusts selection according to `load_strategy`:

   - **`load_aware`**: effective weight = `weight / (1 + load)`, so busier models get proportionally less traffic.
   - **`busy_exclude`**: a model whose load exceeds `load_busy_threshold` is excluded (routing falls back to all models if none are idle).
   - **`least_load`** (default): route to the least-loaded model; static weight breaks ties. Models without a metrics URL are assigned the mean of the observed loads so they neither always win nor always lose.

   The `load_metric` valve selects the vLLM metric used as the load signal: `num_requests_waiting` (queue depth) or `running_plus_waiting` (running + waiting requests, default). Metric values are cached per URL for `metrics_cache_ttl` seconds (failed fetches are also cached, to avoid retry storms). A model whose metrics fetch fails is excluded unless every URL-bearing model fails, in which case routing degrades to static weighting. When no model has a metrics URL, all strategies reproduce today's static weighted-random selection.

   **Sticky routing** (`sticky_routing`, default on): once the load balancer routes a user to a model, that model is remembered in Redis for `sticky_ttl_minutes` (default 5) and the user's subsequent requests stay on it, skipping load-based selection. A sticky target is reused only while it is still eligible (in the active time range and primary/backup pool) and not actually offline (per the status JSON). Busy models are deliberately kept — only true offline status breaks affinity, so a node under load still keeps its user. When the sticky target is unavailable or expired, routing falls back to normal selection and the new choice is recorded. Affinity is per user (not per session) and shared across Open WebUI instances via the `redis_url` store. Sticky state is namespaced: by default it uses the ID of the model calling the filter (the load-balancer base model), so each model family — which runs its own load-balancer copy — keeps an independent affinity; set `sticky_router_id` to share sticky tracking across instances.

   **Model existence check**: model IDs in `load_balancer_models` are validated against Open WebUI's live model registry (`app.state.MODELS`, accessed via the `__request__` object Open WebUI passes to the filter — the same registry the chat pipeline resolves IDs against, so a misspecified ID would fail downstream with "Model not found"). An ID that does not exist on the instance is hard-excluded before selection, so a live metrics endpoint no longer keeps a misspecified model routable. Unlike the status-JSON offline check, this exclusion is never fallen back on — not by the all-offline fallback, the all-metrics-failed fallback, or the backup pool — since a nonexistent model can never become available. If every configured ID is unknown, load-balancer routing is skipped entirely and the request stays on the base model (surfaced in the status indicator when enabled). The check fails open (no exclusion) when the registry is unavailable: no `__request__` (older Open WebUI), or an empty registry (e.g. at startup). A live metrics endpoint that merely lacks the expected vLLM metrics still counts as idle (load 0), not offline; only connection/HTTP/parse failures exclude.

2. **Vision Routing**: When enabled (`enable_vision_routing`), routes requests containing images to the vision model specified in `vision_model_id`.

3. **Chinese Routing**: When enabled (`enable_chinese_routing`), routes requests with mostly Chinese text to the Chinese model specified in `chinese_model_id`.

## Valves Configuration

- `priority`: Priority level for filter operations
- `vision_model_id`: Vision model identifier
- `chinese_model_id`: Chinese model identifier
- `load_balancer_models`: Model IDs for load balancing (one per line, format: `model_id:weight [metrics_url] [HH:MM-HH:MM]`)
- `enabled_for_admins`: Enable dynamic routing for admin users
- `enabled_for_users`: Enable dynamic routing for regular users
- `timezone_str`: Timezone for time-range checks (e.g., 'Asia/Hong_Kong')
- `skip_online_check`: Skip fetching the online/offline status JSON and treat all models as online. Time ranges are still observed. (default: false)
- `load_strategy`: Load-based routing strategy when a model has a metrics URL — `load_aware`, `busy_exclude`, or `least_load` (default). With no metrics URLs configured, all strategies fall back to static weighted-random selection.
- `load_metric`: vLLM metric used as the load signal — `num_requests_waiting` or `running_plus_waiting` (default).
- `load_busy_threshold`: Load value above which a model is treated as busy under `busy_exclude`. (default: 0.0)
- `metrics_cache_ttl`: Seconds to cache per-URL metric values, including failed fetches. (default: 5)
- `sticky_routing`: Keep a user on the same load-balancer model they used within the last `sticky_ttl_minutes`, unless that model is actually offline. Busy models are still used. (default: true)
- `sticky_ttl_minutes`: How long a user's last-routed load-balancer model is remembered for sticky routing. (default: 5)
- `redis_url`: Redis URL for shared sticky-routing tracking. Only used when `sticky_routing` is enabled. (default: redis://redis-valkey:6379/0)
- `sticky_router_id`: Optional namespace for sticky routing. If empty, the ID of the model calling the filter is used automatically, so each model family (which has its own load-balancer instance) keeps independent sticky state. Set the same ID on multiple router instances to share sticky tracking.
- `status`: Enable status indicator updates
- `enable_chinese_routing`: Enable/disable Chinese model routing (default: false)
- `enable_vision_routing`: Enable/disable vision model routing (default: false)

## Routing Priority

1. Vision routing (if enabled and images found)
2. Chinese routing (if enabled and Chinese text detected)
3. Load balancer (if configured)
