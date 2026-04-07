"""Google Cloud Functions handlers for CraftCompanion session logs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from flask import jsonify
from google.cloud import firestore


db = firestore.Client()
COLLECTION = "craft_companion_sessions"


def _json_error(message: str, code: int):
    return jsonify({"error": message}), code


def log_session(request):
    """HTTP Cloud Function: persist a connect/disconnect snapshot."""
    if request.method != "POST":
        return _json_error("Use POST", 405)

    payload = request.get_json(silent=True) or {}
    session = payload.get("session") or {}
    started_at = session.get("started_at") or datetime.now(tz=timezone.utc).isoformat()

    doc_id = started_at.replace(":", "_")
    record: Dict[str, Any] = {
        "event": payload.get("event", "unknown"),
        "timestamp": payload.get("timestamp", datetime.now(tz=timezone.utc).isoformat()),
        "session": {
            "started_at": started_at,
            "screenshots_sent": int(session.get("screenshots_sent", 0)),
            "coords_visited": session.get("coords_visited", []),
        },
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection(COLLECTION).document(doc_id).set(record, merge=True)
    return jsonify({"ok": True, "id": doc_id}), 200


def get_session(request):
    """HTTP Cloud Function: fetch the latest session summary."""
    if request.method != "GET":
        return _json_error("Use GET", 405)

    query = (
        db.collection(COLLECTION)
        .order_by("updated_at", direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        return jsonify({"session": None, "message": "No sessions found"}), 200

    data = docs[0].to_dict() or {}
    return jsonify({"session": data}), 200


def session_api(request):
    """Single-entrypoint router.

    /session_api?action=log -> POST body used by log_session
    /session_api?action=get -> GET for most recent session
    """
    action = (request.args.get("action") or "").lower()
    if action == "log":
        return log_session(request)
    if action == "get":
        return get_session(request)
    return _json_error("action must be 'log' or 'get'", 400)
