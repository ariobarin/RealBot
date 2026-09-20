"""Keyboard drive for the local visitor app over its existing SSH tunnel."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from drive_commands import WHEEL_VEL_COMBOS, wheel_vels_to_twist

router = APIRouter()


def open_drive():
    from bbos import Config, Type, Writer

    width = float(Config('drive').robot_width)
    return Writer('drive.ctrl', Type('drive_ctrl'), keeptime=False), width


@router.websocket('/drive')
async def drive(websocket: WebSocket):
    if websocket.headers.get('origin') != 'http://127.0.0.1:5178':
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        writer, width = open_drive()
    except RuntimeError:
        await websocket.close(code=1013, reason='Drive controls are in use or unavailable')
        return
    try:
        writer['twist'] = (0.0, 0.0)
        await websocket.send_json({'ready': True})
        while True:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=0.25)
            if (not isinstance(message, dict)
                    or not isinstance(message.get('keys'), str)
                    or message['keys'] not in WHEEL_VEL_COMBOS
                    or type(message.get('shift')) is not bool):
                raise ValueError('Invalid drive command')
            left, right = WHEEL_VEL_COMBOS[message['keys']]
            gain = 1.0 if message['shift'] else 0.5
            writer['twist'] = wheel_vels_to_twist(left * gain, right * gain, width)
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason='Drive timed out. Enable it again to resume.')
    except ValueError:
        await websocket.close(code=1008, reason='Invalid drive command')
    except WebSocketDisconnect:
        pass
    finally:
        try:
            writer['twist'] = (0.0, 0.0)
        finally:
            writer.__exit__(None, None, None)
