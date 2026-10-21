"""Persistent personal problem lists; adding a link never requests its page."""

import re
from urllib.parse import urlsplit, urlunsplit

import aiosqlite


class TodoError(ValueError):
    """A TODO command that cannot be applied."""


def normalize_problem_url(value: str) -> str:
    """Validate a web link, retaining query and fragment based problem IDs."""
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise TodoError("请提供一个有效的 HTTP/HTTPS 题目链接（不含空白）。")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme.lower(), port) not in {
            ("http", 80),
            ("https", 443),
        }:
            host += f":{port}"
        return urlunsplit(
            (parts.scheme.lower(), host, parts.path or "/", parts.query, parts.fragment)
        )
    except (ValueError, UnicodeError):
        raise TodoError("请提供一个有效的 HTTP/HTTPS 题目链接。") from None


class TodoService:
    """Scope every read and mutation to an adapter ID and sender ID."""

    @staticmethod
    async def initialize(db: aiosqlite.Connection) -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS todo_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "platform_id TEXT NOT NULL, sender_id TEXT NOT NULL, "
            "url TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, "
            "UNIQUE(platform_id, sender_id, url))"
        )

    @staticmethod
    async def list_pending(db: aiosqlite.Connection, owner: tuple[str, str]) -> str:
        async with db.execute(
            "SELECT id, url FROM todo_items "
            "WHERE platform_id=? AND sender_id=? AND completed=0 ORDER BY id",
            owner,
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return "当前没有待办题目。\n发送 /todo <题目链接> 添加。"
        return "\n\n".join(
            ["我的 TODO"]
            + [f"{number}. {url}" for number, url in rows]
            + ["/todo 完成 <编号>\n/todo 删除 <编号>\n编号固定，请使用上方显示的编号。"]
        )

    @staticmethod
    async def add(db: aiosqlite.Connection, owner: tuple[str, str], value: str) -> str:
        url = normalize_problem_url(value)
        async with db.execute(
            "SELECT id, completed FROM todo_items "
            "WHERE platform_id=? AND sender_id=? AND url=?",
            (*owner, url),
        ) as cursor:
            previous = await cursor.fetchone()
        if previous:
            number, completed = previous
            if not completed:
                return f"该题已在待办中（编号 {number}）。\n{url}"
            await db.execute(
                "UPDATE todo_items SET completed=0 "
                "WHERE id=? AND platform_id=? AND sender_id=?",
                (number, *owner),
            )
            message = f"已重新加入待办（编号 {number}）：\n{url}"
        else:
            async with db.execute(
                "INSERT INTO todo_items (platform_id, sender_id, url) VALUES (?, ?, ?)",
                (*owner, url),
            ) as cursor:
                message = f"已添加待办（编号 {cursor.lastrowid}）：\n{url}"
        await db.commit()
        return message

    @staticmethod
    async def change(
        db: aiosqlite.Connection, owner: tuple[str, str], action: str, value: str
    ) -> str:
        if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < 1:
            raise TodoError("编号无效，请发送 /todo 查看当前待办编号。")
        number = int(value)
        sql = (
            "UPDATE todo_items SET completed=1 "
            if action == "完成"
            else "DELETE FROM todo_items "
        )
        async with db.execute(
            sql + "WHERE id=? AND platform_id=? AND sender_id=? AND completed=0",
            (number, *owner),
        ) as cursor:
            changed = cursor.rowcount
        await db.commit()
        if not changed:
            return "当前待办中没有该编号，请发送 /todo 查看。"
        return f"已{'完成' if action == '完成' else '删除'}待办 {number}。"
