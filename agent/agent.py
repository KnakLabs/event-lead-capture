"""The agent runtime — everything slow enough to belong behind the queue.

Captures happen in a Slack channel named after the Marketo event program: a rep
@mentions the bot with a badge photo and the agent replies in a thread. It acts
only when tagged — but each time it is, it reads the whole thread, so anything
said in between is picked up, including photos posted before the rep remembered
to tag it.

It finishes the job on the turn it is tagged: rows created, emails found, leads
pushed, then one short message saying what happened. A rep mid-conversation
cannot answer a confirmation question, so corrections come afterwards by tagging
it again (`agent/system_prompt.py`).

A thread holds one row per prospect — three badges photographed together are
three people — so the model reads the photos itself and calls `start_lead` once
each. That is why the badges arrive as pictures rather than pre-extracted
fields: one extraction step could only ever describe one of them.

Two rules the model does not own, enforced in `agent/lead.py` so they hold every
time: a lead reaches Marketo only with an email, and a name already in the
thread is never filed twice.

Only two things write to Slack — the cards, and the model's `reply_to_rep`.
Every other tool returns its result to the model and says nothing itself, so the
agent speaks with one voice.

Lead state lives in the event's Google Sheet tab, not in a database — see
`agent/store.py`. Every step writes the row it already owns, so the sheet is
both the working store and the record Marketing Ops reads.

Message contract (`src/lambdas/verify/handler.py` produces these):

    {"version": 1, "kind": "...", "team_id", "channel_id", "user_id", "payload": {...}}
"""

import logging
from typing import Any

from agent import attachments, brain, lead, marketo, thread_files, transcribe
from src.shared import slack_io

logger = logging.getLogger(__name__)


def handle(body: dict[str, Any]) -> None:
    """Entry point. Raising propagates to the queue, which retries then DLQs."""
    kind = body.get('kind')
    handler = _HANDLERS.get(kind)
    if handler is None:
        logger.warning('No handler for message kind %r', kind)
        return
    logger.info('Handling %s for user %s', kind, body.get('user_id'))
    handler(body)


def _on_mention(body: dict[str, Any]) -> None:
    """Every mention lands here, and every mention reaches the model.

    The deterministic work happens first because it is cheap and unambiguous —
    resolve the channel to a program, gather the photos, transcribe voice notes.
    Anything that *didn't* resolve is described to the model rather than turned
    into a canned reply, so it can search for the right program, ask for what it
    needs, or just answer the person. There are no dead ends.
    """
    payload = body['payload']
    channel_id, user_id = body['channel_id'], body['user_id']
    thread_ts = payload['thread_ts']
    files = payload.get('files') or []

    notices: list[str] = []
    channel = _channel_name(channel_id, notices)
    program = marketo.cache.for_channel(channel) if channel else None
    capture = lead.Capture(channel_id, thread_ts, user_id, program)

    thread = _thread_for_model(channel_id, thread_ts, files, notices)
    images = attachments.to_image_parts(
        thread_files.images_in_play(
            files, _thread_files(channel_id, thread_ts), transcribe.is_audio, notices
        ),
        slack_io.download_file,
        notices,
    )

    pending: list[str] = []
    tools = {fn.__name__: fn for fn in lead.build_tools(capture)}
    tools['reply_to_rep'] = lambda message: _remember(pending, message)

    logger.info(
        'Mention in #%s (program=%s, images=%d, thread=%d)',
        channel or '?',
        program.name if program else None,
        len(images),
        len(thread),
    )
    try:
        brain.interpret(
            situation=lead.describe(capture, channel, notices),
            thread=thread,
            tools=tools,
            images=images,
        )
    except Exception:
        logger.exception('The model turn failed')
        pending.append(
            "Something went wrong on my side and I couldn't finish that — tag me "
            'again to pick it up.'
        )

    # Cards first, so the reply's summary lands under the detail it refers to. A
    # card failure must not lose the reply.
    try:
        for who, why in lead.render_cards(capture, slack_io.client):
            logger.error('Card render failed for %s: %s', who, why)
    except Exception:
        logger.exception('Card render failed')

    if not pending:
        # Every turn has to end with something said: from the rep's side, silence
        # is indistinguishable from the agent being broken.
        pending.append("I didn't manage to do anything with that — tag me again?")
    for message in pending:
        slack_io.post_message(channel_id, message, thread_ts=thread_ts)


def _remember(pending: list[str], message: str) -> str:
    """`reply_to_rep`'s body. Held until the cards are up rather than posted now."""
    pending.append(message)
    return 'Told them.'


def _channel_name(channel_id: str, notices: list[str]) -> str:
    """The channel's name, which is what identifies the Marketo program."""
    try:
        return slack_io.channel_name(channel_id)
    except Exception as error:  # noqa: BLE001 — the model can still answer and explain
        logger.error('Could not read the channel name for %s: %s', channel_id, error)
        notices.append(
            "I can't read this channel's name — the app may be missing the "
            'channels:read scope, or not be a member of the channel. Without it I '
            "can't tell which Marketo event this is."
        )
        return ''


def _thread_messages(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    try:
        return slack_io.thread_messages(channel_id, thread_ts)
    except Exception as error:  # noqa: BLE001 — a turn on one message beats no turn
        logger.error('Could not read thread %s: %s', thread_ts, error)
        return []


def _thread_files(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """Earlier messages, shaped the way `thread_files` expects."""
    return [
        {'self': slack_io.is_self(message), 'files': message.get('files') or []}
        for message in _thread_messages(channel_id, thread_ts)
    ]


def _thread_for_model(
    channel_id: str, thread_ts: str, files: list[dict[str, Any]], notices: list[str]
) -> list[dict[str, str]]:
    """The thread as the model sees it, oldest first.

    Reading the whole thread rather than just the triggering message is what
    makes the realistic flow work: drop the badge photo, get pulled into a
    conversation, come back later and type what you remember. None of those
    follow-ups has to tag the bot — they are all read the next time one does.
    """
    thread: list[dict[str, str]] = []
    transcribed = {f.get('id') for f in files}

    for message in _thread_messages(channel_id, thread_ts):
        text = slack_io.strip_mentions(message.get('text') or '')
        if slack_io.is_self(message):
            # The agent's own messages stay in, labelled. They are the only record
            # of what it already told the rep, so without them it repeats itself.
            if text:
                thread.append({'who': 'you (earlier)', 'text': text})
            continue
        if text:
            thread.append({'who': 'salesperson', 'text': text})

        for attachment in message.get('files') or []:
            if not transcribe.is_audio(attachment) or attachment.get('id') in transcribed:
                continue
            # A voice note from an earlier turn was transcribed into the row when
            # it arrived, so naming it is enough — transcribing it again every
            # turn would be pure cost for nothing new.
            thread.append(
                {'who': 'system', 'text': '(a voice note here was handled on an earlier turn)'}
            )

    for voice_note in [f for f in files if transcribe.is_audio(f)]:
        thread.append({'who': 'salesperson (voice note)', 'text': _transcribe(voice_note, notices)})
    return thread


def _transcribe(voice_note: dict[str, Any], notices: list[str]) -> str:
    """This turn's audio, as the rep's own words. A failure is a notice, not a
    dead end — the model says the clip didn't come through and carries on."""
    name = voice_note.get('name', '')
    try:
        return transcribe.transcribe(slack_io.download_file(voice_note), name)
    except Exception as error:  # noqa: BLE001 — a lost clip must not lose the badge
        logger.warning('Transcription failed: %s', error)
        preview = transcribe.slack_preview(slack_io.file_info(voice_note.get('id') or '')) or ''
        if preview:
            return preview
        notices.append(
            f"A voice note ({name or 'unnamed'}) wouldn't transcribe. Ask them to "
            'type what they said.'
        )
        return "(this voice note couldn't be transcribed)"


_HANDLERS = {'mention': _on_mention}
