"""Transcribe a Slack voice note.

Slack does auto-transcribe voice clips and exposes the result on files.info, but
only as a truncated `preview` with no documented way to fetch the full text — so a
30-second note about a prospect can silently lose its second half. We transcribe
the audio ourselves and keep Slack's preview only as a fallback for when the
download or the transcription call fails.

`gpt-transcribe` is the current high-accuracy model (whisper-1 still works but is
legacy) — worth having on a noisy expo floor.
"""

import io
import logging

from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_TRANSCRIBE_MODEL
from conventions import TRANSCRIPTION_VOCABULARY as _VOCABULARY_HINT

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)


def transcribe(audio_bytes: bytes, filename: str) -> str:
    """Return the transcript of a voice note. Raises if transcription fails."""

    def call(include_prompt: bool):
        # The codec is inferred from the filename, so keep Slack's extension.
        buffer = io.BytesIO(audio_bytes)
        buffer.name = filename or 'voice_note.m4a'
        kwargs = {'prompt': _VOCABULARY_HINT} if include_prompt else {}
        return client.audio.transcriptions.create(
            model=OPENAI_TRANSCRIBE_MODEL, file=buffer, **kwargs
        )

    try:
        result = call(include_prompt=True)
    except Exception as error:  # noqa: BLE001 — retried without the prompt below
        # Not every transcription model accepts `prompt`. Losing the vocabulary
        # hint is much better than losing the note.
        logger.warning(
            'Transcription with a vocabulary prompt failed (%s); retrying without', error
        )
        result = call(include_prompt=False)

    transcript = (result.text or '').strip()
    logger.info('Transcribed %d bytes of audio into %d chars', len(audio_bytes), len(transcript))
    return transcript


def slack_preview(file_info: dict) -> str | None:
    """Slack's own transcript preview — truncated, used only when Whisper fails."""
    transcription = (file_info or {}).get('transcription') or {}
    if transcription.get('status') != 'complete':
        return None
    content = ((transcription.get('preview') or {}).get('content') or '').strip()
    if not content:
        return None
    if (transcription.get('preview') or {}).get('has_more'):
        content += ' […transcript truncated by Slack]'
    return content


def is_audio(slack_file: dict) -> bool:
    """Whether a Slack attachment is a voice note.

    Slack's own recordings arrive with subtype `slack_audio`; a file the rep
    uploaded themselves is identified by mimetype alone.
    """
    return str(slack_file.get('mimetype', '')).startswith('audio/') or (
        slack_file.get('subtype') == 'slack_audio'
    )
