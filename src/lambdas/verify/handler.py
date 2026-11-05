"""Verify lambda — the only thing Slack talks to.

Mirrors `packages/slack-agent/src/lambdas/verify/index.ts`. Its whole job is to be
fast and cheap: check the signature, decide whether we care, put the work on the
queue, return 200. Slack gives us 3 seconds; nothing slow may happen here.

Captures arrive as an `app_mention` in the event's Slack channel — the channel is
named after the Marketo program, so the channel identifies where the lead goes.
Only app_mention is subscribed, so the app never sees ordinary channel traffic.

`handle()` takes a raw body and headers and returns `(status, body)` — no web
framework and no AWS types — so the same function serves Flask locally and an
API Gateway proxy event once deployed. Porting is a rewrite of the thin adapter
around it (and, in the deployed repo, a translation to TypeScript), not of the
logic.

There is no interactivity — no buttons and no modals — so Slack needs no
Interactivity Request URL and this handler only ever sees events. Everything the
agent does, it does conversationally.

Only `app_mention` is subscribed. The agent acts when it is tagged and not
otherwise — at that point it reads the whole thread to decide what to do, so nothing
said in between is lost, but nothing happens without being asked either.

The 👀 / 👂 reaction is added inline, before anything is queued, so the rep gets
instant feedback that their message landed.
"""

import json
import logging
from typing import Any

from config import SLACK_SIGNING_SECRET
from src.shared import slack_io
from src.shared.slack_signature import verify_slack_signature

logger = logging.getLogger(__name__)

MESSAGE_VERSION = 1


def handle(raw_body: str, headers: dict[str, str], queue: Any) -> tuple[int, str]:
    lowered = {key.lower(): value for key, value in headers.items()}
    signature = lowered.get('x-slack-signature', '')
    timestamp = lowered.get('x-slack-request-timestamp', '')

    if not verify_slack_signature(
        signing_secret=SLACK_SIGNING_SECRET,
        signature=signature,
        timestamp=timestamp,
        raw_body=raw_body,
    ):
        logger.warning('Invalid Slack signature')
        return 401, 'invalid signature'

    retry_num = lowered.get('x-slack-retry-num')
    if retry_num:
        # Queue deduplication makes this harmless, but it's worth seeing in logs:
        # a retry means we were too slow or returned non-200 last time.
        logger.warning('Slack retry #%s (%s)', retry_num, lowered.get('x-slack-retry-reason'))

    try:
        return _handle_event(raw_body, queue)
    except Exception:
        # A 500 makes Slack retry. Log loudly and accept, since a retry of a
        # malformed request will only fail again.
        logger.exception('Failed to process Slack request')
        return 200, 'error logged'


# ---------------------------------------------------------------- events API


def _handle_event(raw_body: str, queue: Any) -> tuple[int, str]:
    body = json.loads(raw_body)

    # One-time handshake when the Request URL is first saved in Slack.
    if body.get('type') == 'url_verification':
        logger.info('url_verification handshake')
        return 200, body.get('challenge', '')

    if body.get('type') != 'event_callback':
        return 200, 'ignored'

    inner = body.get('event') or {}

    # The agent acts only when tagged. Only app_mention is subscribed, so ordinary
    # channel and thread chatter never reaches this app — it is read from the thread
    # at the moment of a mention instead.
    if inner.get('type') != 'app_mention':
        return 200, 'ignored'
    # Only the agent's own messages, not every app's. A rep's message sent
    # through a workflow or a script carries a bot_id too, and dropping those
    # ignores a real person. One cached auth.test call per container.
    if slack_io.is_self(inner):
        return 200, 'ignored'
    if inner.get('subtype') not in (None, 'file_share'):
        return 200, 'ignored'

    channel_id = inner.get('channel', '')
    user_id = inner.get('user', '')
    event_ts = inner.get('ts', '')
    event_id = body.get('event_id') or event_ts
    files = inner.get('files') or []
    text = slack_io.strip_mentions(inner.get('text') or '')

    if not (user_id and channel_id):
        return 200, 'ignored'

    # A mention inside an existing thread continues that lead; a top-level mention
    # starts a new one and its own thread.
    thread_ts = inner.get('thread_ts') or event_ts

    has_image = any(str(f.get('mimetype', '')).startswith('image/') for f in files)
    slack_io.add_reaction(channel_id, event_ts, 'eyes' if has_image else 'ear')

    _send(
        queue,
        kind='mention',
        team_id=body.get('team_id', ''),
        channel_id=channel_id,
        user_id=user_id,
        payload={
            'files': files,
            'text': text,
            'thread_ts': thread_ts,
            'event_ts': event_ts,
        },
        dedupe_id=event_id,
    )
    return 200, 'ok'


# ------------------------------------------------------------------- enqueue


def _send(
    queue: Any,
    *,
    kind: str,
    team_id: str,
    channel_id: str,
    user_id: str,
    payload: dict[str, Any],
    dedupe_id: str,
) -> None:
    """Put one unit of work on the queue.

    The message group is the rep, not the channel: what has to stay ordered is
    one rep's own sequence — badge photo, then the note about that badge.
    """
    accepted = queue.send(
        message_group_id=user_id or channel_id or 'default',
        dedupe_id=dedupe_id,
        body={
            'version': MESSAGE_VERSION,
            'kind': kind,
            'team_id': team_id,
            'channel_id': channel_id,
            'user_id': user_id,
            'payload': payload,
        },
    )
    if accepted:
        logger.info('Enqueued %s for %s (%s)', kind, user_id, dedupe_id)
