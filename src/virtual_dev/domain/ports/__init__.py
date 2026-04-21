"""Domain ports — interfaces that adapters must implement."""

from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
    CodeAgentTool,
)
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.domain.ports.llm import LlmMessage, LlmPort, LlmResponse
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.secrets import SecretsPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.domain.ports.vcs import VcsPort

__all__ = [
    "AgentMessage",
    "ChatPort",
    "CodeAgentPort",
    "CodeAgentRequest",
    "CodeAgentResult",
    "CodeAgentTool",
    "KnowledgeBasePort",
    "LlmMessage",
    "LlmPort",
    "LlmResponse",
    "MessageBusPort",
    "SecretsPort",
    "TaskTrackerPort",
    "VcsPort",
]
