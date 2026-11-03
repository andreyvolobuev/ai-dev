"""Posting a file into a Mattermost thread."""

from __future__ import annotations

from typing import Any

import pytest

from virtual_dev.adapters.chat.mattermost import MattermostChat


class _Files:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    def upload_file(self, channel_id: str, files: dict[str, Any]) -> dict[str, Any]:
        self.uploads.append({"channel_id": channel_id, "files": files})
        return {"file_infos": [{"id": "file-1"}]}


class _Posts:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_post(self, options: dict[str, Any]) -> dict[str, Any]:
        self.created.append(options)
        return {
            "id": "post-1", "channel_id": options["channel_id"], "user_id": "bot",
            "message": options["message"], "root_id": options.get("root_id", ""),
            "create_at": 0,
        }


class _Driver:
    def __init__(self) -> None:
        self.files = _Files()
        self.posts = _Posts()


@pytest.mark.asyncio
async def test_file_is_uploaded_and_attached_to_the_thread_reply() -> None:
    """Asked for an .md report, the bot said «файл приложить сюда не могу»
    (rainbow!1031)."""
    chat = MattermostChat(url="https://mm.example", token="t")
    driver = _Driver()
    chat._driver = driver  # type: ignore[assignment]
    chat._ensure_login = lambda: None  # type: ignore[method-assign]

    await chat.send_file_to_channel(
        "chan", "Отчёт во вложении.", filename="report.md",
        content="# Отчёт".encode(), thread_root_id="root",
    )

    (upload,) = driver.files.uploads
    assert upload["channel_id"] == "chan"
    assert upload["files"] == {"files": ("report.md", "# Отчёт".encode())}
    (post,) = driver.posts.created
    assert post == {
        "channel_id": "chan", "message": "Отчёт во вложении.",
        "file_ids": ["file-1"], "root_id": "root",
    }
