"""Bus handler that turns ``plan.ready`` messages into code + draft MR.

The Dev inbox is the boundary between the pure DevAgent (which plans a
workspace, runs Claude, commits, pushes, opens an MR) and Phase-2
tracker-facing side-effects:

    * On successful MR: transition the Jira ticket to ``Review`` and
      comment a link to the MR.
    * On FAILED: comment the failure notes so a human can pick it up.

Transitioning / commenting errors are swallowed into logs so one Jira
hiccup does not block future plans.
"""

from __future__ import annotations

from loguru import logger

from virtual_dev.application.agents import DevAgent, DevOutcome
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig


def _render_mr_comment(template: str, *, web_url: str, branch: str) -> str:
    try:
        return template.format(web_url=web_url, branch=branch)
    except (KeyError, IndexError) as exc:
        logger.warning("DevInbox: mr_link_comment template format failed: {}", exc)
        return template


def _render_failure_comment(template: str, *, notes: str, branch: str | None) -> str:
    branch_block = f"- Branch: `{branch}`" if branch else ""
    notes_block = ""
    if notes:
        notes_block = f"\n_Notes from the agent:_\n{notes}"
    try:
        return template.format(branch_block=branch_block, notes_block=notes_block)
    except (KeyError, IndexError) as exc:
        logger.warning("DevInbox: failure_comment template format failed: {}", exc)
        return template


class DevInbox:
    """Handler bound to the ``plan.ready`` topic for one Dev-agent."""

    def __init__(
        self,
        *,
        dev_agent: DevAgent,
        task_tracker: TaskTrackerPort | None,
        config: AppConfig,
        post_to_tracker: bool = True,
    ) -> None:
        self._dev = dev_agent
        self._task_tracker = task_tracker
        self._config = config
        self._post_to_tracker = post_to_tracker

    async def handle(self, message: AgentMessage) -> None:
        tracker = str(message.payload.get("tracker") or "")
        external_id = str(message.payload.get("external_id") or "")
        if not tracker or not external_id:
            logger.warning("DevInbox: malformed payload {}", message.payload)
            return

        try:
            result = await self._dev.handle_plan(tracker, external_id)
        except Exception:
            logger.exception("DevInbox: Dev-agent raised for {}", external_id)
            return

        if result.outcome is DevOutcome.SKIPPED:
            logger.info(
                "DevInbox: Dev skipped {} ({})",
                external_id, result.skip_reason.value if result.skip_reason else "unknown",
            )
            return

        if result.outcome is DevOutcome.MR_OPENED and result.merge_request is not None:
            mr = result.merge_request
            if self._post_to_tracker and self._task_tracker is not None:
                to_review = self._config.agents.jira_transitions.to_review
                try:
                    await self._task_tracker.transition(external_id, to_review)
                except Exception:
                    logger.exception(
                        "DevInbox: transition to {} failed for {}",
                        to_review, external_id,
                    )
                try:
                    await self._task_tracker.comment(
                        external_id,
                        _render_mr_comment(
                            self._config.notifications.jira.mr_link_comment,
                            web_url=mr.web_url,
                            branch=result.branch_name or "",
                        ),
                    )
                except Exception:
                    logger.exception(
                        "DevInbox: MR-link comment failed for {}", external_id,
                    )
            return

        # FAILED or NO_CHANGES: surface a comment so humans see the state.
        if self._post_to_tracker and self._task_tracker is not None:
            notes = str(result.submission.get("notes") or "").strip()
            try:
                await self._task_tracker.comment(
                    external_id,
                    _render_failure_comment(
                        self._config.notifications.jira.failure_comment,
                        notes=notes,
                        branch=result.branch_name,
                    ),
                )
            except Exception:
                logger.exception(
                    "DevInbox: failure comment failed for {}", external_id,
                )
