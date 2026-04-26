"""Tests for the test-analyst InMemoryChat adapter.

Pins the contract that ``lookup_mm_user`` (via
``find_user_by_username``) must not synthesize random users — it
only resolves handles the operator has spoken as. The earlier
permissive behaviour caused the planner to DM "Вася Курочкин" without
asking the reporter for his handle: the lookup tool said "yes he
exists" for any guessed transliteration.
"""

from __future__ import annotations

import pytest

from virtual_dev.adapters.chat.in_memory import InMemoryChat


@pytest.mark.asyncio
async def test_find_user_by_username_refuses_unknown_handle() -> None:
    chat = InMemoryChat()
    assert await chat.find_user_by_username("vasya.kurochkin") is None
    assert await chat.find_user_by_username("v.kurochkin") is None
    assert await chat.find_user_by_username("kurochkin") is None


@pytest.mark.asyncio
async def test_find_user_by_email_refuses_unknown_local_part() -> None:
    chat = InMemoryChat()
    assert await chat.find_user_by_email("vasya.kurochkin@example.com") is None


@pytest.mark.asyncio
async def test_default_user_is_known() -> None:
    """The reporter (whoever launched the test-analyst session) must
    be findable from the start so the planner can DM them."""
    chat = InMemoryChat(user_id="uid-reporter", user_name="reporter")
    user = await chat.find_user_by_username("reporter")
    assert user is not None
    assert user.id == "uid-reporter"


@pytest.mark.asyncio
async def test_speaking_as_registers_username_for_lookup() -> None:
    """Once the operator speaks as ``v.kura``, future lookups for that
    handle resolve. Mirrors how a person becomes known in the session."""
    chat = InMemoryChat()
    assert await chat.find_user_by_username("v.kura") is None
    await chat.post_user_message("hi from v.kura", author_username="v.kura")
    user = await chat.find_user_by_username("v.kura")
    assert user is not None
    assert user.id == "uid-v.kura"


@pytest.mark.asyncio
async def test_known_users_are_consistent_across_email_and_username() -> None:
    """``find_user_by_email("v.kura@2gis.ru")`` should resolve to the
    same id as ``find_user_by_username("v.kura")`` once registered."""
    chat = InMemoryChat()
    await chat.post_user_message("ping", author_username="v.kura")
    by_handle = await chat.find_user_by_username("v.kura")
    by_email = await chat.find_user_by_email("v.kura@2gis.ru")
    assert by_handle is not None and by_email is not None
    assert by_handle.id == by_email.id
