"""Post the three standard test captures into a Slack channel.

Drives the agent the way a rep does — real messages, real uploads, real
mentions — so what it exercises is the whole path: Slack -> verify -> queue ->
worker -> badge reading, transcription, Clay, Marketo, the sheet.

    python scripts/send_test_messages.py C0123456789
    python scripts/send_test_messages.py C0123456789 --tests 2
    python scripts/send_test_messages.py C0123456789 --tests 1,3

It posts as YOU, not as the bot. The verify handler drops any mention carrying
`bot_id` (handler.py), which is what stops the agent replying to itself — so a
bot-token post would be accepted by Slack and then silently ignored. That is why
this needs a user token; see SLACK_USER_TOKEN below.

The three tests, and what each is for:

  1. Three badges in one message. The multi-lead path: three people, three rows,
     one Clay run. The case that used to return a single card.
  2. A badge posted with no mention, then a mention in the thread afterwards.
     Replicates a rep photographing a badge and only remembering the agent
     later; the photo must still be picked up.
  3. A badge and a voice note, with no typed notes at all. Transcription,
     and the rep's spoken words becoming the record.
"""

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

ROOT = Path(__file__).resolve().parent.parent
FILES = ROOT / 'test_files'

load_dotenv(ROOT / '.env')

BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
# A user token (xoxp-), not the bot token. Slack app > OAuth & Permissions >
# User Token Scopes: chat:write + files:write, then reinstall and copy the
# token that starts xoxp-. Posting as the bot would be ignored by the agent.
USER_TOKEN = os.environ.get('SLACK_USER_TOKEN', '')

# No spacing by default: the queue is FIFO per rep, so three captures sent at
# once are processed one at a time anyway. Replies from different tests will
# interleave in the channel; --gap spaces them out when that matters.
DEFAULT_GAP_SECONDS = 0


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('channel', help="the test channel, e.g. '#test-event' or its id")
    parser.add_argument(
        '--tests',
        default='all',
        help="which tests to run: 'all' (default), or numbers like '2' or '1,3'",
    )
    parser.add_argument(
        '--gap',
        type=int,
        default=DEFAULT_GAP_SECONDS,
        help=f'seconds to wait between tests (default {DEFAULT_GAP_SECONDS})',
    )
    args = parser.parse_args()

    problems = _preflight()
    if problems:
        for problem in problems:
            print(f'  ✗ {problem}')
        return 1

    bot = WebClient(token=BOT_TOKEN)
    me = WebClient(token=USER_TOKEN)
    # A big workspace has thousands of channels, and conversations.list is
    # rate limited hard enough to fail the name lookup outright. Sleep and
    # retry on 429 rather than making the caller find the channel id.
    for client in (bot, me):
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))

    agent_id = bot.auth_test()['user_id']
    channel_id = _resolve_channel(bot, args.channel)
    print(f'Channel {args.channel} -> {channel_id}; tagging <@{agent_id}>\n')

    tests = [_test_one, _test_two, _test_three]
    try:
        wanted = _parse_tests(args.tests, len(tests))
    except ValueError as error:
        print(f'  ✗ {error}')
        return 1
    chosen = [tests[number - 1] for number in wanted]

    for index, test in enumerate(chosen):
        test(me, bot, channel_id, agent_id)
        if args.gap and index < len(chosen) - 1:
            print(f'  … waiting {args.gap}s for the agent to finish\n')
            time.sleep(args.gap)

    print('\nAll sent. Watch the channel — and `tail -f` the run_local.py output.')
    return 0


def _parse_tests(value: str, count: int) -> list[int]:
    """'all', or a comma-separated list of test numbers, in the order given.

    Duplicates are dropped rather than run twice — asking for '2,2' is a typo,
    and each run posts real messages.
    """
    if value.strip().lower() == 'all':
        return list(range(1, count + 1))
    wanted = []
    for part in value.split(','):
        part = part.strip()
        if not part.isdigit() or not 1 <= int(part) <= count:
            raise ValueError(f'{part!r} is not a test number. Use "all", or 1-{count}.')
        if int(part) not in wanted:
            wanted.append(int(part))
    if not wanted:
        raise ValueError(f'No tests selected. Use "all", or numbers 1-{count}.')
    return wanted


def _preflight() -> list[str]:
    """Every reason this cannot work, at once — not one per run."""
    problems = []
    if not BOT_TOKEN:
        problems.append('SLACK_BOT_TOKEN is not set in .env')
    if not USER_TOKEN:
        problems.append(
            'SLACK_USER_TOKEN is not set in .env. It must be a user token (xoxp-), '
            'because the agent ignores mentions posted by a bot. '
            'Slack app > OAuth & Permissions > User Token Scopes: chat:write, files:write.'
        )
    elif not USER_TOKEN.startswith('xoxp-'):
        problems.append(
            f'SLACK_USER_TOKEN starts {USER_TOKEN[:5]!r}, not "xoxp-". A bot token here '
            'posts messages the agent will deliberately ignore.'
        )
    for stem in (
        'knak_badge_pierce',
        'knak_badge_brendan',
        'knak_badge_nick',
        'knak_badge_danel',
        'knak_badge_tyron',
        'production_bottleneck_recording',
    ):
        if _find(stem) is None:
            problems.append(f'missing test file: test_files/{stem}.*')
    return problems


def _find(stem: str) -> Path | None:
    """The file for a stem, whatever extension it carries.

    Badge photos get re-exported and recompressed — .jpeg becomes .jpg — and a
    hardcoded extension turns that into a failed run for no real reason.
    """
    for path in sorted(FILES.glob(f'{stem}.*')):
        if path.is_file():
            return path
    return None


def _resolve_channel(client: WebClient, channel: str) -> str:
    """Accept '#name', 'name' or an id. Slack's API wants the id."""
    if channel.startswith(('C', 'G')) and ' ' not in channel:
        return channel
    wanted = channel.lstrip('#')
    cursor = None
    while True:
        page = client.conversations_list(
            types='public_channel,private_channel',
            exclude_archived=True,
            limit=1000,
            cursor=cursor,
        )
        for found in page['channels']:
            if found['name'] == wanted:
                return found['id']
        cursor = (page.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            raise SystemExit(
                f'No channel named {wanted!r} that the bot can see. '
                'Invite the bot to it first — being in the channel is the only gate.'
            )


def _upload(
    client: WebClient,
    reader: WebClient,
    channel_id: str,
    stems,
    comment: str,
    thread_ts=None,
    need_ts: bool = False,
) -> str:
    """Post one message carrying files, and return its ts.

    file_uploads posts a single message for the whole batch, which is what makes
    three badges one capture rather than three.
    """
    response = client.files_upload_v2(
        channel=channel_id,
        initial_comment=comment,
        thread_ts=thread_ts,
        file_uploads=[{'file': str(_find(stem)), 'title': _find(stem).name} for stem in stems],
    )
    first = (response.get('files') or [{}])[0]
    # The share is read back with the BOT client: files.info needs files:read,
    # and the user token only carries chat:write and files:write.
    return _share_ts(first, channel_id) or _share_ts_via_info(reader, first.get('id'), channel_id)


def _share_ts(file_obj: dict, channel_id: str) -> str:
    for scope in ('public', 'private'):
        for shared in ((file_obj.get('shares') or {}).get(scope) or {}).get(channel_id, []):
            return shared['ts']
    return ''


def _share_ts_via_info(client: WebClient, file_id: str | None, channel_id: str) -> str:
    """Ask Slack again for where the file was shared.

    The upload response is built before the share has finished registering, so
    its `shares` is often empty — which is not a failure, just early. Test 2
    needs the ts to reply in thread, so poll rather than give up on it.
    """
    if not file_id:
        return ''
    for _ in range(6):
        time.sleep(1)
        try:
            ts = _share_ts(client.files_info(file=file_id)['file'], channel_id)
        except SlackApiError:
            continue
        if ts:
            return ts
    return ''


def _test_one(client: WebClient, reader: WebClient, channel_id: str, agent_id: str) -> None:
    print('Test 1 — three badges in one message')
    _upload(
        client,
        reader,
        channel_id,
        ('knak_badge_pierce', 'knak_badge_brendan', 'knak_badge_nick'),
        f'<@{agent_id}> Use dynamics, not a fan of the building process\n\n'
        'Asked if we have a direct integration but informed them of open API '
        'and ability to download and upload HTML',
    )
    print('  sent: 3 badges + notes, tagged')


def _test_two(client: WebClient, reader: WebClient, channel_id: str, agent_id: str) -> None:
    print('Test 2 — badge first, mention afterwards')
    # Deliberately no mention: the agent must not act yet.
    parent_ts = _upload(
        client,
        reader,
        channel_id,
        ('knak_badge_danel',),
        'Small team of 5 marketers currently using Pardot. They are not the '
        'decision maker but said it’s worth reaching out. They expressed the '
        'teams frustration with the Pardot editor',
        need_ts=True,
    )
    if not parent_ts:
        print('  ✗ could not read the message ts; skipping the follow-up')
        return
    print('  sent: badge + notes, NOT tagged')
    time.sleep(5)
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=parent_ts,
        text=f'<@{agent_id}> can you grab this one',
    )
    print('  sent: threaded mention — the badge above must still be picked up')


def _test_three(client: WebClient, reader: WebClient, channel_id: str, agent_id: str) -> None:
    print('Test 3 — voice note')
    _upload(
        client,
        reader,
        channel_id,
        ('production_bottleneck_recording', 'knak_badge_tyron'),
        f'<@{agent_id}>',
    )
    print('  sent: badge + m4a, bare mention')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SlackApiError as error:
        print(f'Slack refused the call: {error.response.get("error")}')
        sys.exit(1)
