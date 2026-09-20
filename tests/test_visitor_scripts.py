"""ScriptRelay runs only the three allowlisted demo scripts, only for the current visitor."""
import asyncio
import json
import time

import pytest

from visitor_actions import ScriptRelay


def command(**values):
    return json.dumps(dict(expiresAt=int(time.time() * 1000) + 700, **values))


def relay(tmp_path, body='#!/bin/sh\necho done\n'):
    (tmp_path / 'run-v3/best').mkdir(parents=True)
    (tmp_path / 'run-v3/best/model.safetensors').write_bytes(b'')
    for name in ('demo.sh', 'go.sh', 'stop.sh'):
        script = tmp_path / name
        script.write_text(body)
        script.chmod(0o755)
    instance = ScriptRelay('visitor', root=tmp_path)
    instance.enabled = True  # The hostname gate in __init__ only passes on 0188 itself.
    return instance


async def drain():
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_only_the_visitor_can_run_only_the_three_scripts(tmp_path):
    instance = relay(tmp_path)
    for caller, payload in [('someone-else', command(script='go')),
                            ('visitor', command(script='rm -rf /')),
                            ('visitor', command(script='../../bin/sh')),
                            ('visitor', json.dumps({'script': 'go',
                                                    'expiresAt': int(time.time() * 1000) - 1}))]:
        with pytest.raises(ValueError):
            asyncio.run(instance.run(caller, payload))
    assert instance.running == ''


def test_disabled_away_from_0188(tmp_path):
    instance = relay(tmp_path)
    instance.enabled = False
    with pytest.raises(ValueError):
        asyncio.run(instance.run('visitor', command(script='go')))


def test_go_reports_the_scripts_last_line(tmp_path):
    instance = relay(tmp_path, '#!/bin/sh\necho "reset: ramping back to the demo start pose"\n')

    async def scenario():
        assert (await instance.run('visitor', command(script='go')))['running'] == 'go'
        await instance.task
        return instance.status()

    status = asyncio.run(scenario())
    assert status['phase'] == 'done'
    assert status['exit'] == 0
    assert status['reason'] == 'reset: ramping back to the demo start pose'
    assert status['running'] == ''


def test_a_failing_script_is_reported_not_raised(tmp_path):
    instance = relay(tmp_path, '#!/bin/sh\necho "arms are still owned"\nexit 1\n')

    async def scenario():
        await instance.run('visitor', command(script='init'))
        await instance.task
        return instance.status()

    status = asyncio.run(scenario())
    assert status['phase'] == 'failed'
    assert status['exit'] == 1
    assert status['reason'] == 'arms are still owned'


def test_stop_stays_available_while_another_script_runs(tmp_path):
    instance = relay(tmp_path, '#!/bin/sh\nsleep 5\n')

    async def scenario():
        await instance.run('visitor', command(script='init'))
        with pytest.raises(ValueError):  # A second Go cannot start a competing attempt.
            await instance.run('visitor', command(script='go'))
        assert (await instance.run('visitor', command(script='stop')))['running'] == 'stop'
        await drain()

    asyncio.run(scenario())
