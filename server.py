#!/usr/bin/env python3
# server.py
import asyncio
import base64
import json
import os
import time
import traceback
from aiohttp import web, WSMsgType

CAPTURE_DIR = "captures"
os.makedirs(CAPTURE_DIR, exist_ok=True)

connected_clients = set()        # set of WebSocketResponse
pending_captures = {}            # requestId -> {"future": fut, "return_base64": bool} or old-style Future

# Score state (server-side authoritative snapshot)
score_state = {
    "ai1": 0,
    "ai2": 0,
    "match": 1
}

# config
CAPTURE_TIMEOUT = 8.0            # seconds to wait for an image reply
DEBUG = True
MAX_CAPTURE_BYTES = 10 * 1024 * 1024  # 10 MB safety limit

def log(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

def safe_str(x):
    return str(x) if x is not None else ""


async def websocket_handler(request):
    """
    WebSocket endpoint: clients (browser) should connect to ws://host:port/ws
    This handler now supports resolving pending captures where the pending entry
    may be a dict: {"future": fut, "return_base64": bool}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer = request.remote
    connected_clients.add(ws)
    log(f"[WS CONNECT] {peer}  total_clients={len(connected_clients)}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # parse JSON if possible
                try:
                    data = json.loads(msg.data)
                except Exception:
                    log("[WS] Received non-JSON text (len {})".format(len(msg.data)))
                    continue

                mtype = data.get("type")
                if mtype in ("image_capture", "frame_image"):
                    # normalize ids
                    capture_id = data.get("captureId") or data.get("requestId") or data.get("captureTimestamp")
                    image_field = data.get("imageData") or data.get("image_base64") or data.get("image_base64_payload") or data.get("image")
                    if not image_field:
                        log("[WS] image message received but no image field,", list(data.keys()))
                        continue

                    # strip data: prefix if present
                    if isinstance(image_field, str) and image_field.startswith("data:"):
                        comma = image_field.find(',')
                        if comma != -1:
                            b64_payload = image_field[comma+1:]
                        else:
                            b64_payload = image_field
                    else:
                        b64_payload = image_field

                    # normalize padding for string
                    if isinstance(b64_payload, str):
                        b64_payload = b64_payload.strip()
                        missing_padding = len(b64_payload) % 4
                        if missing_padding:
                            b64_payload += "=" * (4 - missing_padding)

                    # try decode to verify and get binary
                    try:
                        binary = base64.b64decode(b64_payload)
                    except Exception as e:
                        log("[WS] base64 decode failed:", e)
                        continue

                    # safety size check
                    if len(binary) > MAX_CAPTURE_BYTES:
                        log(f"[WS] Received capture too large ({len(binary)} bytes) - rejecting.")
                        continue

                    # determine format (default jpeg)
                    fmt = (data.get("format") or data.get("mime_type") or data.get("imageFormat") or "jpeg").lower()
                    ext = "jpg" if "jpeg" in fmt or "jpg" in fmt else ("png" if "png" in fmt else "bin")

                    # save file
                    ts = int(time.time() * 1000)
                    safe_id = str(capture_id).replace("/", "_").replace(" ", "_") if capture_id else f"noid_{ts}"
                    fname = f"capture_{safe_id}_{ts}.{ext}"
                    fpath = os.path.join(CAPTURE_DIR, fname)
                    try:
                        with open(fpath, "wb") as f:
                            f.write(binary)
                        size_kb = len(binary) / 1024.0
                        log(f"[WS] Saved capture {fname} ({size_kb:.1f} KB)")
                    except Exception as e:
                        log("[WS] Failed to save capture:", e)
                        continue

                    # resolve waiting future(s) by id
                    resolved = False

                    # Helper to resolve an entry popped from pending_captures
                    def _resolve_entry(entry, key_value):
                        # entry may be either the old-style future or our new dict
                        if entry is None:
                            return False
                        fut = entry["future"] if isinstance(entry, dict) else entry
                        want_b64 = False
                        if isinstance(entry, dict):
                            want_b64 = bool(entry.get("return_base64", False))
                        if fut and not fut.done():
                            if want_b64:
                                # return the base64 string (NOT the data: prefix)
                                fut.set_result({"image_base64": b64_payload, "size_kb": size_kb, "requestId": key_value})
                            else:
                                fut.set_result({"filename": fpath, "size_kb": size_kb, "requestId": key_value})
                            return True
                        return False

                    if capture_id is not None:
                        key = str(capture_id)
                        entry = pending_captures.pop(key, None)
                        if _resolve_entry(entry, capture_id):
                            resolved = True

                    # try alt requestId if provided and not yet resolved
                    alt_req = data.get("requestId")
                    if alt_req is not None and not resolved:
                        key2 = str(alt_req)
                        entry2 = pending_captures.pop(key2, None)
                        if _resolve_entry(entry2, alt_req):
                            resolved = True

                    if not resolved:
                        log("[WS] Received image but no matching pending request (maybe late reply). request keys:", list(data.keys()))

                else:
                    # other messages: optionally log or ignore
                    pass

            elif msg.type == WSMsgType.ERROR:
                log("[WS] connection error:", ws.exception())

    except Exception as exc:
        log("[WS] Exception in websocket handler:", exc)
        traceback.print_exc()

    finally:
        try:
            connected_clients.discard(ws)
            await ws.close()
        except Exception:
            pass
        log(f"[WS DISCONNECT] {peer}  total_clients={len(connected_clients)}")

    return ws


async def capture_request_handler(request):
    """
    HTTP POST/GET /capture
    Broadcasts capture_request and waits for first reply that matches requestId.

    Supports optional "returnBase64": true in the JSON body to return the base64 payload
    (image_base64) in the HTTP response instead of the saved filename.
    """
    # read optional JSON body (for GET, we'll accept query params too)
    payload = {}
    if request.method == "POST":
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        # allow GET with query params
        payload = dict(request.query)

    timeout = float(payload.get("timeout", CAPTURE_TIMEOUT))
    raw_req_id = payload.get("requestId")
    req_id = raw_req_id or f"server_req_{int(time.time()*1000)}_{int.from_bytes(os.urandom(2), 'big')}"
    req_key = str(req_id)
    options = payload.get("captureOptions", {}) if isinstance(payload.get("captureOptions", {}), dict) else {}
    quality = float(options.get("quality", payload.get("quality", 0.8)))
    downscale = float(options.get("downscale", payload.get("downscale", 1.0)))
    fmt = payload.get("format", "jpeg")
    # new flag
    return_base64 = bool(payload.get("returnBase64", False))

    if not connected_clients:
        return web.json_response({"ok": False, "error": "no_connected_clients"}, status=503)

    # Build message for clients
    message = {
        "type": "capture_request",
        "requestId": req_id,
        "timestamp": int(time.time() * 1000),
        "captureOptions": {
            "format": fmt,
            "quality": quality,
            "downscale": downscale
        }
    }

    # Future used to wait for result
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    # store dict so websocket handler knows whether to return base64
    pending_captures[req_key] = {"future": fut, "return_base64": return_base64}

    # Broadcast to clients; remove clients that fail
    text = json.dumps(message)
    bad = []
    for ws in list(connected_clients):
        try:
            await ws.send_str(text)
        except Exception as e:
            log("[WARN] Failed sending capture_request to a client; removing. err:", e)
            bad.append(ws)
    for b in bad:
        connected_clients.discard(b)
        try:
            await b.close()
        except Exception:
            pass

    # Wait for a matching result or timeout
    try:
        # Confirm entry exists and get the future
        entry = pending_captures.get(req_key)
        if not entry:
            return web.json_response({"ok": False, "error": "internal", "detail": "missing_pending_entry"}, status=500)
        fut = entry["future"]

        result = await asyncio.wait_for(fut, timeout=timeout)
        # result is already the dict set by websocket handler
        # return it directly
        return web.json_response({"ok": True, "result": result})
    except asyncio.TimeoutError:
        # cleanup and return timeout
        if 'fut' in locals() and not fut.done():
            fut.cancel()
        pending_captures.pop(req_key, None)
        return web.json_response({"ok": False, "error": "timeout_waiting_for_capture", "requestId": req_id}, status=504)
    except Exception as e:
        pending_captures.pop(req_key, None)
        log("[ERROR] capture handler exception:", e)
        return web.json_response({"ok": False, "error": "internal", "detail": str(e)}, status=500)


async def clients_handler(request):
    return web.json_response({"connected_clients": len(connected_clients)})


async def list_captures_handler(request):
    """
    Simple helper: list saved captures (filename and size). Not paginated — meant for debug.
    """
    files = []
    for fn in sorted(os.listdir(CAPTURE_DIR), reverse=True):
        p = os.path.join(CAPTURE_DIR, fn)
        try:
            stat = os.stat(p)
            files.append({"filename": fn, "size_kb": stat.st_size / 1024.0, "mtime": int(stat.st_mtime)})
        except Exception:
            continue
    return web.json_response({"ok": True, "captures": files})


# -------------------------
# New: AI prediction handler (POST /predict)
# -------------------------
async def ai_prediction_handler(request):
    """
    POST/GET /predict
    Accepts JSON body describing AI prediction and forwards it to all connected WS clients.

    Expected POST body (example):
    {
      "model": "ai1",
      "targetY": 320,
      "confidence": 0.92,
      "immediate": false
    }

    Or provide a full message to forward:
    {
      "type": "control",
      "action": "set_paddle",
      "paddle": "ai1",
      "y": 320,
      "immediate": true
    }
    """
    # Read JSON (POST) or query (GET)
    payload = {}
    if request.method == 'POST':
        try:
            payload = await request.json()
        except Exception:
            # try parsing raw text
            try:
                text = await request.text()
                payload = json.loads(text) if text else {}
            except Exception:
                payload = {}
    else:
        payload = dict(request.query)

    # unwrap wrapper if present
    if isinstance(payload, dict) and 'message' in payload and isinstance(payload['message'], dict):
        message_obj = payload['message']
    else:
        message_obj = payload

    if not isinstance(message_obj, dict) or not message_obj:
        return web.json_response({"ok": False, "error": "invalid_or_empty_payload"}, status=400)

    # Normalize shorthand prediction into a standard ai_prediction message
    if message_obj.get('type') is None and ('model' in message_obj and 'targetY' in message_obj):
        normalized = {
            "type": "ai_prediction",
            "requestId": message_obj.get("requestId", f"pred_{int(time.time()*1000)}"),
            "model": message_obj.get("model"),
            "targetY": float(message_obj.get("targetY")),
            "confidence": message_obj.get("confidence"),
            "immediate": bool(message_obj.get("immediate", False)),
            "timestamp": int(time.time() * 1000)
        }
        out_msg = normalized
    else:
        out_msg = message_obj
        if 'type' not in out_msg:
            out_msg['type'] = 'ai_prediction'
        out_msg.setdefault('timestamp', int(time.time() * 1000))
        if 'requestId' not in out_msg:
            out_msg['requestId'] = f"pred_{int(time.time()*1000)}"

    text = json.dumps(out_msg)

    # Broadcast to all connected clients (best-effort). remove broken clients.
    bad = []
    sent = 0
    for ws in list(connected_clients):
        try:
            await ws.send_str(text)
            sent += 1
        except Exception as e:
            log("[WARN] Failed sending ai_prediction to a client; removing. err:", e)
            bad.append(ws)

    for b in bad:
        connected_clients.discard(b)
        try:
            await b.close()
        except Exception:
            pass

    return web.json_response({"ok": True, "sent": sent, "message": out_msg})


# -------------------------
# New: explicit control handler (POST /control)
# -------------------------
async def control_handler(request):
    """
    POST/GET /control
    Body (json): { "paddle": "ai1"|"ai2"|"left"|"right", "y": <number>, "immediate": true|false }
    Broadcasts a control message to all connected websocket clients.
    """
    try:
        if request.method == 'POST':
            payload = await request.json()
        else:
            payload = dict(request.query)
    except Exception:
        payload = {}

    paddle = payload.get('paddle')
    y = payload.get('y')
    immediate = payload.get('immediate', False)

    # validate y
    try:
        y = float(y)
    except Exception:
        return web.json_response({'ok': False, 'error': 'invalid_y'}, status=400)

    if paddle not in ('ai1', 'ai2', 'left', 'right'):
        return web.json_response({'ok': False, 'error': 'invalid_paddle'}, status=400)

    msg = json.dumps({
        'type': 'control',
        'action': 'set_paddle',
        'paddle': paddle,
        'y': y,
        'immediate': bool(immediate),
        'timestamp': int(time.time() * 1000)
    })

    bad = []
    sent = 0
    for ws in list(connected_clients):
        try:
            await ws.send_str(msg)
            sent += 1
        except Exception as e:
            log("[WARN] Failed sending control to a client; removing. err:", e)
            bad.append(ws)

    for b in bad:
        connected_clients.discard(b)
        try:
            await b.close()
        except Exception:
            pass

    return web.json_response({'ok': True, 'broadcasted': True, 'sent': sent, 'paddle': paddle, 'y': y})


# -------------------------
# New: generic broadcast endpoint (POST /broadcast)
# -------------------------
async def broadcast_handler(request):
    """
    POST/GET /broadcast
    Accepts JSON body (or query params for GET) and forwards it to all connected websocket clients.

    Example POST body (already the message you want clients to receive):
      {"type":"ai_prediction","requestId":"pred_123","model":"ai1","targetY":320}

    Or wrapper:
      {"message": {"type":"control","action":"set_paddle","paddle":"ai1","y":320}}

    Response: {"ok": True, "sent": <n_clients>}
    """
    # read body (POST) or query (GET)
    payload = {}
    if request.method == 'POST':
        try:
            payload = await request.json()
        except Exception:
            # fallback: treat body as raw text -> try parse
            try:
                text = await request.text()
                payload = json.loads(text) if text else {}
            except Exception:
                payload = {}
    else:
        payload = dict(request.query)

    # If user wrapped the message in { "message": ... } use that
    if isinstance(payload, dict) and 'message' in payload and isinstance(payload['message'], (dict, list)):
        message_obj = payload['message']
    else:
        message_obj = payload

    # If message is empty, return error
    if not message_obj:
        return web.json_response({"ok": False, "error": "empty_message"}, status=400)

    text = json.dumps(message_obj)

    bad = []
    sent = 0
    for ws in list(connected_clients):
        try:
            await ws.send_str(text)
            sent += 1
        except Exception as e:
            log("[WARN] Failed sending broadcast to a client; removing. err:", e)
            bad.append(ws)

    for b in bad:
        connected_clients.discard(b)
        try:
            await b.close()
        except Exception:
            pass

    return web.json_response({"ok": True, "sent": sent})


# -------------------------
# New: Score endpoint (GET/POST /score)
# -------------------------
async def score_handler(request):
    """
    GET /score -> returns current score_state
    POST /score -> accepts JSON to update score_state (partial updates allowed),
                   then broadcasts a {"type":"score_update", ...} message to clients.

    Example POST body:
      {"ai1": 3, "ai2": 2, "match": 1}
    """
    global score_state

    if request.method == 'GET':
        return web.json_response({"ok": True, "score": score_state})

    # POST: update
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        # try raw text parse
        try:
            txt = await request.text()
            payload = json.loads(txt) if txt else {}
        except Exception:
            payload = {}

    if not isinstance(payload, dict) or not payload:
        return web.json_response({"ok": False, "error": "invalid_or_empty_payload"}, status=400)

    # Validate and update only provided keys
    updated = {}
    errors = []
    if 'ai1' in payload:
        try:
            score_state['ai1'] = int(payload['ai1'])
            updated['ai1'] = score_state['ai1']
        except Exception:
            errors.append("invalid_ai1")
    if 'ai2' in payload:
        try:
            score_state['ai2'] = int(payload['ai2'])
            updated['ai2'] = score_state['ai2']
        except Exception:
            errors.append("invalid_ai2")
    if 'match' in payload:
        try:
            score_state['match'] = int(payload['match'])
            updated['match'] = score_state['match']
        except Exception:
            errors.append("invalid_match")

    if errors:
        return web.json_response({"ok": False, "error": "validation_failed", "details": errors}, status=400)

    # Broadcast the new score to all connected clients
    msg = {
        "type": "score_update",
        "ai1Score": score_state['ai1'],
        "ai2Score": score_state['ai2'],
        "match": score_state['match'],
        "timestamp": int(time.time() * 1000)
    }
    text = json.dumps(msg)

    bad = []
    sent = 0
    for ws in list(connected_clients):
        try:
            await ws.send_str(text)
            sent += 1
        except Exception as e:
            log("[WARN] Failed sending score_update to a client; removing. err:", e)
            bad.append(ws)

    for b in bad:
        connected_clients.discard(b)
        try:
            await b.close()
        except Exception:
            pass

    return web.json_response({"ok": True, "score": score_state, "broadcasted": True, "sent": sent})


async def init_app():
    app = web.Application()
    app.router.add_get("/ws", websocket_handler)

    # capture endpoints
    app.router.add_post("/capture", capture_request_handler)
    app.router.add_get("/capture", capture_request_handler)  # allow GET for quick test

    # prediction / control / broadcast endpoints
    app.router.add_post("/predict", ai_prediction_handler)
    app.router.add_get("/predict", ai_prediction_handler)
    app.router.add_post("/control", control_handler)
    app.router.add_get("/control", control_handler)
    app.router.add_post("/broadcast", broadcast_handler)
    app.router.add_get("/broadcast", broadcast_handler)

    # score endpoint
    app.router.add_get("/score", score_handler)
    app.router.add_post("/score", score_handler)

    # info endpoints
    app.router.add_get("/clients", clients_handler)
    app.router.add_get("/captures", list_captures_handler)
    return app


def main():
    app = asyncio.run(init_app())
    web.run_app(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
