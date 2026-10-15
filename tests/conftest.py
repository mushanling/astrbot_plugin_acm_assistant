"""Minimal AstrBot boundary doubles; no bot or messaging service is started."""

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import pytest_asyncio

import training


class Event:
    def __init__(self, sender="100", origin="qq:GroupMessage:1", admin=False):
        self.sender = sender
        self.unified_msg_origin = origin
        self.admin = admin
        self.stopped = False

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    modules = {
        name: ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
            "astrbot.core",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
            "acm_test",
        )
    }

    class Star:
        def __init__(self, context):
            self.context = context

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("acm_test")
    modules["astrbot.api.event"].AstrMessageEvent = Event
    modules["astrbot.api.event"].MessageEventResult = str
    modules["astrbot.api.event"].filter = SimpleNamespace(
        command=lambda name: lambda handler: handler
    )
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: str(
        tmp_path
    )
    modules["acm_test"].__path__ = [str(root)]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "acm_test.training", training)
    spec = importlib.util.spec_from_file_location("acm_test.main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "acm_test.main", module)
    spec.loader.exec_module(module)
    return module.AcmAssistant


@pytest_asyncio.fixture
async def plugin(plugin_class):
    instance = plugin_class(object(), {"plugin_admins": ["100"]})
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.terminate()
