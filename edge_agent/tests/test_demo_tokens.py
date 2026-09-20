import json
import time

import jwt
import pytest

from edge_agent.demo_tokens import generate, write_configs
from edge_agent.demo_session import DemoConfig

KEY = "local-test-key"
SECRET = "local-test-secret-not-real-credentials-1234567890"


def pair(**kwargs):
    return generate(url="wss://test.invalid", api_key=KEY, api_secret=SECRET, **kwargs)


def decode(token):
    return jwt.decode(token, SECRET, algorithms=["HS256"], issuer=KEY)


@pytest.mark.parametrize("driving", [False, True])
def test_pair_has_distinct_identities_same_room_and_minimal_grants(driving):
    robot, browser = pair(driving=driving)
    r, b = decode(robot["token"]), decode(browser["token"])
    assert r["sub"] != b["sub"] == robot["controllerIdentity"]
    assert r["video"]["room"] == b["video"]["room"] == robot["roomId"]
    assert r["video"]["canPublishSources"] == ["camera"]
    assert r["video"]["canSubscribe"] is True
    assert b["video"]["canPublish"] is False
    assert b["video"]["canPublishData"] is driving
    assert b["video"]["canSubscribe"] is True
    assert all(not r["video"].get(k) and not b["video"].get(k) for k in ("roomAdmin", "roomCreate", "roomList", "roomRecord"))
    assert DemoConfig.parse(robot).mode == ("drive" if driving else "camera")
    assert SECRET not in json.dumps([robot, browser])
    assert robot["token"] not in repr(DemoConfig.parse(robot))
    assert pair()[0]["roomId"] != robot["roomId"]


@pytest.mark.parametrize("minutes", [0, 61, True])
def test_rejects_unbounded_lifetime(minutes):
    with pytest.raises(ValueError): pair(minutes=minutes)


def test_robot_rejects_wrong_room_controller_token_expired_and_bad_permissions():
    robot, browser = pair()
    for changes in ({"roomId": "other"}, {"token": browser["token"]}, {"expiresAt": int(time.time())-1},
                    {"token": "not-a-token"}, {"url": "ws://public.example"}, {"mode": "anything"}):
        with pytest.raises(ValueError): DemoConfig.parse({**robot, **changes})
    claims = decode(robot["token"])
    claims["video"]["roomAdmin"] = True
    robot["token"] = jwt.encode(claims, SECRET, algorithm="HS256")
    with pytest.raises(ValueError): DemoConfig.parse(robot)


def test_private_files_are_never_overwritten(tmp_path):
    robot, browser = pair()
    target = tmp_path / "demo"
    write_configs(target, robot, browser)
    assert json.loads((target / "robot.session.json").read_text()) == robot
    assert json.loads((target / "browser.session.json").read_text()) == browser
    with pytest.raises(FileExistsError): write_configs(target, robot, browser)
