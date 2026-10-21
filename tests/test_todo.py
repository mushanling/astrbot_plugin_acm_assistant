import asyncio
import re

import aiosqlite
import pytest

from todo import TodoError, normalize_problem_url

from .conftest import Event

URL = "https://codeforces.com/problemset/problem/1/A"


def private(sender="100", platform="qq"):
    return Event(sender, f"{platform}:FriendMessage:{sender}")


async def command(plugin, event, action="", value=""):
    replies = [reply async for reply in plugin.todo_command(event, action, value)]
    assert event.stopped
    assert len(replies) == 1
    return replies[0]


def number(reply):
    return re.search(r"编号 (\d+)", reply)[1]


@pytest.mark.asyncio
async def test_lifecycle_and_restart(plugin, plugin_class):
    event = private()
    assert "没有待办" in await command(plugin, event)
    first = number(await command(plugin, event, URL))
    assert "已在待办" in await command(plugin, event, URL)
    second = number(await command(plugin, event, "https://luogu.com.cn/problem/P1001"))
    assert "已完成" in await command(plugin, event, "完成", first)
    pending = await command(plugin, event)
    assert URL not in pending and f"{second}." in pending
    assert "重新加入" in await command(plugin, event, URL)
    assert f"{first}. {URL}" in await command(plugin, event)
    await plugin.terminate()
    restarted = plugin_class(object(), {})
    await restarted.initialize()
    try:
        assert URL in await command(restarted, event)
        assert "已删除" in await command(restarted, event, "删除", first)
        assert URL not in await command(restarted, event)
        assert "没有该编号" in await command(restarted, event, "完成", first)
        assert "已添加" in await command(restarted, event, URL)
    finally:
        await restarted.terminate()


@pytest.mark.asyncio
async def test_owner_isolation_and_private_only(plugin):
    alice = private()
    first = number(await command(plugin, alice, URL))
    for event in [private("200"), private("100", "telegram")]:
        assert URL not in await command(plugin, event)
        for action in ["完成", "删除"]:
            assert "没有该编号" in await command(plugin, event, action, first)
        assert "已添加" in await command(plugin, event, URL)
    for action, value in [("", ""), (URL, ""), ("完成", first), ("删除", first)]:
        assert "仅在私聊" in await command(plugin, Event(admin=True), action, value)
    assert URL in await command(plugin, alice)
    assert "无法识别" in await command(plugin, private(""), URL)


@pytest.mark.asyncio
async def test_concurrent_duplicate_add_and_completion(plugin):
    results = await asyncio.gather(*(command(plugin, private(), URL) for _ in range(5)))
    assert sum("已添加" in reply for reply in results) == 1
    first = number(results[0])
    results = await asyncio.gather(
        *(command(plugin, private(), "完成", first) for _ in range(2))
    )
    assert sum("已完成" in reply for reply in results) == 1
    assert "没有待办" in await command(plugin, private())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "0", "-1", "1.0", "１", "9" * 100, "1 2"])
async def test_invalid_numbers(plugin, value):
    assert "编号无效" in await command(plugin, private(), "完成", value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "https://user:pass@example.com/1",
        "https://example.com:invalid/1",
        "https://example.com/\n1",
        "https://example.com/ 1",
        "https://[bad/1",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_invalid_links(value):
    with pytest.raises(TodoError):
        normalize_problem_url(value)


def test_url_identity():
    assert (
        normalize_problem_url("HTTPS://Codeforces.COM:443/problemset/problem/1/A")
        == URL
    )
    url = "https://vjudge.net/contest/600000#problem/A"
    assert normalize_problem_url(url) == url
    url = "https://example.com/problem?id=123&lang=zh"
    assert normalize_problem_url(url) == url


@pytest.mark.asyncio
async def test_completed_rows_stay_hidden_and_can_reopen(plugin):
    first = number(await command(plugin, private(), URL))
    await command(plugin, private(), "完成", first)
    assert "没有该编号" in await command(plugin, private(), "删除", first)
    assert "没有待办" in await command(plugin, private())
    assert "重新加入" in await command(plugin, private(), URL)
    async with aiosqlite.connect(plugin.db_path) as db:
        async with db.execute("SELECT count(*) FROM todo_items") as cursor:
            assert await cursor.fetchone() == (1,)


@pytest.mark.asyncio
async def test_help_and_bad_command(plugin):
    reply = await command(plugin, private(), URL, "extra")
    assert "用法" in reply
    assert "有效" in await command(plugin, private(), "garbage")
    help_text = "".join([text async for text in plugin.help_menu(private())])
    assert "/todo 完成 <编号>" in help_text
