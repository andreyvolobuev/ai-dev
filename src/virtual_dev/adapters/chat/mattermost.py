"""Mattermost adapter.

Reads (``read_thread``, ``find_user_by_*``) — always enabled.

Writes (``send_direct``, ``send_to_channel``) — enabled from Phase 3 for
the Communicator agent to ping reviewers / escalate / answer questions.
Rate-limiting and working-hours policy live in
:class:`virtual_dev.application.services.CommunicatorService`, not here.

``subscribe`` (websocket listener) stays unimplemented: Phase 3 uses
polling of GitLab MR comments instead of MM incoming.

The underlying library ``mattermostdriver`` is synchronous; calls are wrapped
in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

from loguru import logger
from mattermostdriver import Driver
from mattermostdriver.websocket import Websocket

from virtual_dev.domain.models.chat import ChatFile, ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort


class _ServerAuthSSLWebsocket(Websocket):
    """``Websocket`` with the SSL context fixed for client-side WSS.

    ``mattermostdriver`` builds an ``ssl.Purpose.CLIENT_AUTH`` context, which
    is the *server*-side role. Modern Python then refuses to use it for an
    outgoing connection with:
    "Cannot create a client socket with a PROTOCOL_TLS_SERVER context".

    We replicate the poker-planning-bot fix: build a ``SERVER_AUTH`` context
    (meaning: authenticate the server) and honour ``verify`` / CA-file.
    """

    async def connect(self, event_handler: Any) -> None:
        import websockets
        from mattermostdriver.websocket import log as ws_log

        context: ssl.SSLContext | None
        if self.options["scheme"] == "https":
            verify = self.options.get("verify", True)
            context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            if verify is False:
                # Python ≥3.12 enforces: setting verify_mode=CERT_NONE while
                # check_hostname is True raises ValueError. So clear
                # check_hostname FIRST, then drop verify_mode.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            elif isinstance(verify, str):
                context.load_verify_locations(cafile=verify)
        else:
            context = None

        scheme = "wss://" if self.options["scheme"] == "https" else "ws://"
        url = "{scheme}{url}:{port}{basepath}/websocket".format(
            scheme=scheme,
            url=self.options["url"],
            port=str(self.options["port"]),
            basepath=self.options["basepath"],
        )

        self._alive = True
        kw_args: dict[str, Any] = dict(self.options.get("websocket_kw_args") or {})
        consecutive_failures = 0
        base_delay = float(self.options.get("keepalive_delay", 3))
        while self._alive:
            try:
                ws_log.debug("MM WS connecting → %s (attempt %d)", url, consecutive_failures + 1)
                websocket = await websockets.connect(url, ssl=context, **kw_args)
                ws_log.debug("MM WS connected, sending auth challenge")
                await self._authenticate_websocket(websocket, event_handler)
                ws_log.info("MM WS authenticated; entering message loop")
                consecutive_failures = 0   # successful auth resets backoff
                while self._alive:
                    try:
                        await self._start_loop(websocket, event_handler)
                    except websockets.ConnectionClosedError:
                        ws_log.info("MM WS message loop closed; will reconnect")
                        break
                if not self.options.get("keepalive") or not self._alive:
                    break
            except Exception as exc:
                consecutive_failures += 1
                # Exponential backoff capped at 5 minutes — prevents the corp
                # firewall from rate-limiting us into a tighter loop after a
                # few sequential failures.
                delay = min(base_delay * (2 ** min(consecutive_failures - 1, 7)), 300)
                # Log only the first N failures verbose, then once-per-minute
                # equivalent so the log doesn't drown out everything else.
                if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                    ws_log.warning(
                        "MM WS connect failed (%d in a row): %s: %s — "
                        "retrying in %.0fs",
                        consecutive_failures, type(exc).__name__, exc, delay,
                    )
                await asyncio.sleep(delay)


def _parse_host_port_scheme(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"Mattermost URL has no host: {url!r}")
    scheme = parsed.scheme or "https"
    default_port = 443 if scheme == "https" else 80
    return parsed.hostname, parsed.port or default_port, scheme


class MattermostChat(ChatPort):
    """``ChatPort`` backed by Mattermost (self-hosted), read-only for Phase 1."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        bot_username: str | None = None,
        ssl_verify: bool = True,
        ssl_ca_file: str | None = None,
    ) -> None:
        if not url or not token:
            raise ValueError("Mattermost URL and token must be provided")
        host, port, scheme = _parse_host_port_scheme(url)
        # Corporate MMs often sit behind a self-signed cert or a CA the
        # machine doesn't know about. Callers can disable verify or point
        # at a CA bundle explicitly.
        verify: bool | str
        if not ssl_verify:
            verify = False
        elif ssl_ca_file:
            verify = ssl_ca_file
        else:
            verify = True
        self._driver = Driver(
            {
                "url": host,
                "port": port,
                "scheme": scheme,
                "basepath": "/api/v4",
                "token": token,
                "verify": verify,
                "timeout": 30,
            }
        )
        # Stash the original URL so we can build absolute file-download
        # links (``<base>/api/v4/files/<id>``) for ``ChatFile.url``. The
        # driver stores host/port/scheme separately, so reconstructing
        # would lose any path prefix the operator embedded in the URL.
        self._base_url = url.rstrip("/")
        self._bot_username = bot_username
        self._logged_in = False
        self._bot_id_cached: str = ""

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._driver.login()
            self._logged_in = True

    def _bot_user_id(self) -> str:
        """Return the authenticated user's id (our bot token → bot account)."""
        self._ensure_login()
        if self._bot_id_cached:
            return self._bot_id_cached
        me = self._driver.users.get_user("me")
        if not isinstance(me, dict) or not me.get("id"):
            raise RuntimeError("Could not resolve Mattermost bot user id from /users/me")
        self._bot_id_cached = str(me["id"])
        return self._bot_id_cached

    async def bot_user_id(self) -> str:
        """Async accessor, used by agents that need to compare post authors."""
        return await asyncio.to_thread(self._bot_user_id)

    # --- Phase-1 allowed methods ---

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        def _fetch() -> list[ChatMessage]:
            self._ensure_login()
            bot_id = self._bot_user_id()
            raw = self._driver.posts.get_thread(thread_root_id)
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Unexpected response from Mattermost thread API: {type(raw).__name__}"
                )
            posts = cast(dict[str, dict[str, Any]], raw.get("posts") or {})
            order = cast(list[str], raw.get("order") or list(posts.keys()))
            reactions = self._reactions_for_posts(list(order))
            out: list[ChatMessage] = []
            for post_id in order:
                post = posts.get(post_id)
                if post is None:
                    continue
                message = self._post_to_message(post)
                all_reactions, bot_reactions = reactions.get(post_id, (set(), set()))
                message.reactions = sorted(all_reactions)
                message.bot_reactions = sorted(bot_reactions)
                # If the bot itself authored this post, mark it trusted.
                if post.get("user_id") == bot_id:
                    message.trusted = True
                out.append(message)
            return out

        return await asyncio.to_thread(_fetch)

    def _reactions_for_posts(
        self, post_ids: list[str],
    ) -> dict[str, tuple[set[str], set[str]]]:
        """Fetch reactions for a batch of posts.

        Returns ``{post_id: (all_emoji_names, bot_emoji_names)}``. On
        permission errors / 4xx we swallow and return empty sets — missing
        reactions just mean "not yet processed", which is the safe default.
        """
        if not post_ids:
            return {}
        bot_id = self._bot_user_id()
        out: dict[str, tuple[set[str], set[str]]] = {}
        for pid in post_ids:
            try:
                raw = self._driver.posts.client.get(f"/posts/{pid}/reactions")
            except Exception:
                out[pid] = (set(), set())
                continue
            if not isinstance(raw, list):
                out[pid] = (set(), set())
                continue
            all_names: set[str] = set()
            bot_names: set[str] = set()
            for r in raw:
                if not isinstance(r, dict):
                    continue
                name = str(r.get("emoji_name") or "")
                if not name:
                    continue
                all_names.add(name)
                if r.get("user_id") == bot_id:
                    bot_names.add(name)
            out[pid] = (all_names, bot_names)
        return out

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        def _fetch() -> ChatUser | None:
            self._ensure_login()
            try:
                raw = self._driver.users.get_user_by_email(email)
            except Exception:  # 404 or 400; surface as None
                logger.debug("mattermost user lookup by email {!r} failed", email)
                return None
            return self._user_from_raw(raw)

        return await asyncio.to_thread(_fetch)

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        def _fetch() -> ChatUser | None:
            self._ensure_login()
            try:
                raw = self._driver.users.get_user_by_username(username)
            except Exception:
                logger.debug("mattermost user lookup by username {!r} failed", username)
                return None
            return self._user_from_raw(raw)

        return await asyncio.to_thread(_fetch)

    async def search_users_by_name(
        self, query: str, *, limit: int = 25,
    ) -> Sequence[ChatUser]:
        """Fuzzy-search MM directory: matches username, first/last name,
        nickname. Wraps ``GET /api/v4/users/autocomplete?name=<query>``.
        """
        query = query.strip()
        if not query:
            return []
        capped = max(1, min(limit, 100))

        def _fetch() -> list[ChatUser]:
            self._ensure_login()
            try:
                raw = self._driver.users.autocomplete_users(
                    params={"name": query, "limit": capped},
                )
            except Exception:
                logger.debug(
                    "mattermost autocomplete_users {!r} failed", query,
                )
                return []
            # MM returns either a flat list or
            # {"users": [...], "out_of_channel": [...]} depending on
            # whether in_team/in_channel were supplied. Handle both.
            entries: list[Any] = []
            if isinstance(raw, list):
                entries = list(raw)
            elif isinstance(raw, dict):
                entries.extend(raw.get("users") or [])
                entries.extend(raw.get("out_of_channel") or [])
            users: list[ChatUser] = []
            for entry in entries[:capped]:
                user = self._user_from_raw(entry)
                if user is not None:
                    users.append(user)
            return users

        return await asyncio.to_thread(_fetch)

    # --- Writes (Phase 3) ---

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        def _run() -> ChatMessage:
            self._ensure_login()
            bot_id = self._bot_user_id()
            channel = self._driver.channels.create_direct_message_channel(
                options=[bot_id, user_id]
            )
            if not isinstance(channel, dict) or not channel.get("id"):
                raise RuntimeError(
                    f"create_direct_message_channel returned unexpected: {channel!r}"
                )
            post = self._driver.posts.create_post(
                options={"channel_id": str(channel["id"]), "message": text}
            )
            return self._post_to_message(cast(dict[str, Any], post))

        return await asyncio.to_thread(_run)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None
    ) -> ChatMessage:
        def _run() -> ChatMessage:
            self._ensure_login()
            options: dict[str, Any] = {"channel_id": channel_id, "message": text}
            if thread_root_id:
                options["root_id"] = thread_root_id
            post = self._driver.posts.create_post(options=options)
            return self._post_to_message(cast(dict[str, Any], post))

        return await asyncio.to_thread(_run)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        """Stick an emoji reaction (as the bot user) on a post."""
        def _run() -> None:
            self._ensure_login()
            bot_id = self._bot_user_id()
            try:
                self._driver.reactions.create_reaction({
                    "user_id": bot_id,
                    "post_id": post_id,
                    "emoji_name": emoji_name,
                })
            except Exception as exc:
                # Reactions endpoint returns 4xx if the reaction already
                # exists — treat as success.
                msg = str(exc)
                if "already exists" in msg or "400" in msg:
                    return
                raise

        await asyncio.to_thread(_run)

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> list[ChatMessage]:
        """Pull posts in ``channel_id`` strictly newer than ``since``.

        Used by the catch-up worker to fill the gap when our WebSocket
        was disconnected. Mattermost's ``GET /channels/{id}/posts?since``
        endpoint returns posts that were created OR updated at or after
        the timestamp; we re-filter on ``create_at`` so we don't surface
        edits as new fragments.

        Returned in chronological order (oldest first).
        """
        def _fetch() -> list[ChatMessage]:
            self._ensure_login()
            bot_id = self._bot_user_id()
            since_ms = int(since.timestamp() * 1000)
            try:
                raw = self._driver.posts.get_posts_for_channel(
                    channel_id, params={"since": since_ms},
                )
            except Exception:
                logger.exception(
                    "MattermostChat: get_posts_for_channel failed for {} since {}",
                    channel_id, since.isoformat(),
                )
                return []
            if not isinstance(raw, dict):
                return []
            posts = cast(dict[str, dict[str, Any]], raw.get("posts") or {})
            # MM returns ``order`` sorted newest-first; we want chronological
            # for the catch-up dispatch loop, and we filter strictly newer.
            entries: list[tuple[int, dict[str, Any]]] = []
            for post_id, post in posts.items():
                if not isinstance(post, dict):
                    continue
                create_at = int(post.get("create_at") or 0)
                if create_at <= since_ms:
                    continue
                entries.append((create_at, post))
            entries.sort(key=lambda t: t[0])
            out: list[ChatMessage] = []
            for _create_at, post in entries:
                message = self._post_to_message(post)
                if str(post.get("user_id") or "") == bot_id:
                    message.trusted = True
                out.append(message)
            return out

        return await asyncio.to_thread(_fetch)

    async def get_post(self, post_id: str) -> ChatMessage | None:
        def _run() -> ChatMessage | None:
            self._ensure_login()
            try:
                raw = self._driver.posts.get_post(post_id)
            except Exception:
                return None
            if not isinstance(raw, dict):
                return None
            message = self._post_to_message(raw)
            reactions = self._reactions_for_posts([post_id])
            all_r, bot_r = reactions.get(post_id, (set(), set()))
            message.reactions = sorted(all_r)
            message.bot_reactions = sorted(bot_r)
            return message

        return await asyncio.to_thread(_run)

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        """Stream incoming MM posts via WebSocket.

        Bridges the driver's callback-based WebSocket to an async iterator:
        a background task drives the ``connect`` coroutine and drops parsed
        ``ChatMessage`` objects onto a queue that the iterator drains.

        Critical: ``driver.login()`` MUST be called before constructing the
        Websocket. ``Client.__init__`` initialises ``client.token = ''``;
        only ``login()`` copies the real token out of ``options``. Without
        this call the WS auth challenge sends an empty token and MM closes
        the connection (manifests as ``no close frame received or sent``).
        """
        # Force a login + cache bot id before reading client.token.
        self._ensure_login()
        self._bot_user_id()

        queue: asyncio.Queue[ChatMessage] = asyncio.Queue()

        async def _handler(raw: str) -> None:
            parsed = self._parse_posted_event(raw)
            if parsed is not None:
                await queue.put(parsed)

        options = dict(self._driver.options)
        options.setdefault("keepalive", True)
        options.setdefault("keepalive_delay", 3)
        options.setdefault("websocket_kw_args", None)
        ws = _ServerAuthSSLWebsocket(options, self._driver.client.token)
        ws_task = asyncio.create_task(ws.connect(_handler), name="mm-ws-connect")

        async def _iter() -> AsyncIterator[ChatMessage]:
            try:
                while True:
                    msg = await queue.get()
                    yield msg
            finally:
                ws._alive = False
                ws_task.cancel()

        return _iter()

    def _parse_posted_event(self, raw: str) -> ChatMessage | None:
        """Extract a ChatMessage from a ``posted``-style WebSocket event.

        Returns ``None`` for any other event type so the iterator stays a
        stream of real user messages.
        """
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("event") != "posted":
            return None
        data = event.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        post_raw = data.get("post")
        if isinstance(post_raw, str):
            try:
                post = json.loads(post_raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(post_raw, dict):
            post = post_raw
        else:
            return None
        if not isinstance(post, dict):
            return None
        message = self._post_to_message(post)
        # Mark trusted if the bot authored it.
        if self._bot_id_cached and post.get("user_id") == self._bot_id_cached:
            message.trusted = True
        return message

    # --- helpers ---

    def _post_to_message(self, raw: dict[str, Any]) -> ChatMessage:
        ts_ms = int(raw.get("create_at") or 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
        root = raw.get("root_id") or None
        text = str(raw.get("message") or "")
        # Trust is False unless the post is authored by our own bot user.
        trusted = bool(
            self._bot_username
            and raw.get("user_id")
            and str(raw.get("props", {}).get("username", "")) == self._bot_username
        )
        return ChatMessage(
            id=str(raw.get("id") or ""),
            channel_id=str(raw.get("channel_id") or ""),
            author_id=str(raw.get("user_id") or ""),
            text=text,
            timestamp=ts,
            thread_root_id=root,
            trusted=trusted,
            files=self._extract_files(raw),
        )

    def _extract_files(self, raw: dict[str, Any]) -> list[ChatFile]:
        """Pull file attachments out of a Mattermost post.

        Mattermost's posts API returns files as
        ``post.metadata.files: [{id, name, extension, size, mime_type, ...}]``
        when the post has any (an empty list otherwise). We turn each
        entry into a ``ChatFile`` with the ready-to-fetch download URL
        — that's ``<MM_URL>/api/v4/files/<id>``, which the generic
        ``download_url_bytes`` will hit with the bot's MATTERMOST_TOKEN
        as Bearer (host-aware auth dispatch in ``_helpers.py``).

        Falls back to ``post.file_ids`` (just the id list, no metadata)
        if ``metadata.files`` is absent — older MM versions or
        edge-case post shapes. Name/mime/size will be empty in that
        case but the URL is still usable.
        """
        files: list[ChatFile] = []
        metadata = raw.get("metadata") or {}
        seen: set[str] = set()
        for file_info in metadata.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            fid = str(file_info.get("id") or "")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            files.append(ChatFile(
                id=fid,
                name=str(file_info.get("name") or ""),
                url=f"{self._base_url.rstrip('/')}/api/v4/files/{fid}",
                mime_type=str(file_info.get("mime_type") or ""),
                extension=str(file_info.get("extension") or ""),
                size=int(file_info.get("size") or 0),
            ))
        for fid in raw.get("file_ids") or []:
            fid_s = str(fid)
            if not fid_s or fid_s in seen:
                continue
            seen.add(fid_s)
            files.append(ChatFile(
                id=fid_s,
                name="",
                url=f"{self._base_url.rstrip('/')}/api/v4/files/{fid_s}",
            ))
        return files

    def _user_from_raw(self, raw: Any) -> ChatUser | None:
        if not isinstance(raw, dict):
            return None
        return ChatUser(
            id=str(raw.get("id") or ""),
            username=str(raw.get("username") or ""),
            email=str(raw.get("email") or "") or None,
            display_name=str(raw.get("nickname") or raw.get("first_name") or "") or None,
            first_name=str(raw.get("first_name") or "") or None,
            last_name=str(raw.get("last_name") or "") or None,
            position=str(raw.get("position") or "") or None,
            is_bot=bool(raw.get("is_bot")),
        )
