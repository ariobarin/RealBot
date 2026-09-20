"""Local IK camera page with heartbeat control. Stop keeps torque on."""
import argparse
import asyncio
import secrets
import signal
import socket
import time
from pathlib import Path

import numpy as np
from aiohttp import web

from .adapters.bbos_free_cam import BbosWrist


class WristSession:
    def __init__(self, robot, control=False):
        self.robot, self.control = robot, control
        self.session = None
        self.phase = 'idle'
        self.reason = ''
        self.beat = self.started = 0.

    def stop(self, reason=None):
        if self.robot.motion is not None:
            self.robot.motion.stop()
        self.session = None
        self.phase = 'held' if self.robot.motion is not None else 'idle'
        self.reason = reason or ('Stopped; arm and base remain held' if self.robot.motion is not None
                                 else 'Stopped; no controls acquired')

    def command(self, message, now):
        action = message.get('action')
        if action == 'stop':
            self.stop()
            return
        expires = message.get('expires')
        if type(expires) not in (float, int) or not time.time() < expires <= time.time()+2:
            raise ValueError('Command expired; refresh the page')
        if action == 'release':
            if message.get('supported') is not True:
                raise ValueError('Support the arm before releasing torque')
            self.stop()
            self.robot.release()
            self.phase, self.reason = 'idle', 'Torque released'
            return
        if action == 'start':
            if not self.control:
                raise ValueError('Read-only process; launch with --control to enable Start')
            if self.phase in ('starting', 'active'):
                raise ValueError('Free Cam is already active; Stop before taking over')
            self.robot.health()
            if self.robot.motion is None:
                self.robot.acquire()
                self.started = now
                self.phase = 'starting'
            else:
                self.phase = 'active'
            self.robot.motion.sequence = -1
            self.session = secrets.token_hex(16)
            self.beat, self.reason = now, ''
            return
        if not self.session or message.get('session') != self.session:
            raise ValueError('Inactive or stale controller session')
        if now-self.beat >= 1:
            self.stop('Browser heartbeat expired; held stopped')
            raise ValueError(self.reason)
        if action == 'heartbeat':
            self.beat = now
        elif action == 'jog' and self.phase == 'active':
            self.robot.motion.jog(message.get('pan'), message.get('tilt'), message.get('sequence'), now)
        elif action == 'pose' and self.phase == 'active':
            self.robot.motion.pose(message.get('pan'), message.get('tilt'), message.get('sequence'), now)
        elif action == 'open_gripper' and self.phase == 'active':
            self.robot.open_gripper(now)
        else:
            raise ValueError('Command unavailable while starting')

    def tick(self, now):
        if self.robot.motion is None:
            return
        try:
            if self.session and now-self.beat >= 1:
                self.stop('Browser heartbeat expired; held stopped')
            if self.phase == 'starting' and now-self.started < 1.5:
                self.robot.health(arm=False)  # bbOS mode changes briefly interrupt arm feedback.
            else:
                state = self.robot.health()
                if np.max(np.abs(state['pos']-self.robot.motion.position)) > .025:
                    raise RuntimeError('Arm is not tracking its held pose')
                if self.phase == 'starting':
                    self.phase = 'active'
                if self.phase == 'active':
                    self.robot.motion.tick(now)
        except Exception as error:
            self.stop(str(error))
        self.robot.write()

    def state(self):
        motion = self.robot.motion
        return dict(phase=self.phase, reason=self.reason, control=self.control,
                    robot=socket.gethostname(),
                    moving=bool(self.session and motion and (not motion.ready or motion.move is not None or motion.gripper_target is not None or np.any(motion.direction))),
                    ready=bool(motion and motion.ready), notice=motion.notice if motion else '',
                    offsets=motion.offsets() if motion else [0, 0], now=time.time())


async def serve(args):
    if socket.gethostname() != args.robot:
        raise RuntimeError('Robot hostname does not match --robot')
    robot = BbosWrist(args.bbos_root)
    controller = WristSession(robot, args.control)
    token, ending = secrets.token_urlsafe(24), asyncio.Event()

    def signal_stop():
        controller.stop('Terminal interrupted; support the arm and use Release to exit')
        if robot.motion is None:
            ending.set()
        else:
            print(controller.reason, flush=True)

    async def handle(request):
        if request.path == '/' and request.method == 'GET':
            return web.Response(text=Path(__file__).with_name('wrist_demo.html').read_text(), content_type='text/html',
                                headers={'Cache-Control': 'no-store'})
        if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer '+token):
            raise web.HTTPUnauthorized()
        try:
            if request.path == '/camera' and request.method == 'GET':
                return web.Response(body=robot.jpeg(), content_type='image/jpeg', headers={'Cache-Control': 'no-store'})
            if request.path == '/state' and request.method == 'GET':
                return web.json_response(controller.state(), headers={'Cache-Control': 'no-store'})
            if request.path == '/command' and request.method == 'POST':
                message = await request.json()
                if not isinstance(message, dict):
                    raise ValueError('Expected a command object')
                controller.command(message, time.monotonic())
                result = controller.state()
                if message.get('action') == 'start':
                    result['session'] = controller.session
                return web.json_response(result)
            raise web.HTTPNotFound()
        except (ValueError, RuntimeError, KeyError) as error:
            return web.json_response({'error': str(error)}, status=409)

    app = web.Application(client_max_size=2048)
    app.router.add_route('*', '/{path:.*}', handle)
    runner = web.AppRunner(app, handle_signals=False)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_stop)
    try:
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', args.port).start()
        print(f'http://127.0.0.1:{args.port}/#{token}', flush=True)
        while not ending.is_set():
            controller.tick(time.monotonic())
            await asyncio.sleep(.02)
    finally:
        await runner.cleanup()
        robot.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bbos-root', type=Path, default=Path.home()/'bbos')
    parser.add_argument('--robot', default='bracketbot-0188')
    parser.add_argument('--port', type=int, default=8012)
    parser.add_argument('--control', action='store_true')
    asyncio.run(serve(parser.parse_args()))


if __name__ == '__main__':
    main()
