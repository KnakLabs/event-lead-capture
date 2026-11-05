"""Ingress — signature verification, event filtering, and the queue's semantics.

This is the half of the system the deployed copy does not share: there an API
Gateway, two Lambdas and SQS FIFO do this job, and here it is Flask plus a SQLite
queue that reproduces the same four behaviours — message groups, deduplication,
visibility timeout and dead-lettering.

Covers the things that would silently break a capture: a forged or replayed
request getting through, an event that should have been ignored waking the
agent, a Slack retry filing the same lead twice, and one rep's slow capture
blocking another's.

No network and no real credentials.
"""

import hashlib
import hmac
import json
import tempfile
import time

import pytest

from config import SLACK_SIGNING_SECRET
from src.lambdas.verify.handler import handle
from src.shared import slack_io
from src.shared.taskqueue import TaskQueue
from tests.conftest import AGENT_USER_ID


def sign(raw_body: str, timestamp: int | None = None) -> dict[str, str]:
    timestamp = timestamp if timestamp is not None else int(time.time())
    signature = (
        'v0='
        + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            f'v0:{timestamp}:{raw_body}'.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return {
        'X-Slack-Signature': signature,
        'X-Slack-Request-Timestamp': str(timestamp),
        'Content-Type': 'application/json',
    }


def mention(
    text: str,
    event_id: str,
    user: str = 'U_REP1',
    channel: str = 'C_EVENT',
    thread_ts: str | None = None,
    files: list | None = None,
    ts: str = '1700000000.000100',
) -> str:
    """An app_mention in an event channel — the only event this app subscribes to."""
    event = {
        'type': 'app_mention',
        'channel': channel,
        'user': user,
        'ts': ts,
        'text': f'<@UBOT123> {text}'.strip(),
    }
    if thread_ts:
        event['thread_ts'] = thread_ts
    if files:
        event['files'] = files
    return json.dumps(
        {'type': 'event_callback', 'team_id': 'T1', 'event_id': event_id, 'event': event}
    )


def channel_message(event_id: str, **event) -> str:
    """A plain message in the channel — never a trigger, whatever it says."""
    return json.dumps(
        {
            'type': 'event_callback',
            'team_id': 'T1',
            'event_id': event_id,
            'event': {'type': 'message', 'channel_type': 'channel', 'channel': 'C_EVENT', **event},
        }
    )


@pytest.fixture
def queue() -> TaskQueue:
    return TaskQueue(
        tempfile.mktemp(suffix='.db'), visibility_timeout_seconds=5, dlq_max_receive_count=3
    )


# ------------------------------------------------------ 1. proving it is Slack


def test_a_validly_signed_request_is_accepted(queue):
    body = mention('hello', 'Ev1')
    status, _ = handle(body, sign(body), queue)
    assert status == 200


def test_a_forged_signature_is_rejected(queue):
    body = mention('hello', 'Ev1')
    status, _ = handle(body, {**sign(body), 'X-Slack-Signature': 'v0=deadbeef'}, queue)
    assert status == 401


def test_a_replayed_request_is_rejected(queue):
    """Ten minutes old. A correct signature is not enough on its own."""
    body = mention('hello', 'Ev1')
    status, _ = handle(body, sign(body, timestamp=int(time.time()) - 600), queue)
    assert status == 401


def test_a_tampered_body_is_rejected(queue):
    body = mention('hello', 'Ev1')
    tampered = body.replace('hello', 'goodbye')
    status, _ = handle(tampered, sign(body), queue)
    assert status == 401


def test_the_url_verification_handshake_is_echoed(queue):
    body = json.dumps({'type': 'url_verification', 'challenge': 'abc123'})
    status, response = handle(body, sign(body), queue)
    assert (status, response) == (200, 'abc123')


# -------------------------------------------------- 2. what wakes the agent


def test_the_agents_own_message_is_not_enqueued(queue):
    """The loop guard: without it the agent answers its own replies forever."""
    body = json.dumps(
        {
            'type': 'event_callback',
            'team_id': 'T1',
            'event_id': 'EvBot',
            'event': {
                'type': 'app_mention',
                'channel': 'C_EVENT',
                'user': AGENT_USER_ID,
                'bot_id': 'B_SELF',
                'ts': '1.1',
                'text': '<@UBOT123> from me',
            },
        }
    )
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 0


def test_a_persons_message_sent_through_another_app_is_enqueued(queue):
    """Slack stamps a bot_id on anything posted through an app's token.

    A rep tagging the agent from a workflow, a scheduled message or a test
    script is still a person asking for something, and ignoring them made the
    agent look broken while the message sat there tagged.
    """
    body = json.dumps(
        {
            'type': 'event_callback',
            'team_id': 'T1',
            'event_id': 'EvViaApp',
            'event': {
                'type': 'app_mention',
                'channel': 'C_EVENT',
                'user': 'U_REP1',
                'bot_id': 'B_OTHER_APP',
                'app_id': 'A_OTHER_APP',
                'ts': '1.2',
                'text': '<@UBOT123> can you grab this one',
            },
        }
    )
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 1


def test_any_app_post_is_ignored_when_the_agents_identity_is_unknown(monkeypatch, queue):
    """auth.test failing must not open the door to a reply loop."""
    monkeypatch.setattr(slack_io, 'self_user_id', lambda: '')
    body = json.dumps(
        {
            'type': 'event_callback',
            'team_id': 'T1',
            'event_id': 'EvUnknown',
            'event': {
                'type': 'app_mention',
                'channel': 'C_EVENT',
                'user': 'U_REP1',
                'bot_id': 'B_OTHER_APP',
                'ts': '1.3',
                'text': '<@UBOT123> hello',
            },
        }
    )
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 0


def test_ordinary_channel_chatter_is_ignored(queue):
    body = channel_message('EvPlain', user='U1', ts='1.1', text='just chatting')
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 0


def test_an_untagged_thread_reply_is_ignored(queue):
    """It is still read — from the thread — the next time someone does tag."""
    body = channel_message(
        'EvReply',
        user='U_REP1',
        ts='1700000005.5',
        thread_ts='1700000000.000100',
        text='he wants a demo in September',
    )
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 0


def test_a_tagged_reply_does_trigger(queue):
    body = mention('yes send it', 'EvTagged', thread_ts='1700000000.000100', ts='1700000010.1')
    handle(body, sign(body), queue)
    assert queue.stats()['queued'] == 1


def test_a_slack_retry_is_enqueued_once(queue):
    """Slack retries on a slow ack; filing the same lead twice is the failure."""
    body = mention('met Sarah at the booth', 'Ev42')
    handle(body, sign(body), queue)
    handle(body, {**sign(body), 'X-Slack-Retry-Num': '1'}, queue)
    assert queue.stats()['queued'] == 1


# ------------------------------------------------------ 3. the message shape


def test_the_mention_carries_what_the_agent_needs(queue):
    body = mention(
        'met Sarah at the booth',
        'EvMention',
        files=[{'mimetype': 'image/jpeg', 'id': 'F1', 'name': 'badge.jpg'}],
    )
    handle(body, sign(body), queue)
    message = queue.receive()

    assert message is not None
    assert message.body['kind'] == 'mention'
    # The mention token is stripped so it never reads as part of the notes.
    assert message.body['payload']['text'] == 'met Sarah at the booth'
    # The channel id is what identifies the Marketo program.
    assert message.body['channel_id'] == 'C_EVENT'
    assert message.body['payload']['files'][0]['id'] == 'F1'
    # A top-level mention threads on its own ts.
    assert message.body['payload']['thread_ts'] == '1700000000.000100'


def test_a_thread_reply_joins_the_same_thread(queue):
    body = mention(
        'also wants pricing', 'EvThread', thread_ts='1700000000.000100', ts='1700000009.000999'
    )
    handle(body, sign(body), queue)
    message = queue.receive()
    assert message is not None
    assert message.body['payload']['thread_ts'] == '1700000000.000100'


# ---------------------------------------------------- 4. queue semantics


def test_one_reps_messages_stay_in_order(queue):
    for index, text in enumerate(['first', 'second', 'third']):
        body = mention(text, f'EvSeq{index}')
        handle(body, sign(body), queue)

    first = queue.receive()
    assert first is not None and first.body['payload']['text'] == 'first'
    # The same rep's next message waits until that one is acked, so a badge and
    # the note about it cannot be processed out of order.
    assert queue.receive() is None

    queue.delete(first.id)
    second = queue.receive()
    assert second is not None and second.body['payload']['text'] == 'second'


def test_a_second_rep_is_not_blocked_by_the_first(queue):
    body = mention('first', 'EvSeq0')
    handle(body, sign(body), queue)
    queue.receive()

    other = mention('other rep', 'EvOther', user='U_REP2')
    handle(other, sign(other), queue)
    concurrent = queue.receive()

    assert concurrent is not None and concurrent.body['user_id'] == 'U_REP2'


def test_an_abandoned_message_is_returned_when_its_lease_expires():
    queue = TaskQueue(
        tempfile.mktemp(suffix='.db'), visibility_timeout_seconds=1, dlq_max_receive_count=3
    )
    body = mention('will fail', 'EvFail')
    handle(body, sign(body), queue)

    claimed = queue.receive()
    assert claimed is not None and claimed.receive_count == 1

    time.sleep(1.2)
    recovered = queue.receive()
    assert recovered is not None and recovered.receive_count == 2


def test_a_message_that_keeps_failing_is_dead_lettered():
    queue = TaskQueue(
        tempfile.mktemp(suffix='.db'), visibility_timeout_seconds=1, dlq_max_receive_count=3
    )
    body = mention('will fail', 'EvFail')
    handle(body, sign(body), queue)

    for _ in range(3):
        message = queue.receive()
        assert message is not None
        queue.release(message, error='boom')

    assert queue.stats()['dead_letters'] == 1
    assert queue.stats()['queued'] == 0
