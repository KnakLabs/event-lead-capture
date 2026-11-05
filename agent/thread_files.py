"""Which attachments the model is shown on a turn.

Reps forget to tag the agent. The badge photo and the notes go in first, the
mention arrives a message or two later, and the photo is the whole point of the
turn — so images are gathered from the whole thread, not just the message that
triggered it. Before this the runtime knew only that *some* file existed and told
the rep it had no badge to read, which made a forgotten tag unrecoverable.

Audio is deliberately not gathered this way: a past voice note was transcribed
into its row on the turn it arrived, and re-transcribing it every turn would be
pure cost for no new information.

Kept separate from `agent.py` so it can be tested without the AgentCore runtime.
"""

from collections.abc import Callable
from typing import Any

# Images the model is shown in one turn, across this message and the thread.
# A rep photographs a few badges at a time; beyond this the turn gets slow and
# the extra photos are almost certainly ones already filed on an earlier turn.
MAX_TURN_IMAGES = 8


def images_in_play(
    files: list[dict[str, Any]],
    messages: Any,
    is_audio: Callable[[dict[str, Any]], bool],
    notices: list[str],
) -> list[dict[str, Any]]:
    """Every non-audio attachment to show this turn: this message's first, then
    earlier ones in the thread, newest first, deduped by file id.

    Re-reading a badge already filed is harmless — `start_lead` refuses a name
    the thread already holds — so this errs towards showing a photo again rather
    than risking never showing it at all.
    """
    thread_files = [
        f
        for m in (messages if isinstance(messages, list) else [])
        if isinstance(m, dict) and not m.get('self')
        for f in (m.get('files') or [])
        if isinstance(f, dict)
    ]

    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for candidate in [*files, *reversed(thread_files)]:
        if is_audio(candidate):
            continue
        key = candidate.get('id') or candidate.get('urlPrivateDownload') or ''
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)

    if len(ordered) > MAX_TURN_IMAGES:
        notices.append(
            f'This thread holds {len(ordered)} attachments and you are being shown the '
            f'{MAX_TURN_IMAGES} most recent. If a badge seems to be missing, say so and ask '
            'them to re-post that one.'
        )
        ordered = ordered[:MAX_TURN_IMAGES]
    return ordered
