"""HTTP API for heartbeats: `/api/heartbeats/{kind}/{id}`.

GET reads a target's heartbeat (null when it has none) and its recent ticks,
PUT sets or replaces it, PATCH changes its prompt or interval or pauses it,
DELETE stops it. The `yuki-conductor heartbeat` CLI and the web UI both use
these; `heartbeat.py` does the work.
"""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yuki_conductor import heartbeat
from yuki_conductor import heartbeat_store as hs
from yuki_conductor.heartbeat import HeartbeatError

router = APIRouter()


class HeartbeatSet(BaseModel):
    prompt: str
    interval_s: int = hs.DEFAULT_INTERVAL_S
    lifetime_s: int = hs.MAX_LIFETIME_S
    created_by: str = "user"


class HeartbeatPatch(BaseModel):
    prompt: str | None = None
    interval_s: int | None = None
    paused: bool | None = None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HeartbeatError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("/api/heartbeats/{kind}/{target_id}")
def get_heartbeat(kind: str, target_id: str):
    hb = _call(heartbeat.get, kind, target_id)
    return {
        "heartbeat": heartbeat.serialize(hb),
        "ticks": [asdict(t) for t in hs.store.ticks(kind, target_id)],
    }


@router.put("/api/heartbeats/{kind}/{target_id}")
def set_heartbeat(kind: str, target_id: str, body: HeartbeatSet):
    hb = _call(
        heartbeat.set_heartbeat, kind, target_id, body.prompt,
        interval_s=body.interval_s, lifetime_s=body.lifetime_s, created_by=body.created_by,
    )
    return heartbeat.serialize(hb)


@router.patch("/api/heartbeats/{kind}/{target_id}")
def update_heartbeat(kind: str, target_id: str, body: HeartbeatPatch):
    hb = _call(
        heartbeat.update, kind, target_id,
        prompt=body.prompt, interval_s=body.interval_s, paused=body.paused,
    )
    return heartbeat.serialize(hb)


@router.delete("/api/heartbeats/{kind}/{target_id}")
def stop_heartbeat(kind: str, target_id: str):
    if not _call(heartbeat.stop, kind, target_id):
        raise HTTPException(status_code=404, detail="No heartbeat on this target")
    return {"ok": True}
