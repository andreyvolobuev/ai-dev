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
async def test_at_mention_in_operator_message_auto_registers_handle() -> None:
    """When the operator types ``@v.kura`` in a reply, that handle
    becomes resolvable. Mirrors production where any workspace handle
    resolves on first DM — without this the bot can't follow «у него
    ник @v.kura» pointers and stays stuck searching the directory.

    Pure-handle text like ``@v.kura`` should register; non-handle ats
    (emails, addresses) shouldn't pollute the directory."""
    chat = InMemoryChat()
    assert await chat.find_user_by_username("v.kura") is None
    await chat.post_user_message("у него ник @v.kura, спрашивай его")
    user = await chat.find_user_by_username("v.kura")
    assert user is not None
    assert user.id == "uid-v.kura"


@pytest.mark.asyncio
async def test_at_mention_skips_email_local_parts() -> None:
    """An email-looking ``foo@bar.com`` shouldn't auto-register ``foo``
    or ``bar.com`` as Mattermost handles — only ``@handle`` patterns."""
    chat = InMemoryChat()
    await chat.post_user_message("write to vasya@example.com instead")
    assert await chat.find_user_by_username("vasya") is None
    assert await chat.find_user_by_username("example.com") is None


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


@pytest.mark.asyncio
async def test_search_users_by_name_matches_first_and_last_name() -> None:
    """search_users_by_name does substring (case-insensitive) match
    on first_name / last_name / display_name / username."""
    chat = InMemoryChat()
    chat.register_user(
        "v.kurochkin", first_name="Василий", last_name="Курочкин",
        display_name="Vasiliy Kurochkin", position="QA Engineer",
    )
    chat.register_user(
        "d.shvarts", first_name="Дмитрий", last_name="Шварц",
        display_name="Dmitry Shvarts",
    )

    by_surname = await chat.search_users_by_name("курочкин")
    assert {u.username for u in by_surname} == {"v.kurochkin"}

    by_first = await chat.search_users_by_name("Василий")
    assert {u.username for u in by_first} == {"v.kurochkin"}

    by_partial = await chat.search_users_by_name("шварц")
    assert {u.username for u in by_partial} == {"d.shvarts"}

    none = await chat.search_users_by_name("ivanov")
    assert list(none) == []


@pytest.mark.asyncio
async def test_search_users_by_name_respects_limit() -> None:
    chat = InMemoryChat()
    for i in range(5):
        chat.register_user(f"u{i}", first_name="Тест", last_name=f"User{i}")
    results = await chat.search_users_by_name("Тест", limit=2)
    assert len(list(results)) == 2


@pytest.mark.asyncio
async def test_search_users_by_name_empty_query_returns_empty() -> None:
    chat = InMemoryChat()
    chat.register_user("alice", first_name="Alice")
    assert list(await chat.search_users_by_name("")) == []
    assert list(await chat.search_users_by_name("   ")) == []
