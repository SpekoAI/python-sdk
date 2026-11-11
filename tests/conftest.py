import json

import pytest

from spekoai import AsyncSpeko, Speko

BASE = "https://api.test"


@pytest.fixture
def speko():
    client = Speko(api_key="sk-test", base_url=BASE)
    yield client
    client.close()


@pytest.fixture
async def aspeko():
    client = AsyncSpeko(api_key="sk-test", base_url=BASE)
    yield client
    await client.close()


def sse(*events: tuple) -> str:
    """Render (event_name, payload) pairs as an SSE body."""
    blocks = []
    for name, payload in events:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        blocks.append(f"event: {name}\ndata: {data}\n\n")
    return "".join(blocks)
