"""Slack request signature verification.

Mirrors `src/shared/slack.ts` → `verifySlackSignature` in the internal-tooling
repo. Verification runs on the RAW request body — parse only after it passes, or
re-serialisation will change the bytes and the HMAC will never match.
"""

import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)

# Slack's recommended replay window.
MAX_TIMESTAMP_SKEW_SECONDS = 60 * 5


def verify_slack_signature(
    *, signing_secret: str, signature: str, timestamp: str, raw_body: str
) -> bool:
    if not signature or not timestamp:
        return False

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        logger.warning('Non-numeric Slack timestamp: %r', timestamp)
        return False

    if age > MAX_TIMESTAMP_SKEW_SECONDS:
        logger.warning('Rejecting Slack request %ds outside the replay window', age)
        return False

    expected = (
        'v0='
        + hmac.new(
            signing_secret.encode('utf-8'),
            f'v0:{timestamp}:{raw_body}'.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(expected, signature)
