"""Environment configuration.

Local-only concern: the deployed form of this app reads secrets from AWS Secrets
Manager via `*_SECRET_ARN` env vars declared in `agents.json`, not from a .env
file. Keeping every secret read behind this module means that swap is one file.
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} is not set — populate it in .env')
    return value


def _optional(name: str, default: str = '') -> str:
    return os.environ.get(name, '').strip() or default


# ---------------------------------------------------------------------- Slack
SLACK_BOT_TOKEN = _required('SLACK_BOT_TOKEN')
# Verifies that a request genuinely came from Slack. Replaces the app-level
# token Socket Mode would have needed.
SLACK_SIGNING_SECRET = _required('SLACK_SIGNING_SECRET')

# --------------------------------------------------------------------- models
# One vendor for both the badge read and the transcription. Becomes Bedrock and
# Amazon Transcribe when this moves to the internal repo (README → Porting).
OPENAI_API_KEY = _required('OPENAI_API_KEY')
# Balanced default. gpt-5.6-luna is ~6x cheaper, gpt-5.6-sol is the flagship.
OPENAI_MODEL = _optional('OPENAI_MODEL', 'gpt-5.6-terra')
# whisper-1 still works but is legacy.
OPENAI_TRANSCRIBE_MODEL = _optional('OPENAI_TRANSCRIBE_MODEL', 'gpt-transcribe')

# ---------------------------------------------------------------------- Clay
CLAY_API_KEY = _required('CLAY_API_KEY')
CLAY_ROUTINE_ID = _required('CLAY_ROUTINE_ID')

# ------------------------------------------------------------------- Marketo
MARKETO_BASE_URL = _required('MARKETO_BASE_URL').rstrip('/')
MARKETO_CLIENT_ID = _required('MARKETO_CLIENT_ID')
MARKETO_CLIENT_SECRET = _required('MARKETO_CLIENT_SECRET')
# Where a human opens a lead, which is NOT the REST host above even though both
# belong to the same instance. It goes in the sheet's Marketo Lead column so
# Marketing Ops can click straight through to the person.
MARKETO_UI_HOST = _required('MARKETO_UI_HOST').rstrip('/')

# ------------------------------------------------------- Google Sheets (part 5)
GOOGLE_SHEET_ID = _required('GOOGLE_SHEET_ID')


def _service_account_info() -> dict:
    """Service account key, from a file path locally or inline JSON on deploy.

    Secrets Manager hands over the whole JSON blob as a string, so supporting both
    means the port is a change of env var, not of code.
    """
    inline = _optional('GOOGLE_SERVICE_ACCOUNT_JSON')
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as error:
            raise RuntimeError(f'GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {error}') from error

    path = os.path.expanduser(_required('GOOGLE_SERVICE_ACCOUNT_FILE'))
    # Resolve a relative path against the project root, not the shell's cwd, so
    # the server can be started from anywhere.
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.isfile(path):
        raise RuntimeError(f'GOOGLE_SERVICE_ACCOUNT_FILE not found: {path}')
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


GOOGLE_SERVICE_ACCOUNT_INFO = _service_account_info()

# ------------------------------------------------------------------- runtime
PORT = int(_optional('PORT', '3000'))
QUEUE_DB_PATH = _optional('QUEUE_DB_PATH', 'queue.db')

# Mirrors the `ingress.queue` block in agents.json — keep these in sync with it
# so local behaviour matches deployed behaviour.
QUEUE_VISIBILITY_TIMEOUT_SECONDS = int(_optional('QUEUE_VISIBILITY_TIMEOUT_SECONDS', '360'))
QUEUE_DLQ_MAX_RECEIVE_COUNT = int(_optional('QUEUE_DLQ_MAX_RECEIVE_COUNT', '3'))

CLAY_POLL_TIMEOUT_SECONDS = int(_optional('CLAY_POLL_TIMEOUT_SECONDS', '180'))
CLAY_POLL_INTERVAL_SECONDS = int(_optional('CLAY_POLL_INTERVAL_SECONDS', '5'))
