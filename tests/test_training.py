import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import aiohttp
import aiosqlite
import pytest

from training import (
    BEIJING,
    ContestError,
    TrainingService,
    normalize_url,
    parse_abc_list,
    parse_contest,
    week_bounds,
)

from .conftest import Event


def abc_page(start=None):
    start = start if start is not None else week_bounds()[0] + 86400
    stamp = datetime.fromtimestamp(start, BEIJING).isoformat(" ")
    return (
        '<div id="contest-table-recent"><table><tbody><tr>'
        f'<td><time>{stamp}</time></td><td><a href="/contests/abc999">'
        "Sponsored AtCoder Beginner Contest 999</a></td><td>01:40</td>"
        '</tr><tr><td><a href="/contests/arc999">ARC</a></td></tr>'
        "</tbody></table></div>"
    )


def vjudge_page(start=None):
    start = start if start is not None else week_bounds()[0] + 172800
    return (
        '<textarea name="dataJson">'
        f'{{"title":"Training &amp; practice", "begin":{start * 1000},'
        f'"end":{(start + 7200) * 1000}}}</textarea>'
    )


async def rows(plugin, weekly=False):
    async with aiosqlite.connect(plugin.db_path) as db:
        return await TrainingService.list_contests(db, weekly)


async def command(plugin, event, action="", value=""):
    result = [text async for text in plugin.training_command(event, action, value)]
    assert event.stopped
    return result[0]


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/contest/1",
        "https://atcoder.jp.evil.example/contests/abc1",
        "https://user:password@atcoder.jp/contests/abc1",
        "file:///etc/passwd",
        "https://127.0.0.1/contest/1",
        "https://vjudge.net:444/contest/1",
        "https://vjudge.net/contest/1/tasks",
        "https://ac.nowcoder.com/acm/contest/0",
        "https://atcoder.jp/contests/%2e%2e",
        "https://vjudge.net/contest/-1",
    ],
)
def test_reject_invalid_urls(value):
    with pytest.raises(ContestError):
        normalize_url(value)


def test_url_canonicalization():
    assert normalize_url("http://ATCODER.JP/contests/ABC375/?lang=en#overview") == (
        "AtCoder",
        "https://atcoder.jp/contests/abc375",
    )
    assert normalize_url("https://vjudge.net/contest/600000#overview")[1] == (
        "https://vjudge.net/contest/600000"
    )


@pytest.mark.parametrize(
    ("source", "page", "title", "start"),
    [
        (
            "AtCoder",
            '<a class="contest-title">ABC 375</a>'
            '<small class="contest-duration"><time>2024-10-12 21:00:00+0900</time>'
            "<time>2024-10-12 22:40:00+0900</time></small>",
            "ABC 375",
            1728734400,
        ),
        ("VJudge", vjudge_page(1728734400), "Training & practice", 1728734400),
        (
            "牛客",
            '<script>window.pageInfo = {"name":"牛客周赛",'
            '"description":"{嵌套描述}","startTime":1728734400000,'
            '"endTime":1728741600000};</script>',
            "牛客周赛",
            1728734400,
        ),
    ],
)
def test_parse_platform_pages(source, page, title, start):
    item = parse_contest(source, "https://example.test", page)
    assert item["title"] == title
    assert item["start"] == start
    assert item["end"] > start


@pytest.mark.parametrize("source", ["AtCoder", "VJudge", "牛客"])
def test_private_or_changed_page_is_not_registered(source):
    with pytest.raises(ContestError):
        parse_contest(source, "https://example.test", "<title>Please log in</title>")


def test_reject_inverted_duration():
    with pytest.raises(ContestError):
        parse_contest(
            "VJudge",
            "https://vjudge.net/contest/1",
            (
                '<textarea name="dataJson">'
                '{"title":"Bad","begin":2000,"end":1000}</textarea>'
            ),
        )


def test_reject_missing_atcoder_timezone():
    with pytest.raises(ContestError):
        parse_contest(
            "AtCoder",
            "https://atcoder.jp/contests/abc999",
            '<a class="contest-title">ABC</a><small class="contest-duration">'
            "<time>2026-10-17 21:00:00</time><time>2026-10-17 22:40:00</time></small>",
        )


def test_beijing_week_boundary():
    sunday = datetime.fromisoformat("2026-10-11T15:59:59+00:00")
    monday = datetime.fromisoformat("2026-10-11T16:00:00+00:00")
    previous_begin, previous_end = week_bounds(sunday)
    current_begin, current_end = week_bounds(monday)
    assert previous_end == current_begin == int(monday.timestamp())
    assert previous_end - previous_begin == current_end - current_begin == 604800


def test_abc_includes_recent_and_excludes_arc():
    entries = parse_abc_list(abc_page())
    assert len(entries) == 1
    assert entries[0]["end"] - entries[0]["start"] == 6000
    with pytest.raises(ContestError):
        parse_abc_list("<title>Captcha</title>")


@pytest.mark.asyncio
async def test_permissions(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value=vjudge_page())
    )
    assert plugin.is_training_admin(Event(sender="other", admin=True))
    assert plugin.is_training_admin(Event(sender="100"))
    assert not plugin.is_training_admin(Event(sender="other"))
    assert "仅 AstrBot" in await command(
        plugin, Event(sender="other"), "添加", ("https://vjudge.net/contest/1")
    )
    assert "仅 AstrBot" in await command(plugin, Event(sender="other"), "删除")
    assert "仅 AstrBot" in await command(plugin, Event(sender="other"), "删除", "1")
    plugin.training.get_page.assert_not_called()
    plugin.config["plugin_admins"] = "100"
    assert not plugin.is_training_admin(Event())


@pytest.mark.asyncio
async def test_add_duplicate_and_restore(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value=vjudge_page())
    )
    assert "已添加" in await command(
        plugin, Event(), "添加", "https://vjudge.net/contest/1"
    )
    assert "已在周赛" in await command(
        plugin, Event(), "添加", ("http://vjudge.net/contest/1/#overview")
    )
    item = (await rows(plugin))[0]
    async with aiosqlite.connect(plugin.db_path) as db:
        assert await TrainingService.delete(db, item["id"], item["revision"])
    assert "已添加" in await command(
        plugin, Event(), "添加", "https://vjudge.net/contest/1"
    )
    restored = (await rows(plugin))[0]
    assert restored["id"] == item["id"]
    assert restored["revision"] > item["revision"]


@pytest.mark.asyncio
async def test_abc_deletion_survives_sync_and_reload(plugin, monkeypatch, plugin_class):
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    await command(plugin, Event(), "删除")
    assert (await rows(plugin))[0]["origin"] == "auto"
    assert "已删除" in await command(plugin, Event(), "删除", "1")
    async with aiosqlite.connect(plugin.db_path) as db:
        await plugin.training.sync_abc(db, 900)
    assert not await rows(plugin)
    reloaded = plugin_class(object(), {})
    await reloaded.initialize()
    try:
        async with aiosqlite.connect(reloaded.db_path) as db:
            await reloaded.training.sync_abc(db, 900)
        assert not await rows(reloaded)
    finally:
        await reloaded.terminate()
    monkeypatch.setattr(
        plugin.training,
        "get_page",
        AsyncMock(
            return_value=(
                '<a class="contest-title">ABC 999</a><small class="contest-duration">'
                "<time>2026-10-17 21:00:00+0900</time>"
                "<time>2026-10-17 22:40:00+0900</time>"
                "</small>"
            )
        ),
    )
    assert "已添加" in await command(
        plugin, Event(), "添加", "https://atcoder.jp/contests/abc999"
    )
    assert (await rows(plugin))[0]["origin"] == "manual"


@pytest.mark.asyncio
async def test_delete_scope_expiry_and_revoked_permission(plugin, monkeypatch):
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    plugin.config["plugin_admins"] = ["100", "200"]
    await command(plugin, Event(), "删除")
    assert "请先发送" in await command(plugin, Event(sender="200"), "删除", "1")
    assert "请先发送" in await command(
        plugin, Event(origin="qq:GroupMessage:2"), "删除", "1"
    )
    assert "编号无效" in await command(plugin, Event(), "删除", "0")
    assert "编号无效" in await command(plugin, Event(), "删除", "abc")
    key = (Event().unified_msg_origin, "100")
    created, entries = plugin.delete_selections[key]
    plugin.delete_selections[key] = (created - 301, entries)
    assert "请先发送" in await command(plugin, Event(), "删除", "1")
    await command(plugin, Event(), "删除")
    plugin.config["plugin_admins"] = []
    assert "仅 AstrBot" in await command(plugin, Event(), "删除", "1")
    assert len(await rows(plugin)) == 1


@pytest.mark.asyncio
async def test_concurrent_deletion_does_not_shift_numbers(plugin, monkeypatch):
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    plugin.config["plugin_admins"] = ["100", "200"]
    await command(plugin, Event(), "删除")
    await command(plugin, Event(sender="200"), "删除")
    results = await asyncio.gather(
        command(plugin, Event(), "删除", "1"),
        command(plugin, Event(sender="200"), "删除", "1"),
    )
    assert sum("已删除周赛" in text for text in results) == 1
    assert sum("信息发生变化" in text for text in results) == 1
    assert "请先发送" in await command(plugin, Event(), "删除", "1")


@pytest.mark.asyncio
async def test_readded_contest_cannot_be_deleted_with_old_selection(
    plugin, monkeypatch
):
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value=vjudge_page())
    )
    await command(plugin, Event(), "添加", "https://vjudge.net/contest/1")
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    await command(plugin, Event(), "删除")
    item = next(row for row in await rows(plugin) if row["source"] == "VJudge")
    selection = plugin.delete_selections[(Event().unified_msg_origin, "100")][1]
    number = next(i for i, row in enumerate(selection, 1) if row["id"] == item["id"])
    async with aiosqlite.connect(plugin.db_path) as db:
        await TrainingService.delete(db, item["id"], item["revision"])
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value=vjudge_page())
    )
    await command(plugin, Event(), "添加", "https://vjudge.net/contest/1")
    assert "信息发生变化" in await command(plugin, Event(), "删除", str(number))
    assert any(row["id"] == item["id"] for row in await rows(plugin))


@pytest.mark.asyncio
async def test_abc_postponed_to_next_week_leaves_current_week(plugin, monkeypatch):
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    await command(plugin, Event(), "删除")
    assert len(await rows(plugin, weekly=True)) == 1
    async with aiosqlite.connect(plugin.db_path) as db:
        await db.execute("UPDATE source_cache SET updated_at=0, attempted_at=0")
        await db.commit()
        monkeypatch.setattr(
            plugin.training,
            "get_page",
            AsyncMock(return_value=(abc_page(week_bounds()[1]))),
        )
        await plugin.training.sync_abc(db, 900)
    assert not await rows(plugin, weekly=True)
    assert len(await rows(plugin)) == 1


@pytest.mark.asyncio
async def test_week_filter_and_manual_contests_without_abc_network(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value=vjudge_page())
    )
    await command(plugin, Event(), "添加", "https://vjudge.net/contest/1")
    monkeypatch.setattr(
        plugin.training,
        "get_page",
        AsyncMock(return_value=(vjudge_page(week_bounds()[1]))),
    )
    assert "不在本周" in await command(
        plugin, Event(), "添加", "https://vjudge.net/contest/2"
    )
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(side_effect=aiohttp.ClientError)
    )
    text = [text async for text in plugin.weekly_contests(Event(sender="member"))][0]
    assert "自动 ABC 同步暂不可用" in text
    assert "https://vjudge.net/contest/1" in text
    assert "https://vjudge.net/contest/2" not in text
    assert len(await rows(plugin, weekly=True)) == 1
    assert len(await rows(plugin)) == 2
    await command(plugin, Event(), "删除")
    plugin.training.get_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_cache_fallback_and_existing_cache_migration(plugin, monkeypatch):
    monkeypatch.setattr(plugin.training, "get_page", AsyncMock(return_value=abc_page()))
    await command(plugin, Event(), "删除")
    async with aiosqlite.connect(plugin.db_path) as db:
        await db.execute(
            "UPDATE source_cache SET updated_at=updated_at-1000, attempted_at=0"
        )
        await db.execute("INSERT INTO source_cache VALUES ('Codeforces:7', '[]', 1, 1)")
        await db.commit()
        await TrainingService.initialize(db)
        monkeypatch.setattr(
            plugin.training, "get_page", AsyncMock(side_effect=asyncio.TimeoutError)
        )
        assert "旧缓存" in await plugin.training.sync_abc(db, 900)
        async with db.execute(
            "SELECT payload FROM source_cache WHERE source='Codeforces:7'"
        ) as cur:
            assert await cur.fetchone() == ("[]",)
    assert len(await rows(plugin)) == 1


@pytest.mark.asyncio
async def test_bad_fetch_does_not_write_incomplete_contest(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(return_value="<title>Login</title>")
    )
    assert "无法读取" in await command(
        plugin, Event(), "添加", "https://vjudge.net/contest/1"
    )
    assert not await rows(plugin)
    monkeypatch.setattr(
        plugin.training, "get_page", AsyncMock(side_effect=asyncio.TimeoutError)
    )
    assert "请求失败或超时" in await command(
        plugin, Event(), "添加", "https://vjudge.net/contest/1"
    )
    assert not await rows(plugin)
