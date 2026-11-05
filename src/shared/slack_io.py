"""Thin Slack Web API wrapper shared by the ingress lambdas and the agent.

Mirrors `src/shared/slack.ts`. Everything that talks to Slack goes through here so
the deployed version only has to change where the token comes from.
"""

import logging
import re
from typing import Any

import requests
from slack_sdk import WebClient

from config import SLACK_BOT_TOKEN

logger = logging.getLogger(__name__)

client = WebClient(token=SLACK_BOT_TOKEN)


def download_file(file_obj: dict[str, Any]) -> bytes:
    """Fetch a private Slack file. Needs the bot token as a bearer credential."""
    url = file_obj.get('url_private_download') or file_obj['url_private']
    response = requests.get(url, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}, timeout=60)
    response.raise_for_status()
    return response.content


def file_info(file_id: str) -> dict[str, Any]:
    return client.files_info(file=file_id).get('file') or {}


def channel_name(channel_id: str) -> str:
    """The channel's name, which is what identifies the Marketo program.

    Slack events carry only the channel id, so this lookup is unavoidable. Cached
    for the process: renaming an event channel mid-event isn't a thing.
    """
    if channel_id in _channel_names:
        return _channel_names[channel_id]
    info = client.conversations_info(channel=channel_id).get('channel') or {}
    name = info.get('name') or ''
    if name:
        _channel_names[channel_id] = name
    return name


_channel_names: dict[str, str] = {}


def post_message(
    channel: str, text: str, blocks: list | None = None, thread_ts: str | None = None
) -> str:
    response = client.chat_postMessage(
        channel=channel, text=text, blocks=blocks, thread_ts=thread_ts
    )
    return response['ts']


def update_message(channel: str, ts: str, text: str, blocks: list | None = None) -> None:
    client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)


def add_reaction(channel: str, ts: str, emoji: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=emoji)
    except Exception as error:  # noqa: BLE001 — a reaction is not worth a failed capture
        # A duplicate reaction is not worth failing a capture over.
        logger.debug('Could not add :%s:: %s', emoji, error)


_MENTION_RE = re.compile(r'<@[A-Z0-9]+>')


def strip_mentions(text: str) -> str:
    """Drop `<@U123>` tokens, keeping whatever the person actually wrote."""
    return _MENTION_RE.sub(' ', text or '').strip()


def thread_messages(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """Every message in a thread, oldest first.

    This is what lets a rep drop the badge photo, walk away, and come back later to
    type notes without having to remember to tag the bot again — when they do tag it
    (or press a button), the whole conversation is read, not just the last message.

    Needs `channels:history` / `groups:history`. Paginates because a chatty thread
    can exceed one page.
    """
    messages: list[dict[str, Any]] = []
    cursor = None
    for _ in range(10):  # 10 x 200 is far more than any lead thread
        response = client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=200, cursor=cursor
        )
        messages.extend(response.get('messages') or [])
        cursor = (response.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            break
    return messages


_self_user_id: str | None = None


def self_user_id() -> str:
    """This app's own bot user id, looked up once per process.

    Empty when the lookup failed, which callers read as "identity unknown".
    """
    global _self_user_id
    if _self_user_id is None:
        try:
            _self_user_id = client.auth_test()['user_id']
        except Exception:
            logger.exception('auth.test failed; falling back to treating any app post as self')
            _self_user_id = ''
    return _self_user_id


def is_self(message: dict[str, Any]) -> bool:
    """True only for messages this agent posted itself.

    Identity, not "was posted by a bot". Slack stamps a `bot_id` on anything
    sent through an app's token, including a message a person wrote and sent
    via a workflow, a scheduled post or a script — so the broader test silently
    ignores real people. The author's user id is the same on the agent's own
    messages and on nothing else.
    """
    own = self_user_id()
    if not own:
        # Identity unknown. Fall back to the broad test: answering a person late
        # is recoverable, answering itself in a loop is not.
        return bool(
            message.get('bot_id')
            or message.get('app_id')
            or message.get('subtype') == 'bot_message'
        )
    return message.get('user') == own
