# Model Router Filter

An Open WebUI filter that routes requests to different models based on content type and load balancing.

## Features

1. **Load Balancer**: Randomly assigns requests to models specified in `load_balancer_models` (one per line). Format: `model_id:weight [metrics_url] [HH:MM-HH:MM]` (e.g., `gpt-oss-120b:3 http://scrp-node-20:12359/metrics 09:00-17:00`). Fields after `model_id:weight` are whitespace-separated and order-free. Weight, the metrics URL, and the time range are all optional. Default weight is 1. Selection chance = weight / total_weight. Models with weight 0 are treated as backups — they are only selected when all primary (weight > 0) models are unavailable. Optional time range `HH:MM-HH:MM` (24-hour, server local time) restricts when the model is eligible; overnight ranges like `22:00-06:00` cross midnight. End time is exclusive. If the same model is chosen, routing is skipped to prevent loops.

   When a model line includes a `metrics_url` (a vLLM `/metrics` endpoint), the load balancer fetches live load metrics and adjusts selection according to `load_strategy`:

   - **`load_aware`** (default): effective weight = `weight / (1 + load)`, so busier models get proportionally less traffic.
   - **`busy_exclude`**: a model whose load exceeds `load_busy_threshold` is excluded (routing falls back to all models if none are idle).
   - **`least_load`**: route to the least-loaded model; static weight breaks ties. Models without a metrics URL are assigned the mean of the observed loads so they neither always win nor always lose.

   The `load_metric` valve selects the vLLM metric used as the load signal: `num_requests_waiting` (queue depth, default) or `running_plus_waiting` (running + waiting requests). Metric values are cached per URL for `metrics_cache_ttl` seconds (failed fetches are also cached, to avoid retry storms). A model whose metrics fetch fails is excluded unless every URL-bearing model fails, in which case routing degrades to static weighting. When no model has a metrics URL, all strategies reproduce today's static weighted-random selection.

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
- `load_strategy`: Load-based routing strategy when a model has a metrics URL — `load_aware` (default), `busy_exclude`, or `least_load`. With no metrics URLs configured, all strategies fall back to static weighted-random selection.
- `load_metric`: vLLM metric used as the load signal — `num_requests_waiting` (default) or `running_plus_waiting`.
- `load_busy_threshold`: Load value above which a model is treated as busy under `busy_exclude`. (default: 0.0)
- `metrics_cache_ttl`: Seconds to cache per-URL metric values, including failed fetches. (default: 5)
- `status`: Enable status indicator updates
- `enable_chinese_routing`: Enable/disable Chinese model routing (default: false)
- `enable_vision_routing`: Enable/disable vision model routing (default: false)

## Routing Priority

1. Vision routing (if enabled and images found)
2. Chinese routing (if enabled and Chinese text detected)
3. Load balancer (if configured)
