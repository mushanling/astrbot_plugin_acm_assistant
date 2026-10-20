import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import aiohttp
import aiosqlite
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .training import ContestError, TrainingService, format_contest, week_bounds

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
SOURCES = ("AtCoder", "Codeforces", "牛客")


class AcmAssistant(Star):
    """Provide upcoming contest information with a persistent per-source cache."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """Store plugin settings and resources.

        Args:
            context: AstrBot plugin context.
            config: Plugin settings supplied by AstrBot.
        """
        super().__init__(context)
        self.config = config
        self.db_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_acm_assistant"
            / "contests.sqlite3"
        )
        self.lock = asyncio.Lock()
        self.session: aiohttp.ClientSession | None = None
        self.training: TrainingService | None = None
        # A numbered deletion list belongs to one sender in one conversation.
        self.delete_selections: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    async def initialize(self) -> None:
        """Create the persistent cache and HTTP session.

        Raises:
            OSError: If the plugin data directory cannot be created.
            aiosqlite.Error: If the cache cannot be initialized.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS source_cache ("
                "source TEXT PRIMARY KEY, payload TEXT, updated_at REAL NOT NULL, "
                "attempted_at REAL NOT NULL)"
            )
            await TrainingService.initialize(db)
            await db.commit()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "AstrBot-ACM-Assistant/0.2.3"},
            trust_env=True,
        )
        self.training = TrainingService(self.session)

    async def fetch_contests(self, source: str, days: int) -> list[dict]:
        """Fetch and normalize one platform's contests without touching the cache.

        Args:
            source: Platform name from SOURCES.
            days: Number of upcoming days requested by the plugin settings.

        Returns:
            Contest dictionaries containing title, start, end, and url.

        Raises:
            RuntimeError: If the session or upstream response is invalid.
            aiohttp.ClientError: If the HTTP request fails.
            asyncio.TimeoutError: If the upstream request times out.
            ValueError: If upstream contest fields cannot be parsed.
        """
        if self.session is None:
            raise RuntimeError("Plugin HTTP session is not initialized")
        contests = []
        now = datetime.now(UTC)
        if source == "Codeforces":
            async with self.session.get(
                "https://codeforces.com/api/contest.list", params={"gym": "false"}
            ) as response:
                response.raise_for_status()
                data = await response.json()
            if data.get("status") != "OK" or not isinstance(data.get("result"), list):
                raise RuntimeError("Invalid Codeforces API response")
            for item in data["result"]:
                if item["phase"] != "BEFORE" or item.get("startTimeSeconds") is None:
                    continue
                start = int(item["startTimeSeconds"])
                duration = int(item["durationSeconds"])
                if duration <= 0:
                    raise ValueError("Invalid Codeforces contest duration")
                contests.append(
                    {
                        "title": " ".join(str(item["name"]).split()),
                        "start": start,
                        "end": start + duration,
                        "url": f"https://codeforces.com/contest/{int(item['id'])}",
                    }
                )
        else:
            host = "atcoder.jp" if source == "AtCoder" else "ac.nowcoder.com"
            params = {
                "resource": host,
                "start__gte": now.strftime("%Y-%m-%dT%H:%M:%S"),
                # Extra coverage prevents the rolling display window from
                # extending past the fetched window while the cache is fresh.
                "start__lte": (now + timedelta(days=days + 1)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                # CLIST appends id for stable pagination. Tastypie treats a
                # comma-separated value as one invalid field name.
                "order_by": "start",
                "limit": 100,
                "offset": 0,
                "format": "json",
                "format_time": "false",
            }
            headers = {
                "Authorization": (
                    f"ApiKey {str(self.config.get('clist_username', '')).strip()}:"
                    f"{str(self.config.get('clist_api_key', '')).strip()}"
                )
            }
            # Keep pagination on a fixed endpoint; never forward credentials to
            # a URL supplied by an upstream response.
            while True:
                async with self.session.get(
                    "https://clist.by/api/v4/contest/",
                    params=params,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "CLIST request failed for %s (HTTP %s)",
                            source,
                            response.status,
                        )
                        raise RuntimeError(f"CLIST returned HTTP {response.status}")
                    data = await response.json()
                if not isinstance(data.get("objects"), list):
                    raise RuntimeError("Invalid CLIST API response")
                for item in data["objects"]:
                    times = []
                    for field in ("start", "end"):
                        value = datetime.fromisoformat(
                            item[field].replace("Z", "+00:00")
                        )
                        # CLIST's unformatted timestamps are UTC, including
                        # responses that omit an explicit timezone suffix.
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=UTC)
                        times.append(int(value.timestamp()))
                    if times[1] <= times[0]:
                        raise ValueError("Invalid CLIST contest duration")
                    url = str(item["href"])
                    if not url.startswith(f"https://{host}/"):
                        raise ValueError("Unexpected CLIST contest URL")
                    contests.append(
                        {
                            "title": " ".join(str(item["event"]).split()),
                            "start": times[0],
                            "end": times[1],
                            "url": url,
                        }
                    )
                if not data.get("meta", {}).get("next"):
                    break
                params["offset"] += len(data["objects"])
                if not data["objects"] or params["offset"] >= 1000:
                    raise RuntimeError("CLIST pagination exceeded the safety limit")
        return contests

    async def cached_contests(
        self, db: aiosqlite.Connection, source: str, days: int, ttl: int
    ) -> tuple[list[dict], str | None]:
        """Read or refresh one source, preserving successful data on failure.

        Args:
            db: Open SQLite connection, protected by the command lock.
            source: Platform name from SOURCES.
            days: Upcoming display window in days.
            ttl: Cache lifetime in seconds.

        Returns:
            Cached contests and an optional user-facing availability notice.
        """
        if source != "Codeforces" and (
            not str(self.config.get("clist_username", "")).strip()
            or not str(self.config.get("clist_api_key", "")).strip()
        ):
            return [], f"{source}：未配置 CLIST 用户名/API Key，暂不可用。"
        # Include the query window in the key so configuration changes cannot
        # accidentally reuse a narrower snapshot.
        key = f"{source}:{days}"
        async with db.execute(
            "SELECT payload, updated_at, attempted_at "
            "FROM source_cache WHERE source = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        now = datetime.now(UTC).timestamp()
        cached = None
        updated_at = 0
        attempted_at = 0
        if row:
            updated_at, attempted_at = row[1:]
            try:
                cached = json.loads(row[0]) if row[0] is not None else None
                if cached is not None and not isinstance(cached, list):
                    cached = None
            except (ValueError, TypeError):
                cached = None
        if cached is not None and 0 <= now - updated_at < ttl:
            return cached, None
        if now - attempted_at >= 60:
            try:
                contests = await self.fetch_contests(source, days)
            except Exception as exc:
                # Exception messages and response bodies may contain API keys.
                logger.warning(
                    "Contest refresh failed for %s (%s)", source, type(exc).__name__
                )
                await db.execute(
                    "INSERT INTO source_cache VALUES (?, NULL, 0, ?) "
                    "ON CONFLICT(source) DO UPDATE SET "
                    "attempted_at = excluded.attempted_at",
                    (key, now),
                )
                await db.commit()
            else:
                updated_at = datetime.now(UTC).timestamp()
                await db.execute(
                    "INSERT INTO source_cache VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(source) DO UPDATE SET payload = excluded.payload, "
                    "updated_at = excluded.updated_at, "
                    "attempted_at = excluded.attempted_at",
                    (
                        key,
                        json.dumps(contests, ensure_ascii=False),
                        updated_at,
                        updated_at,
                    ),
                )
                await db.commit()
                return contests, None
        if cached is not None and 0 <= now - updated_at <= 86400:
            stamp = datetime.fromtimestamp(updated_at, BEIJING).strftime("%m-%d %H:%M")
            return cached, f"{source}：刷新失败，使用 {stamp} 的旧缓存。"
        return (
            [],
            f"{source}：暂不可用，无有效缓存；"
            "请检查网络、API 配额或凭据（60 秒后可重试）。",
        )

    @filter.command("acmhelp")
    async def help_menu(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Show the plugin menu through its dedicated command.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text menu of public and administrator commands.
        """
        event.stop_event()
        yield event.plain_result(
            "AstrBot ACM Assistant｜帮助\n\n"
            "比赛查询\n"
            "/最近比赛 — 所有平台，未来 3 天\n"
            "/牛客 或 /牛客比赛 — 牛客，未来 5 天\n"
            "/atc 或 /atc比赛 — AtCoder，未来 5 天\n"
            "/cf 或 /cf比赛 — Codeforces，未来 5 天\n\n"
            "周赛查询\n"
            "/本周周赛 — 查看本周自动收录的 ABC 和手动登记比赛\n\n"
            "周赛管理（仅 AstrBot 超级管理员或插件白名单管理员）\n"
            "/周赛 添加 <链接> — 登记 AtCoder、VJudge 或牛客比赛\n"
            "/周赛 删除 — 获取可删除比赛及编号\n"
            "/周赛 删除 <编号> — 删除对应比赛，编号有效期 5 分钟\n\n"
            "/acmhelp — 查看本菜单"
        )

    @filter.command("最近比赛")
    async def recent_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Reply with all platforms' contests in the next three days.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text contest list or an actionable error message.
        """
        event.stop_event()
        yield event.plain_result(await self.contest_message(SOURCES, 3))

    @filter.command("牛客比赛", alias={"牛客"})
    async def nowcoder_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Reply with Nowcoder contests in the next five days.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text contest list or an actionable error message.
        """
        event.stop_event()
        yield event.plain_result(await self.contest_message(("牛客",), 5))

    @filter.command("atc比赛", alias={"atc"})
    async def atcoder_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Reply with AtCoder contests in the next five days.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text contest list or an actionable error message.
        """
        event.stop_event()
        yield event.plain_result(await self.contest_message(("AtCoder",), 5))

    @filter.command("cf比赛", alias={"cf"})
    async def codeforces_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Reply with Codeforces contests in the next five days.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text contest list or an actionable error message.
        """
        event.stop_event()
        yield event.plain_result(await self.contest_message(("Codeforces",), 5))

    async def contest_message(self, sources: tuple[str, ...], days: int) -> str:
        """Build the contest reply shared by the four command handlers.

        Args:
            sources: Platforms to query, without fetching unrelated sources.
            days: Upcoming display window in days.

        Returns:
            Formatted contest information or an actionable error message.
        """
        try:
            limit = max(1, min(20, int(self.config.get("max_contests", 10))))
            ttl = max(300, min(3600, int(self.config.get("cache_ttl_seconds", 900))))
            contests, notices = [], []
            async with self.lock, aiosqlite.connect(self.db_path) as db:
                for source in sources:
                    entries, notice = await self.cached_contests(db, source, days, ttl)
                    contests.extend({**entry, "source": source} for entry in entries)
                    if notice:
                        notices.append(notice)
            now = datetime.now(UTC)
            end = (now + timedelta(days=days)).timestamp()
            unique = {
                (item["source"], item["url"]): item
                for item in contests
                if now.timestamp() <= item["start"] <= end
            }
            upcoming = sorted(
                unique.values(),
                key=lambda item: (item["start"], item["source"], item["url"]),
            )
            title = "最近比赛" if len(sources) > 1 else f"{sources[0]}比赛"
            lines = [f"{title}｜未来 {days} 天"]
            for index, item in enumerate(upcoming[:limit], 1):
                start = datetime.fromtimestamp(item["start"], BEIJING).strftime(
                    "%m-%d %H:%M"
                )
                minutes = (item["end"] - item["start"]) // 60
                hours, minutes = divmod(minutes, 60)
                duration = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
                lines.append(
                    f"{index}. [{item['source']}] {item['title']}\n"
                    f"{start}｜时长 {duration}\n{item['url']}"
                )
            if not upcoming:
                lines.append(
                    "可用数据中暂无符合条件的比赛。"
                    if notices
                    else "暂无即将开始的比赛。"
                )
            if len(upcoming) > limit:
                lines.append(f"仅显示最早的 {limit} 场比赛。")
            if notices:
                lines.append("数据提示：\n" + "\n".join(notices))
            message = "\n\n".join(lines)
        except Exception as exc:
            logger.error("Upcoming contest command failed (%s)", type(exc).__name__)
            message = "比赛查询失败，请管理员检查插件配置和缓存目录读写权限。"
        return message

    def is_training_admin(self, event: AstrMessageEvent) -> bool:
        """Allow AstrBot administrators or explicitly configured plugin admins."""
        if event.is_admin():
            return True
        sender = str(event.get_sender_id())
        whitelist = self.config.get("plugin_admins", [])
        if not isinstance(whitelist, list) or not sender:
            return False
        return sender in {str(value).strip() for value in whitelist}

    @filter.command("本周周赛")
    async def weekly_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Show automatic ABCs and registered contests starting this calendar week."""
        event.stop_event()
        try:
            if self.training is None:
                raise RuntimeError("Training service is not initialized")
            ttl = max(300, min(3600, int(self.config.get("cache_ttl_seconds", 900))))
            async with self.lock, aiosqlite.connect(self.db_path) as db:
                notice = await self.training.sync_abc(db, ttl)
                entries = await self.training.list_contests(db, weekly=True)
            begin, end = week_bounds()
            monday = datetime.fromtimestamp(begin, BEIJING).strftime("%m-%d")
            sunday = datetime.fromtimestamp(end - 1, BEIJING).strftime("%m-%d")
            lines = [f"本周周赛｜{monday} ～ {sunday}"]
            for index, item in enumerate(entries, 1):
                lines.append(f"{index}. {format_contest(item)}")
            if not entries:
                lines.append("暂无已登记周赛。")
            if notice:
                lines.append(notice)
            message = "\n\n".join(lines)
        except Exception as exc:
            logger.error("Weekly contest query failed (%s)", type(exc).__name__)
            message = "周赛查询失败，请管理员检查网络及数据库目录读写权限。"
        yield event.plain_result(message)

    async def manage_training(
        self, event: AstrMessageEvent, action: str, value: str
    ) -> str:
        """Apply an authorized management command under the shared database lock."""
        if self.training is None:
            raise RuntimeError("Training service is not initialized")
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        now = monotonic()
        self.delete_selections = {
            owner: selection
            for owner, selection in self.delete_selections.items()
            if now - selection[0] < 300
        }
        if action == "添加" and value:
            async with self.lock, aiosqlite.connect(self.db_path) as db:
                item = await self.training.add(db, value, key[1])
            begin, end = week_bounds()
            suffix = (
                ""
                if begin <= item["start"] < end
                else "\n该比赛不在本周，按开始时间归属对应自然周。"
            )
            return f"已添加周赛：\n{format_contest(item)}{suffix}"
        if action != "删除":
            return "用法：\n/周赛 添加 <链接>\n/周赛 删除\n/周赛 删除 <编号>"
        async with self.lock, aiosqlite.connect(self.db_path) as db:
            if value:
                selection = self.delete_selections.get(key)
                if selection is None or monotonic() - selection[0] >= 300:
                    self.delete_selections.pop(key, None)
                    return "请先发送 /周赛 删除 获取编号（有效期 5 分钟）。"
                if not value.isascii() or not value.isdecimal() or len(value) > 9:
                    return "编号无效，请使用刚刚列出的比赛编号。"
                number = int(value)
                if not 1 <= number <= len(selection[1]):
                    return "编号无效，请使用刚刚列出的比赛编号。"
                item = selection[1][number - 1]
                changed = await self.training.delete(db, item["id"], item["revision"])
                self.delete_selections.pop(key, None)
                if not changed:
                    return "该比赛已被删除或信息发生变化，请重新发送 /周赛 删除。"
                return f"已删除周赛：{item['title']}\n{item['url']}"
            ttl = max(300, min(3600, int(self.config.get("cache_ttl_seconds", 900))))
            notice = await self.training.sync_abc(db, ttl)
            entries = await self.training.list_contests(db)
            self.delete_selections.pop(key, None)
            if not entries:
                return "暂无可删除的周赛。" + (f"\n{notice}" if notice else "")
            # Bound transient selection memory even if many admins request lists.
            if len(self.delete_selections) >= 1000:
                oldest = min(
                    self.delete_selections, key=lambda k: self.delete_selections[k][0]
                )
                self.delete_selections.pop(oldest)
            self.delete_selections[key] = (monotonic(), entries)
        lines = ["可删除周赛｜编号 5 分钟内有效（含其他周已登记比赛）"]
        for index, item in enumerate(entries, 1):
            lines.append(f"{index}. {format_contest(item)}")
        lines.append("发送 /周赛 删除 <编号> 删除对应比赛；每次删除后需重新获取列表。")
        if notice:
            lines.append(notice)
        return "\n\n".join(lines)

    @filter.command("周赛")
    async def training_command(
        self, event: AstrMessageEvent, action: str = "", value: str = ""
    ) -> AsyncIterator[MessageEventResult]:
        """Manage contests using AstrBot admins plus the plugin's own whitelist."""
        event.stop_event()
        if not self.is_training_admin(event):
            yield event.plain_result(
                "仅 AstrBot 超级管理员或插件白名单管理员可管理周赛。"
            )
            return
        try:
            message = await self.manage_training(event, action, value)
        except ContestError as exc:
            message = str(exc)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            message = "比赛页面请求失败或超时，请稍后重试；未添加比赛。"
        except Exception as exc:
            logger.error("Training contest command failed (%s)", type(exc).__name__)
            message = "周赛操作失败，请管理员检查网络及数据库目录读写权限。"
        yield event.plain_result(message)

    async def terminate(self) -> None:
        """Close HTTP resources when the plugin is disabled or reloaded."""
        async with self.lock:
            self.delete_selections.clear()
            self.training = None
            if self.session is not None:
                await self.session.close()
                self.session = None
