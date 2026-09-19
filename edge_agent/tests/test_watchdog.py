import asyncio

from edge_agent.watchdog import ControlLeaseWatchdog


def test_heartbeat_expiry_stops_once() -> None:
    async def scenario() -> None:
        reasons: list[str] = []
        expired = asyncio.Event()

        async def on_expire(reason: str) -> None:
            reasons.append(reason)
            expired.set()

        watchdog = ControlLeaseWatchdog(on_expire, timeout_s=0.03, check_interval_s=0.005)
        await watchdog.start()
        watchdog.beat()
        await asyncio.wait_for(expired.wait(), 0.2)
        await asyncio.sleep(0.04)
        await watchdog.close()
        assert reasons == ["heartbeat_expired"]

    asyncio.run(scenario())


def test_explicit_revoke_is_immediate() -> None:
    async def scenario() -> None:
        reasons: list[str] = []
        watchdog = ControlLeaseWatchdog(reasons.append)
        watchdog.beat()
        await watchdog.revoke("controller_disconnected")
        assert reasons == ["controller_disconnected"]

    asyncio.run(scenario())
