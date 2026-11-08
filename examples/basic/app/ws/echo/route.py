"""Minimal WebSocket echo endpoint: text and binary messages are sent back unchanged.

Connect from a browser with::

    const ws = new WebSocket("ws://127.0.0.1:3000/ws/echo");
    ws.onmessage = (event) => console.log(event.data);
    ws.onopen = () => ws.send("hello");

Messages larger than 64 KiB are refused with close code 1009. There is no
subprotocol negotiation and no broadcast: each connection only hears its own
messages back.
"""
from pynext import WebSocket, WebSocketDisconnect, websocket


@websocket
async def echo(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            kind, data = await websocket.receive()
            if kind == "text":
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)
    except WebSocketDisconnect:
        # The peer went away; nothing to send. Any per-connection cleanup
        # belongs here (this handler holds no resources beyond the socket).
        pass
