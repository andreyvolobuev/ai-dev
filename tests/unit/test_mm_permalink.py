"""MM-адаптер: email автора поста и permalink на пост.

Permalink в MM имеет вид ``<base>/<team-name>/pl/<post-id>`` — имя
команды в конфиге не хранится, поэтому адаптер резолвит
channel → team → name и кеширует по каналу.
"""

from __future__ import annotations

from typing import Any

from virtual_dev.adapters.chat.mattermost import MattermostChat


class _FakeUsers:
    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self._users = users
        self.calls: list[str] = []

    def get_user(self, user_id: str) -> dict[str, Any]:
        self.calls.append(user_id)
        return self._users[user_id]


class _FakeChannels:
    def __init__(self, team_id: str = "team-1") -> None:
        self._team_id = team_id
        self.calls: list[str] = []

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        self.calls.append(channel_id)
        return {"id": channel_id, "team_id": self._team_id}


class _FakeTeams:
    def __init__(self, name: str = "datamining") -> None:
        self._name = name
        self.calls: list[str] = []

    def get_team(self, team_id: str) -> dict[str, Any]:
        self.calls.append(team_id)
        return {"id": team_id, "name": self._name}


class _FakeDriver:
    def __init__(self, users: dict[str, dict[str, Any]] | None = None) -> None:
        self.users = _FakeUsers(users or {})
        self.channels = _FakeChannels()
        self.teams = _FakeTeams()

    def login(self) -> None:
        return None


def _chat(driver: _FakeDriver) -> MattermostChat:
    chat = MattermostChat(url="https://mm.example", token="t")
    chat._driver = driver  # type: ignore[assignment]
    chat._logged_in = True
    return chat


async def test_get_user_by_id_returns_email() -> None:
    driver = _FakeDriver(users={"u1": {
        "id": "u1", "username": "ivanov", "email": "ivan.ivanov@2gis.ru",
    }})
    user = await _chat(driver).get_user_by_id("u1")

    assert user is not None
    assert user.username == "ivanov"
    assert user.email == "ivan.ivanov@2gis.ru"


async def test_get_user_by_id_unknown_user_is_none() -> None:
    driver = _FakeDriver(users={})
    assert await _chat(driver).get_user_by_id("nope") is None


async def test_post_permalink_includes_team_name() -> None:
    driver = _FakeDriver()
    link = await _chat(driver).post_permalink("post-9", "chan-1")

    assert link == "https://mm.example/datamining/pl/post-9"


async def test_post_permalink_caches_team_per_channel() -> None:
    """Ссылку строим на каждый интейк — второй раз ходить в API за тем
    же каналом незачем."""
    driver = _FakeDriver()
    chat = _chat(driver)
    await chat.post_permalink("post-9", "chan-1")
    await chat.post_permalink("post-10", "chan-1")

    assert driver.channels.calls == ["chan-1"]
    assert driver.teams.calls == ["team-1"]
