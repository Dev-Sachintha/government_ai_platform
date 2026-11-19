import os, secrets, threading, time, json
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, Response
from pathlib import Path
from pydantic import BaseModel, Field

API_KEY = os.environ.get("MOBILE_MONITOR_API_KEY", "").strip()
if len(API_KEY) < 32:
    raise RuntimeError("Set MOBILE_MONITOR_API_KEY to a random secret of at least 32 characters")

app = FastAPI(title="Government AI Platform - Mobile Monitor API", version="1.0.0")
lock = threading.Lock()
devices = {}
frames = {}
pc_metrics = {}
# Short-lived one-time launch tickets; values are held in memory and never expose the API key in a URL.
pc_launch_tickets = {}
FRAME_TTL_SECONDS = 180
PC_LAUNCH_TTL_SECONDS = 300

def authorize(value: Optional[str]):
    if not value or not secrets.compare_digest(value, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")

class Heartbeat(BaseModel):
    device_id: str = Field(min_length=8, max_length=80)
    kind: str = Field(default="android", max_length=20)   # "android" phone or "pc" browser
    device_name: str = Field(default="Android phone", max_length=80)
    manufacturer: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=100)
    android_version: str = Field(default="", max_length=40)
    battery_percent: Optional[int] = Field(default=None, ge=0, le=100)
    network_type: str = Field(default="unknown", max_length=40)
    screen_resolution: str = Field(default="", max_length=40)
    login_user: str = Field(default="", max_length=80)

class PCLaunchRequest(BaseModel):
    login_user: str = Field(default="User", min_length=1, max_length=80)

class PCMetrics(BaseModel):
    device_id: str = Field(min_length=8, max_length=80)
    device_name: str = Field(default="PC", max_length=80)
    kind: str = "pc"
    metrics: dict


PC_PAGE = Path(__file__).with_name("pc_share.html")


@app.post("/v1/pc/launch")
def create_pc_launch(payload: PCLaunchRequest, x_monitor_key: Optional[str] = Header(default=None)):
    """Create a one-time, short-lived PC sharing ticket for an authenticated dashboard operator."""
    authorize(x_monitor_key)
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with lock:
        # Discard expired tickets while keeping the in-memory store bounded.
        for old_ticket, item in list(pc_launch_tickets.items()):
            if item["expires_at"] <= now:
                pc_launch_tickets.pop(old_ticket, None)
        pc_launch_tickets[ticket] = {
            "login_user": payload.login_user.strip() or "User",
            "expires_at": now + PC_LAUNCH_TTL_SECONDS,
        }
    return {"url": "/pc?launch=" + ticket, "expires_in": PC_LAUNCH_TTL_SECONDS}


@app.get("/pc", response_class=HTMLResponse)
def pc_share_page(launch: Optional[str] = Query(default=None, max_length=100)):
    """Share page. Auto tickets prefill the pairing key and account name once; manual mode remains available."""
    page = PC_PAGE.read_text(encoding="utf-8")
    auto_config = {"enabled": False, "pairing_key": "", "login_user": ""}
    if launch:
        with lock:
            ticket_data = pc_launch_tickets.pop(launch, None)  # one-time use
        if not ticket_data or ticket_data["expires_at"] <= time.time():
            raise HTTPException(status_code=401, detail="This automatic sharing link has expired or was already used. Generate a new one from Screen Monitor.")
        auto_config = {
            "enabled": True,
            "pairing_key": API_KEY,
            "login_user": ticket_data["login_user"],
        }
    page = page.replace("/*__GOV_AUTO_CONFIG__*/", "window.GOV_AUTO_CONFIG = " + json.dumps(auto_config) + ";")
    return HTMLResponse(
        page,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/heartbeat")
def heartbeat(payload: Heartbeat, x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        item = payload.model_dump()
        item["kind"] = "pc" if item.get("kind") == "pc" else "android"
        item["last_seen"] = now
        item["online"] = True
        devices[payload.device_id] = item
    return {"ok": True, "last_seen": now}

@app.post("/v1/frame")
async def upload_frame(device_id: str = Form(...), frame: UploadFile = File(...), x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    if len(device_id) < 8 or len(device_id) > 80:
        raise HTTPException(status_code=400, detail="Invalid device id")
    if frame.content_type not in ("image/jpeg", "image/jpg"):
        raise HTTPException(status_code=415, detail="JPEG frames only")
    data = await frame.read(2 * 1024 * 1024 + 1)
    if not data or len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Frame must be under 2 MB")
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="Invalid JPEG")
    with lock:
        frames[device_id] = (data, time.time())
    return {"ok": True}

@app.post("/v1/pc/metrics")
def receive_pc_metrics(payload: PCMetrics, x_monitor_key: Optional[str] = Header(default=None)):
    """Receive telemetry explicitly shared by a locally running PC agent."""
    authorize(x_monitor_key)
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        devices[payload.device_id] = {
            "device_id": payload.device_id, "kind": "pc", "device_name": payload.device_name,
            "last_seen": now, "online": True,
        }
        pc_metrics[payload.device_id] = {"metrics": payload.metrics, "received_at": now}
    return {"ok": True, "received_at": now}


@app.get("/v1/pc/metrics")
def get_pc_metrics(device_id: str, x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    with lock:
        item = pc_metrics.get(device_id)
    if not item:
        raise HTTPException(status_code=404, detail="No PC metrics received yet")
    return item


@app.get("/v1/devices")
def list_devices(x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    now = time.time()
    with lock:
        result = []
        for device_id, raw in devices.items():
            item = dict(raw)
            last = datetime.fromisoformat(item["last_seen"]).timestamp()
            item["online"] = (now - last) < 90
            result.append(item)
    result.sort(key=lambda x: x.get("device_name", "").lower())
    return {"devices": result}

@app.get("/v1/devices/{device_id}/frame")
def latest_frame(device_id: str, x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    with lock:
        item = frames.get(device_id)
    if not item or time.time() - item[1] > FRAME_TTL_SECONDS:
        raise HTTPException(status_code=404, detail="No recent frame")
    return Response(content=item[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

@app.delete("/v1/devices/{device_id}")
def forget_device(device_id: str, x_monitor_key: Optional[str] = Header(default=None)):
    authorize(x_monitor_key)
    with lock:
        devices.pop(device_id, None)
        frames.pop(device_id, None)
        pc_metrics.pop(device_id, None)
    return {"ok": True}
