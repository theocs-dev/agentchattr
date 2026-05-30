"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data"):
        self._registry = registry
        self._data_dir = Path(data_dir)

    def is_available(self, name: str) -> bool:
        return self._registry.is_registered(name)

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, get_role
        instances = self._registry.get_all()
        return {
            name: {
                "registered": True,
                "available": is_online(name),
                "busy": is_active(name),
                "label": info["label"],
                "color": info["color"],
                "transport": info.get("transport", "unknown"),
                "terminal_injectable": bool(info.get("terminal_injectable", False)),
                "clear_supported": bool(info.get("clear_supported", False)),
                "clear_strategy": info.get("clear_strategy", "unsupported"),
                "clear_confirmation": info.get("clear_confirmation", "none"),
                "clear_state": info.get("clear_state", {}),
                "role": get_role(name),
            }
            for name, info in instances.items()
        }

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Write to the agent's queue file. The worker terminal picks it up."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Synchronous version of trigger — writes to queue file without async."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

    def queue_clear_context_sync(
        self,
        agent_name: str,
        *,
        channel: str = "general",
        requested_by: str = "user",
    ) -> dict:
        """Queue a terminal action for the wrapper without using prompt injection."""
        action_file = self._data_dir / f"{agent_name}_actions.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        action = {
            "type": "clear_context",
            "strategy": "session_restart",
            "request_id": uuid.uuid4().hex,
            "requested_at": int(time.time()),
            "requested_by": requested_by,
            "channel": channel,
            "confirmed_by_user": True,
        }
        with open(action_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(action) + "\n")

        log.info("Queued clear_context action for @%s (ch=%s)", agent_name, channel)
        return action
