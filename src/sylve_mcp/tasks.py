"""Bounded asynchronous task and event polling for Sylve operations.

Unlike the CLI's interactive progress spinner, pollers here run headless and
never block the MCP transport for more than the caller's ``timeout``: on
timeout they return the last in-progress record flagged ``completed=false`` so
the model can decide to keep waiting via ``sylve_task``.
"""

from __future__ import annotations

import time
from typing import Any

from sylve_mcp.client import SylveClient, SylveError

TERMINAL_TASK_STATUSES = {
    "success",
    "completed",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "aborted",
    "interrupted",
}
FAILURE_TASK_STATUSES = {"failed", "error", "cancelled", "canceled", "aborted", "interrupted"}


def _finish(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out["completed"] = True
    return out


def _timeout(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out["completed"] = False
    out["timedOut"] = True
    return out


def get_lifecycle_task(
    client: SylveClient,
    task_id: int,
    node: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a lifecycle task by ID with fallback to the active/recent lists."""
    data = client.get(f"/tasks/lifecycle/{task_id}", node=node)
    if isinstance(data, dict) and data.get("id") is not None:
        return data

    for path, params in (
        ("/tasks/lifecycle/active", None),
        ("/tasks/lifecycle/recent", {"limit": 100}),
    ):
        data = client.get(path, node=node, params=params)
        if isinstance(data, list):
            for task in data:
                if isinstance(task, dict) and str(task.get("id")) == str(task_id):
                    return task
    return None


def wait_for_lifecycle_task(
    client: SylveClient,
    task_id: int,
    node: str | None = None,
    timeout: float = 60.0,
    poll_interval: float = 1.5,
) -> dict[str, Any]:
    """Poll a lifecycle task until terminal, raising on failure. Bounded by *timeout*."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_task: dict[str, Any] = {"id": task_id, "status": "queued"}
    while time.monotonic() < deadline:
        data = get_lifecycle_task(client, task_id, node=node)
        if data is None:
            raise SylveError(f"Lifecycle task #{task_id} not found.")
        last_task = data
        status = str(data.get("status") or "").strip().lower()
        if status in TERMINAL_TASK_STATUSES:
            if status in FAILURE_TASK_STATUSES:
                err_detail = data.get("error") or data.get("message") or status
                raise SylveError(f"Task #{task_id} {status}: {err_detail}")
            return _finish(data)
        time.sleep(poll_interval)

    last_task["message"] = last_task.get("message") or (
        f"still {last_task.get('status', 'queued')} after {int(timeout)}s"
    )
    return _timeout(last_task)


def wait_for_replication_event(
    client: SylveClient,
    event_id: int,
    node_id: str | None = None,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Poll a replication event until terminal. Bounded by *timeout*."""
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] = {"id": event_id, "status": "queued"}
    while time.monotonic() < deadline:
        params = {"nodeId": node_id} if node_id else None
        data = client.get(f"/cluster/replication/events/{event_id}/progress", params=params)
        if isinstance(data, dict):
            event_value = data.get("event")
            ev = event_value if isinstance(event_value, dict) else data
            status = str(ev.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                if status in FAILURE_TASK_STATUSES:
                    err_detail = ev.get("error") or ev.get("message") or status
                    raise SylveError(f"Replication event #{event_id} {status}: {err_detail}")
                return _finish(ev)
            last = ev
        time.sleep(poll_interval)

    return _timeout(last)


def wait_for_backup_event(
    client: SylveClient,
    event_id: int,
    node_id: str | None = None,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Poll a backup event until terminal. Bounded by *timeout*."""
    deadline = time.monotonic() + max(0.0, timeout)
    last: dict[str, Any] = {"id": event_id, "status": "queued"}
    while time.monotonic() < deadline:
        params = {"nodeId": node_id} if node_id else None
        data = client.get(f"/cluster/backups/events/{event_id}/progress", params=params)
        if isinstance(data, dict):
            event_value = data.get("event")
            ev = event_value if isinstance(event_value, dict) else data
            status = str(ev.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                if status in FAILURE_TASK_STATUSES:
                    err_detail = ev.get("error") or status
                    raise SylveError(f"Backup event #{event_id} {status}: {err_detail}")
                return _finish(ev)
            last = ev
        time.sleep(poll_interval)

    return _timeout(last)


def wait_for_result_event(
    client: SylveClient,
    kind: str,
    event_id: int,
    node_id: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Route an event wait to the replication or backup poller."""
    if kind == "replication":
        return wait_for_replication_event(client, event_id, node_id=node_id, timeout=timeout)
    if kind == "backup":
        return wait_for_backup_event(client, event_id, node_id=node_id, timeout=timeout)
    raise SylveError(f"Unknown event kind '{kind}'.")
