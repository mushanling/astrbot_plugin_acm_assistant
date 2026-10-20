"""Fetch contest metadata and persist the shared weekly training schedule."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
MAX_PAGE_BYTES = 2 * 1024 * 1024


class ContestError(ValueError):
    """A safe, actionable error that can be shown in a command response."""


def week_bounds(now: datetime | None = None) -> tuple[int, int]:
    """Return this Beijing calendar week's inclusive start and exclusive end."""
    local = (now or datetime.now(UTC)).astimezone(BEIJING)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(monday.timestamp()), int((monday + timedelta(days=7)).timestamp())


def normalize_url(value: str) -> tuple[str, str]:
    """Accept only supported contest paths and return a fixed HTTPS destination."""
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"https", "http"}
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
        ):
            raise ValueError
        routes = {
            "atcoder.jp": ("AtCoder", r"/contests/([a-zA-Z0-9_-]{1,100})/?"),
            "vjudge.net": ("VJudge", r"/contest/([1-9]\d{0,11})/?"),
            "ac.nowcoder.com": ("牛客", r"/acm/contest/([1-9]\d{0,11})/?"),
        }
        source, pattern = routes[parts.hostname]
        match = re.fullmatch(pattern, parts.path)
        if match is None:
            raise ValueError
    except (KeyError, ValueError):
        raise ContestError(
            "请提供 AtCoder、VJudge 或牛客的比赛主页链接，例如：\n"
            "https://atcoder.jp/contests/abc480\n"
            "https://vjudge.net/contest/600000\n"
            "https://ac.nowcoder.com/acm/contest/95323"
        ) from None
    path = parts.path.rstrip("/")
    if source == "AtCoder":
        path = path.lower()
    return source, f"https://{parts.hostname}{path}"


def contest_data(source: str, url: str, title: str, start: int, end: int) -> dict:
    """Validate normalized metadata before it reaches the database."""
    title = " ".join(str(title).split())[:300]
    if not title or not 0 < start < end < 253402300800:
        raise ContestError(
            "未能读取有效比赛信息；请检查比赛是否公开、起止时间是否已公布。"
        )
    return {"source": source, "url": url, "title": title, "start": start, "end": end}


def iso_timestamp(value: str) -> int:
    """Require an explicit offset instead of depending on the bot host's timezone."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Missing contest timezone")
    return int(parsed.timestamp())


def parse_contest(source: str, url: str, page: str) -> dict:
    """Read platform metadata without executing JavaScript or guessing times."""
    soup = BeautifulSoup(page, "html.parser")
    try:
        if source == "AtCoder":
            title = soup.select_one(".contest-title").get_text(" ", strip=True)
            times = soup.select(".contest-duration time")
            start, end = (
                iso_timestamp(node.get_text(strip=True)) for node in times[:2]
            )
        elif source == "VJudge":
            node = soup.select_one('textarea[name="dataJson"]')
            data = json.loads(node.get_text())
            title, start, end = (
                data["title"],
                int(data["begin"]) // 1000,
                int(data["end"]) // 1000,
            )
        else:
            data = None
            for script in soup.find_all("script"):
                text = script.string or ""
                match = re.search(r"\bwindow\.pageInfo\s*=\s*", text)
                if match:
                    data, _ = json.JSONDecoder().raw_decode(text[match.end() :])
                    break
            title, start, end = (
                data["name"],
                int(data["startTime"]) // 1000,
                int(data["endTime"]) // 1000,
            )
        return contest_data(source, url, title, start, end)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ContestError(
            "无法读取比赛标题或起止时间；比赛可能未公开、需要登录，或页面结构已变化。"
        ) from None


def parse_abc_list(page: str) -> list[dict]:
    """Read upcoming, active and recent ABCs from AtCoder's public list."""
    soup = BeautifulSoup(page, "html.parser")
    tables = soup.select(
        "#contest-table-upcoming, #contest-table-active, #contest-table-recent"
    )
    if not tables:
        raise ContestError("AtCoder 比赛列表暂不可用。")
    contests = {}
    for table in tables:
        for row in table.select("tbody tr"):
            link = row.find("a", href=re.compile(r"^/contests/abc\d+$"))
            if link is None:
                continue
            try:
                start = iso_timestamp(row.find("time").get_text(strip=True))
                hours, minutes = map(int, row.find_all("td")[2].get_text().split(":"))
                if hours < 0 or not 0 <= minutes < 60:
                    raise ValueError
                url = "https://atcoder.jp" + link["href"]
                contests[url] = contest_data(
                    "AtCoder",
                    url,
                    link.get_text(" ", strip=True),
                    start,
                    start + hours * 3600 + minutes * 60,
                )
            except (AttributeError, IndexError, TypeError, ValueError):
                raise ContestError("AtCoder ABC 列表时间解析失败。") from None
    return list(contests.values())


class TrainingService:
    """Manage training contests on connections protected by the plugin lock."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    @staticmethod
    async def initialize(db: aiosqlite.Connection) -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS training_contests ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, "
            "source TEXT NOT NULL, title TEXT NOT NULL, start INTEGER NOT NULL, "
            "end INTEGER NOT NULL CHECK(end > start), "
            "origin TEXT NOT NULL CHECK(origin IN ('auto', 'manual')), "
            "added_by TEXT NOT NULL DEFAULT '', deleted INTEGER NOT NULL DEFAULT 0, "
            "revision INTEGER NOT NULL DEFAULT 1)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS training_contests_start "
            "ON training_contests(deleted, start)"
        )

    async def get_page(self, url: str) -> str:
        # Destinations are fixed or normalized; do not follow off-site redirects.
        async with self.session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ContestError(
                    f"比赛页面访问失败（HTTP {response.status}），"
                    "请稍后重试或检查比赛是否公开。"
                )
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise ContestError("比赛页面过大，暂时无法解析。")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")

    async def sync_abc(self, db: aiosqlite.Connection, ttl: int) -> str | None:
        """Cache ABC discovery independently of CLIST and keep deletion tombstones."""
        key = "training:ABC"
        now = datetime.now(UTC).timestamp()
        async with db.execute(
            "SELECT payload, updated_at, attempted_at FROM source_cache WHERE source=?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        entries, updated, attempted = None, 0, 0
        if row:
            updated, attempted = row[1:]
            try:
                entries = json.loads(row[0])
                if not isinstance(entries, list):
                    entries = None
            except (TypeError, ValueError):
                pass
        notice = None
        if entries is None or not 0 <= now - updated < ttl:
            if now - attempted >= 60:
                try:
                    fresh = parse_abc_list(
                        await self.get_page("https://atcoder.jp/contests/")
                    )
                except (
                    ContestError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    UnicodeError,
                ):
                    await db.execute(
                        "INSERT INTO source_cache VALUES (?, NULL, 0, ?) "
                        "ON CONFLICT(source) DO UPDATE SET "
                        "attempted_at=excluded.attempted_at",
                        (key, now),
                    )
                else:
                    entries, updated = fresh, now
                    await db.execute(
                        "INSERT INTO source_cache VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(source) DO UPDATE SET payload=excluded.payload, "
                        "updated_at=excluded.updated_at, "
                        "attempted_at=excluded.attempted_at",
                        (key, json.dumps(entries, ensure_ascii=False), now, now),
                    )
            if entries is None or not 0 <= now - updated <= 86400:
                entries = []
                notice = (
                    "自动 ABC 同步暂不可用，可能有遗漏；"
                    "已登记周赛仍会显示，60 秒后可重试。"
                )
            elif now - updated >= ttl:
                stamp = datetime.fromtimestamp(updated, BEIJING).strftime("%m-%d %H:%M")
                notice = (
                    f"自动 ABC 刷新失败，使用 {stamp} 的旧缓存；已登记周赛仍会显示。"
                )
        begin, end = week_bounds()
        for item in entries:
            if not begin <= item["start"] < end:
                # Also refresh an existing ABC that was postponed to another week.
                async with db.execute(
                    "SELECT id FROM training_contests WHERE url=?", (item["url"],)
                ) as cursor:
                    if await cursor.fetchone() is None:
                        continue
            await db.execute(
                "INSERT INTO training_contests(url, source, title, start, end, origin) "
                "VALUES (?, ?, ?, ?, ?, 'auto') ON CONFLICT(url) DO UPDATE SET "
                "title=excluded.title, start=excluded.start, end=excluded.end, "
                "revision=training_contests.revision+1 "
                "WHERE training_contests.origin='auto' AND training_contests.deleted=0 "
                "AND (training_contests.title!=excluded.title "
                "OR training_contests.start!=excluded.start "
                "OR training_contests.end!=excluded.end)",
                (
                    item["url"],
                    item["source"],
                    item["title"],
                    item["start"],
                    item["end"],
                ),
            )
        await db.commit()
        return notice

    async def add(self, db: aiosqlite.Connection, value: str, sender: str) -> dict:
        source, url = normalize_url(value)
        async with db.execute(
            "SELECT deleted FROM training_contests WHERE url=?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and not row[0]:
            raise ContestError("该比赛已在周赛列表中，无需重复添加。")
        item = parse_contest(source, url, await self.get_page(url))
        await db.execute(
            "INSERT INTO training_contests"
            "(url, source, title, start, end, origin, added_by) "
            "VALUES (?, ?, ?, ?, ?, 'manual', ?) ON CONFLICT(url) DO UPDATE SET "
            "source=excluded.source, title=excluded.title, start=excluded.start, "
            "end=excluded.end, origin='manual', added_by=excluded.added_by, deleted=0, "
            "revision=training_contests.revision+1",
            (url, source, item["title"], item["start"], item["end"], sender),
        )
        await db.commit()
        return item

    @staticmethod
    async def list_contests(
        db: aiosqlite.Connection, weekly: bool = False
    ) -> list[dict]:
        clause, params = "", ()
        if weekly:
            clause, params = " AND start>=? AND start<?", week_bounds()
        async with db.execute(
            "SELECT id, url, source, title, start, end, origin, revision "
            "FROM training_contests WHERE deleted=0" + clause + " ORDER BY start, id",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in rows]

    @staticmethod
    async def delete(db: aiosqlite.Connection, contest_id: int, revision: int) -> bool:
        async with db.execute(
            "UPDATE training_contests SET deleted=1, revision=revision+1 "
            "WHERE id=? AND revision=? AND deleted=0",
            (contest_id, revision),
        ) as cursor:
            changed = cursor.rowcount == 1
        await db.commit()
        return changed


def format_contest(item: dict) -> str:
    """Format both endpoints in Beijing time, including cross-day contests."""
    start, end = (
        datetime.fromtimestamp(item[field], BEIJING).strftime("%m-%d %H:%M")
        for field in ("start", "end")
    )
    return f"[{item['source']}] {item['title']}\n{start} ～ {end}\n{item['url']}"
