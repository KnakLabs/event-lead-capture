"""Slack image attachments, prepared for the model to look at.

The model reads the badges itself and calls `start_lead` once per person, so
what this produces is picture input, not extracted text. That is what makes
three badges in one message three leads: an OCR step that returned one set of
fields could only ever describe one of them.

Photos are downscaled first. A phone camera produces 3000px+ images several
megabytes each, and a capture can carry several — sending them untouched is slow
and buys no accuracy, since a badge is legible long before that resolution.

Nothing here is fatal. A photo that will not download or decode is reported and
the turn carries on with the ones that did, because the notes and the other
badges are still worth filing.
"""

import base64
import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)

# Long edge, in pixels. Comfortably above what badge text needs.
MAX_IMAGE_EDGE = 1600

# Formats the API accepts as-is. Anything else (HEIC, TIFF) is re-encoded.
_NATIVE_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')


def to_image_parts(files: list[dict], download, notices: list[str]) -> list[dict]:
    """Slack file dicts -> OpenAI `input_image` parts, in the order given."""
    parts = []
    for slack_file in files:
        name = slack_file.get('name') or 'photo'
        try:
            raw = download(slack_file)
        except Exception as error:  # noqa: BLE001 — one bad download must not lose the turn
            logger.warning('Could not download %s: %s', name, error)
            notices.append(f"I couldn't open the photo {name}. Ask them to post that one again.")
            continue
        data, mimetype = shrink(raw, slack_file.get('mimetype') or 'image/jpeg')
        encoded = base64.b64encode(data).decode('ascii')
        parts.append({'type': 'input_image', 'image_url': f'data:{mimetype};base64,{encoded}'})
    return parts


def shrink(image_bytes: bytes, mimetype: str) -> tuple[bytes, str]:
    """Downscale an oversized photo, and normalise formats the API won't take.

    Failure is never fatal — an image that cannot be decoded is sent as-is and
    the model decides, rather than losing the capture over a resize.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as error:  # noqa: BLE001 — send the original rather than fail
        logger.warning('Could not decode the image (%s); sending it as-is', error)
        return image_bytes, mimetype

    longest = max(image.size)
    needs_resize = longest > MAX_IMAGE_EDGE
    needs_convert = (image.format or '').upper() not in _NATIVE_FORMATS
    if not (needs_resize or needs_convert):
        return image_bytes, mimetype

    if needs_resize:
        scale = MAX_IMAGE_EDGE / longest
        image = image.resize((int(image.width * scale), int(image.height * scale)))
        logger.info('Resized photo from %dpx to %dpx on the long edge', longest, max(image.size))

    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=88)
    return buffer.getvalue(), 'image/jpeg'
