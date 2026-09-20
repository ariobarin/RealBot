"""Expiring local commands for live2's visitor mode. A stopped attempt cannot resume."""
import json
import os
import time
from pathlib import Path


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class VisitorControl:
    def __init__(self, path):
        self.path = Path(path)
        self.state_path = self.path.with_suffix('.state.json')
        self.attempt = ''
        self.blocked = ''
        self.phase = 'loading'

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            if (isinstance(value.get('attempt'), str) and value['attempt']
                    and type(value.get('until')) in (float, int)
                    and 0 < value['until'] - time.monotonic() <= 2):
                return value['attempt']
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return ''

    def tick(self):
        attempt = self.read()
        if not attempt or attempt == self.blocked:
            if self.attempt:
                self.blocked = self.attempt
            self.report('held')
            return False, False
        restart = attempt != self.attempt
        self.attempt = attempt
        self.report('running')
        return True, restart

    def hold(self):
        self.blocked = self.attempt or self.read()
        self.report('held')

    def report(self, phase, reason=''):
        self.phase = phase
        write_json(self.state_path, {'phase': phase, 'attempt': self.attempt,
                                    'at': time.monotonic(), 'reason': reason, 'pid': os.getpid()})
