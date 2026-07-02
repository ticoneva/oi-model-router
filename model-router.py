"""
title: Model Router with Load Balancing
author: ticoneva, open-webui, atgehrhardt,
version: 0.6
"""

from pydantic import BaseModel, Field
from typing import Callable, Awaitable, Any, Optional, Literal
import json
import re
import datetime
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
            description="A list of model IDs for the load balancer, one per line. Format: 'model_id:weight HH:MM-HH:MM' (e.g., 'gpt-oss-120b:3 09:00-17:00'). Both weight and time range are optional. Default weight is 1 if not specified. Use weight 0 to designate a model as a backup — it will only be selected when all primary (weight > 0) models are offline. Optional time range 'HH:MM-HH:MM' (24-hour, server local time) restricts when the model is eligible; overnight ranges like '22:00-06:00' cross midnight. End time is exclusive. Models with malformed or out-of-range time windows are skipped.",
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
        status: bool = Field(
            default=False,
            description="A flag to enable or disable the status indicator. Set to True to enable status updates.",
        )
        pass

    def __init__(self):
        self.valves = self.Valves()
        pass

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
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
        if self.valves.load_balancer_models.strip():
            lines = [
                m.strip()
                for m in self.valves.load_balancer_models.strip().split("\n")
                if m.strip()
            ]
            if lines:
                # Parse model IDs, weights, and optional time ranges.
                # Format per line: "model_id:weight HH:MM-HH:MM" (weight and time range are optional)
                weighted_models = []
                total_weight = 0
                for line in lines:
                    # Split model spec from optional time range on first whitespace
                    parts = line.split(None, 1)
                    model_spec = parts[0]
                    time_range_str = parts[1].strip() if len(parts) > 1 else None

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
                        weighted_models.append((model_id, weight))
                        total_weight += weight

                if weighted_models:
                    # Split into primary (weight > 0) and backup (weight == 0) models
                    primary_models = [(mid, w) for mid, w in weighted_models if w > 0]
                    backup_models = [(mid, w) for mid, w in weighted_models if w <= 0]

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
                            (mid, w)
                            for mid, w in primary_models
                            if mid not in offline_models
                        ]
                        available_backup = [
                            (mid, w)
                            for mid, w in backup_models
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
                        selection_pool = [(mid, 1.0) for mid, _ in available_backup]
                    else:
                        selection_pool = weighted_models

                    available_weight = sum(w for _, w in selection_pool)

                    if available_weight > 0:
                        # Select model based on weights
                        r = random.uniform(0, available_weight)
                        cumulative = 0
                        selected_model = selection_pool[0][0]
                        for model_id, weight in selection_pool:
                            cumulative += weight
                            if r <= cumulative:
                                selected_model = model_id
                                break
                    else:
                        selected_model = weighted_models[0][0]

                    # Skip routing if the same model is chosen (prevents infinite loops)
                    # but still allow Chinese and vision routing to apply
                    if selected_model != __model__["id"]:
                        body["model"] = selected_model

        final_model = body["model"]

        if self.valves.status:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Load balancer routed to {final_model} ({now.strftime('%H:%M')})",
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
