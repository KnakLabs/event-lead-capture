"""Trigger lambda — drains the queue into the agent runtime.

Mirrors `packages/slack-agent/src/lambdas/trigger/index.ts`. Deployed, SQS invokes
this with a batch of records and it calls the AgentCore runtime; locally, a worker
thread calls `process_once()` in a loop and the "runtime" is an in-process import.

Either way the contract is the same: one queue message in, one agent invocation
out, and a failure leaves the message on the queue to be retried (then
dead-lettered) rather than swallowing it.
"""

import logging
from typing import Any

from agent import agent

logger = logging.getLogger(__name__)


def process_once(queue: Any) -> bool:
    """Handle at most one message. Returns False when the queue is empty."""
    message = queue.receive()
    if message is None:
        return False

    try:
        agent.handle(message.body)
    except Exception as error:
        logger.exception(
            'Agent failed on %s (attempt %d)', message.body.get('kind'), message.receive_count
        )
        queue.release(message, error=str(error))
        return True

    queue.delete(message.id)
    return True


def handle_batch(records: list[dict[str, Any]]) -> None:
    """The deployed entry point: SQS hands over a batch of message bodies.

    Kept alongside `process_once` so the port has an obvious landing spot — the
    local worker doesn't call this.
    """
    for record in records:
        agent.handle(record)
