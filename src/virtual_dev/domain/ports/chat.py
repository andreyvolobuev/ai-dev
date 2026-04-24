"""Port for chat (Mattermost / Slack / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from virtual_dev.domain.models.chat import ChatMessage, ChatUser


class ChatPort(ABC):
    """Abstraction over a team chat.

    ``send_*`` and ``subscribe`` are side-effects that reach humans —
    gate them through :class:`~virtual_dev.application.services.CommunicatorService`
    where rate limits and working-hours policy apply.
    """

    @abstractmethod
    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        """Send a DM to a user."""

    @abstractmethod
    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None
    ) -> ChatMessage:
        """Send a message to a channel, optionally threaded under ``thread_root_id``."""

    @abstractmethod
    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        """Return every message in the thread, oldest first."""

    @abstractmethod
    async def find_user_by_email(self, email: str) -> ChatUser | None:
        """Resolve a user by email. Returns ``None`` if not found."""

    @abstractmethod
    async def find_user_by_username(self, username: str) -> ChatUser | None:
        """Resolve a user by username. Returns ``None`` if not found."""

    @abstractmethod
    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        """Add an emoji reaction (by name, e.g. ``white_check_mark``) to a post.

        Used as an idempotency marker so the bot doesn't re-process a
        thread reply it has already handled.
        """

    @abstractmethod
    async def get_post(self, post_id: str) -> ChatMessage | None:
        """Fetch a single post by id, including its reactions."""

    @abstractmethod
    def subscribe(self) -> AsyncIterator[ChatMessage]:
        """Stream incoming chat messages (WebSocket-backed in real adapters)."""
