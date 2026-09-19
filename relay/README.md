# RealBot hackathon relay

Small in-memory WebSocket relay plus a simulated robot. This is a disposable
hackathon component, not a production device gateway.

```sh
cd relay
uv sync
uv run uvicorn app:app --reload --port 8000
```

In another terminal:

```sh
cd relay
uv run python simulator.py
```

The simulator joins room `demo-bot` by default. Configure it with
`RELAY_WS_URL` and `ROOM_ID`. Run tests with `uv run pytest`.

