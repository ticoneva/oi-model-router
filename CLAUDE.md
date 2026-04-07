# Model Router Filter

An Open WebUI filter that routes requests to different models based on content type and load balancing.

## Features

1. **Load Balancer**: Randomly assigns requests to models specified in `load_balancer_models` (one per line). Format: `model_id:weight` (e.g., `gpt-oss-120b:3`). Default weight is 1. Selection chance = weight / total_weight. If the same model is chosen, routing is skipped to prevent loops.

2. **Vision Routing**: When enabled (`enable_vision_routing`), routes requests containing images to the vision model specified in `vision_model_id`.

3. **Chinese Routing**: When enabled (`enable_chinese_routing`), routes requests with mostly Chinese text to the Chinese model specified in `chinese_model_id`.

## Valves Configuration

- `priority`: Priority level for filter operations
- `vision_model_id`: Vision model identifier
- `chinese_model_id`: Chinese model identifier
- `skip_reroute_models`: Models that should not be re-routed
- `enabled_for_admins`: Enable dynamic routing for admin users
- `enabled_for_users`: Enable dynamic routing for regular users
- `status`: Enable status indicator updates
- `load_balancer_models`: Model IDs for load balancing (one per line)
- `enable_chinese_routing`: Enable/disable Chinese model routing (default: false)
- `enable_vision_routing`: Enable/disable vision model routing (default: false)

## Routing Priority

1. Load balancer (if configured)
2. Vision routing (if enabled and images found)
3. Chinese routing (if enabled and Chinese text detected)
