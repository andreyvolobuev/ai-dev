"""Port for chat (Mattermost / Slack / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from virtual_dev.domain.models.chat import ChatMessage, ChatUser


class ChatPort(ABC):
    """Abstraction over a team chat.

    Only ``send_*`` and ``subscribe`` are considered side-effects that can
    reach humans — the Communicator agent is the only caller in Phases 0-1.
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
    def subscribe(self) -> AsyncIterator[ChatMessage]:
        """Stream incoming chat messages (websocket-backed in real adapters)."""
