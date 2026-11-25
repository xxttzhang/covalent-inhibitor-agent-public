"""Workspace enumeration and scheduling-capability queries via browser API."""

from __future__ import annotations

import re
from typing import Any

from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID, WebSession

from .core import _get_base_url, _request_json, _v2_result

_WS_ID_RE = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class WorkspaceCapabilityError(RuntimeError):
    """Raised when a write path cannot resolve workspace scheduling policy."""


class WorkspaceEnumerationError(RuntimeError):
    """Raised when the live workspace enumeration request fails."""


def _workspace_route_entries(
    session: WebSession,
    *,
    base_url: str | None,
    workspace_id: str | None,
) -> dict[str, dict[str, Any]]:
    resolved_base_url = (base_url or "").strip() or _get_base_url()
    referer = f"{resolved_base_url}/jobs/distributedTraining"

    # The new authenticated enumeration endpoint takes no workspace selector.
    # It must also work during bootstrap, before a workspace is known.
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/user?Action=ListAccessibleWorkspaces",
            referer=referer,
            body={},
            timeout=15,
        )
    )
    return _accessible_workspace_entries(payload)


def _accessible_workspace_entries(payload: Any) -> dict[str, dict[str, Any]]:
    """Read the current console contract; unknown policy never becomes False."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise WorkspaceCapabilityError("Missing accessible workspace list.")
    results: dict[str, dict[str, Any]] = {}
    for entry in payload["items"]:
        if not isinstance(entry, dict):
            raise WorkspaceCapabilityError("Invalid accessible workspace entry.")
        ws_id, ws_name, fair = (entry.get(key) for key in
                                ("workspace_id", "name", "is_fair_workspace"))
        if (not isinstance(ws_id, str) or not _WS_ID_RE.fullmatch(ws_id)
                or ws_id == DEFAULT_WORKSPACE_ID or ws_id in results
                or not isinstance(ws_name, str) or not ws_name.strip()
                or type(fair) is not bool):
            raise WorkspaceCapabilityError("Invalid accessible workspace identity or policy.")
        results[ws_id] = {"id": ws_id, "name": ws_name, "is_fair_workspace": fair}
    return results


def _cache_fair_scheduling(session: WebSession, results: dict[str, dict[str, Any]]) -> None:
    if not results:
        return
    cached = dict(getattr(session, "all_workspace_fair_scheduling", None) or {})
    cached.update({ws_id: bool(item["is_fair_workspace"]) for ws_id, item in results.items()})
    session.all_workspace_fair_scheduling = cached


def try_enumerate_workspaces(
    session: WebSession,
    base_url: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Try to enumerate workspaces via API endpoints.

    Uses the current console's ``user.ListAccessibleWorkspaces`` contract:
    ``items`` with workspace_id, name and explicit boolean is_fair_workspace.

    Returns workspace id, name, and fair-scheduling capability dictionaries.
    Returns an empty list only when the live API successfully reports no
    accessible workspaces. Request and API failures are raised so callers can
    preserve their last successful snapshot.
    """
    try:
        results = _workspace_route_entries(
            session,
            base_url=base_url,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        raise WorkspaceEnumerationError(
            "Could not enumerate accessible workspaces."
        ) from exc

    _cache_fair_scheduling(session, results)
    return list(results.values())


def is_fair_scheduling_workspace(session: WebSession, workspace_id: str) -> bool:
    """Return the live workspace capability used by qz priority selectors."""
    cached = getattr(session, "all_workspace_fair_scheduling", None) or {}
    if workspace_id in cached:
        return cached[workspace_id] is True

    if not isinstance(session, WebSession) or not _WS_ID_RE.match(workspace_id):
        return False

    try:
        results = _workspace_route_entries(
            session,
            base_url=None,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        raise WorkspaceCapabilityError(
            "Could not resolve the selected workspace's scheduling policy."
        ) from exc
    _cache_fair_scheduling(session, results)
    if workspace_id not in results:
        raise WorkspaceCapabilityError(
            "Could not resolve the selected workspace's scheduling policy."
        )
    return bool(results[workspace_id]["is_fair_workspace"])


# `-1` is how the platform spells "no ceiling" in every quota field.
__all__ = [
    "WorkspaceCapabilityError",
    "WorkspaceEnumerationError",
    "is_fair_scheduling_workspace",
    "try_enumerate_workspaces",
]
