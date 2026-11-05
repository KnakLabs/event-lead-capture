"""Test setup: put the repo root on `sys.path` and give `config` something to read.

The suite is hermetic — no network, no Google, no Marketo, no OpenAI. But
`config` validates its environment at import and the agent modules import it, so
the variables have to exist before anything else loads. Loaded during collection,
so a bare `pytest` from the repo root works.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault('SLACK_BOT_TOKEN', 'xoxb-test')
os.environ.setdefault('SLACK_SIGNING_SECRET', 'test-signing-secret')
os.environ.setdefault('OPENAI_API_KEY', 'test')
os.environ.setdefault('CLAY_API_KEY', 'test')
os.environ.setdefault('CLAY_ROUTINE_ID', 'function:t_test')
os.environ.setdefault('MARKETO_BASE_URL', 'https://test.mktorest.com')
os.environ.setdefault('MARKETO_CLIENT_ID', 'test')
os.environ.setdefault('MARKETO_CLIENT_SECRET', 'test')
os.environ.setdefault('MARKETO_UI_HOST', 'https://app-test.marketo.com')
os.environ.setdefault('GOOGLE_SHEET_ID', 'sheet-test')
os.environ.setdefault('GOOGLE_SERVICE_ACCOUNT_JSON', '{"type":"service_account","project_id":"p"}')


import pytest  # noqa: E402  (config's environment must exist before this import)

from src.shared import slack_io  # noqa: E402

# The identity the agent answers to in tests. `is_self` compares against it, so
# a message from this user is the agent's own and anything else is a person's.
AGENT_USER_ID = 'UBOT123'


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep the suite hermetic.

    Both of these reach Slack when left alone: the ingress acknowledges every
    mention with a reaction, and `is_self` asks auth.test who it is. With a test
    token they fail slowly rather than loudly, which is worse than not calling.
    """
    monkeypatch.setattr(slack_io, 'self_user_id', lambda: AGENT_USER_ID)
    monkeypatch.setattr(slack_io, 'add_reaction', lambda *args, **kwargs: None)
