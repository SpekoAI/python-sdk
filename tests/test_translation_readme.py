"""The README's translation snippet, run as written against the mock server."""

import asyncio
import re
from functools import partial
from pathlib import Path

import spekoai

from .translation_mock import MockTranslationServer

README = Path(__file__).parent.parent / "README.md"


def snippet() -> str:
    section = README.read_text(encoding="utf-8").split("## Live translation", 1)[1]
    found = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert found is not None
    return found.group(1)


async def test_the_translation_snippet_runs_as_written(monkeypatch, capsys):
    monkeypatch.setenv("SPEKO_API_KEY", "sk-test")
    options = {"ready_delay": 0.01, "echo_delay": 0.05, "reconnect_every": None}
    async with MockTranslationServer(**options) as server:
        client_to_mock = partial(spekoai.AsyncSpeko, translation_base_url=server.base_url)
        monkeypatch.setattr(spekoai, "AsyncSpeko", client_to_mock)
        code = compile(snippet(), "README.md", "exec")
        # asyncio.run() in the snippet needs a thread with no running loop.
        await asyncio.wait_for(asyncio.to_thread(exec, code, {"__name__": "__main__"}), 20)

        assert server.session().end_reason == "finished"
        assert server.session().test is True

    assert "Доброе утро всем." in capsys.readouterr().out
