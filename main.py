import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import aiosqlite
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

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
            await db.commit()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "AstrBot-ACM-Assistant/0.1.0"},
            trust_env=True,
        )

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
                "order_by": "start,id",
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

    @filter.command("最近比赛")
    async def recent_contests(
        self, event: AstrMessageEvent
    ) -> AsyncIterator[MessageEventResult]:
        """Reply with upcoming contests ordered by start time.

        Args:
            event: Incoming AstrBot command event.

        Yields:
            A plain-text contest list or an actionable error message.
        """
        event.stop_event()
        try:
            days = max(1, min(30, int(self.config.get("upcoming_days", 7))))
            limit = max(1, min(20, int(self.config.get("max_contests", 10))))
            ttl = max(300, min(3600, int(self.config.get("cache_ttl_seconds", 900))))
            contests, notices = [], []
            async with self.lock, aiosqlite.connect(self.db_path) as db:
                for source in SOURCES:
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
            lines = [f"最近比赛｜未来 {days} 天｜北京时间 UTC+8"]
            for index, item in enumerate(upcoming[:limit], 1):
                start = datetime.fromtimestamp(item["start"], BEIJING).strftime(
                    "%m-%d %H:%M"
                )
                minutes = (item["end"] - item["start"]) // 60
                hours, minutes = divmod(minutes, 60)
                duration = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
                lines.append(
                    f"\n{index}. [{item['source']}] {item['title']}\n"
                    f"{start}｜时长 {duration}\n{item['url']}"
                )
            if not upcoming:
                lines.append(
                    "可用数据中暂无符合条件的比赛。"
                    if notices
                    else "暂无即将开始的比赛。"
                )
            if len(upcoming) > limit:
                lines.append(f"\n仅显示最早的 {limit} 场比赛。")
            if notices:
                lines.append("\n数据提示：\n" + "\n".join(notices))
            lines.append("\n数据来源：Codeforces 官方 API；AtCoder / 牛客：CLIST。")
            message = "\n".join(lines)
        except Exception as exc:
            logger.error("Upcoming contest command failed (%s)", type(exc).__name__)
            message = "比赛查询失败，请管理员检查插件配置和缓存目录读写权限。"
        yield event.plain_result(message)

    async def terminate(self) -> None:
        """Close HTTP resources when the plugin is disabled or reloaded."""
        async with self.lock:
            if self.session is not None:
                await self.session.close()
                self.session = None
