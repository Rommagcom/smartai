from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.core.security import decode_token
from app.services.websocket_manager import connection_manager

router = APIRouter()


@router.websocket("/chat")
async def ws_chat(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4001)
            return
        user_id = payload.get("sub")
    except JWTError:
        await websocket.close(code=4001)
        return

    await connection_manager.connect(websocket, user_id=user_id)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket, user_id=user_id)
