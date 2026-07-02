# Model Router Filter

An Open WebUI filter that routes requests to different models based on content type and load balancing.

## Features

1. **Load Balancer**: Randomly assigns requests to models specified in `load_balancer_models` (one per line). Format: `model_id:weight HH:MM-HH:MM` (e.g., `gpt-oss-120b:3 09:00-17:00`). Both weight and time range are optional. Default weight is 1. Selection chance = weight / total_weight. Models with weight 0 are treated as backups — they are only selected when all primary (weight > 0) models are offline. Optional time range `HH:MM-HH:MM` (24-hour, server local time) restricts when the model is eligible; overnight ranges like `22:00-06:00` cross midnight. End time is exclusive. If the same model is chosen, routing is skipped to prevent loops.

2. **Vision Routing**: When enabled (`enable_vision_routing`), routes requests containing images to the vision model specified in `vision_model_id`.

3. **Chinese Routing**: When enabled (`enable_chinese_routing`), routes requests with mostly Chinese text to the Chinese model specified in `chinese_model_id`.

## Valves Configuration

- `priority`: Priority level for filter operations
- `vision_model_id`: Vision model identifier
- `chinese_model_id`: Chinese model identifier
- `load_balancer_models`: Model IDs for load balancing (one per line, format: `model_id:weight HH:MM-HH:MM`)
- `enabled_for_admins`: Enable dynamic routing for admin users
- `enabled_for_users`: Enable dynamic routing for regular users
- `timezone_str`: Timezone for time-range checks (e.g., 'Asia/Hong_Kong')
- `skip_online_check`: Skip fetching the online/offline status JSON and treat all models as online. Time ranges are still observed. (default: false)
- `status`: Enable status indicator updates
- `enable_chinese_routing`: Enable/disable Chinese model routing (default: false)
- `enable_vision_routing`: Enable/disable vision model routing (default: false)

## Routing Priority

1. Vision routing (if enabled and images found)
2. Chinese routing (if enabled and Chinese text detected)
3. Load balancer (if configured)
