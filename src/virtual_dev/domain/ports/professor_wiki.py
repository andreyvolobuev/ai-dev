"""Порт обязательной статьи Профессора (fail-closed preflight, Этап 2)."""

from abc import ABC, abstractmethod
from typing import Final

from virtual_dev.domain.models.wiki_article import WikiArticle

MANDATORY_REVIEW_TOPIC: Final = "krusenstern/chris-review"
"""Обязательный стандарт командного код-ревью (статья Лоции) для Dev/Reviewer."""


class WikiUnavailableError(RuntimeError):
    """LLM-Wiki недоступна либо обязательная статья не получена.

    Повторяемая инфраструктурная ошибка: вызывающие обработчики НЕ должны
    подтверждать (ack) сообщение, делать claim, помечать задачу
    FAILED/stuck/blocked или превращать её в бизнес-результат. После
    восстановления Wiki следующая повторная попытка должна пройти сама
    (lease-redelivery, MM catch-up, polling).
    """


class ProfessorWikiPort(ABC):
    """Доставка обязательной статьи LLM-Wiki.

    Контракт: либо полная статья, либо ``WikiUnavailableError`` —
    никаких частичных текстов, кэша и «пока без статьи».
    """

    @abstractmethod
    async def fetch_mandatory_article(self, topic: str) -> WikiArticle:
        """Вернуть полную статью ``topic`` или бросить ``WikiUnavailableError``."""
