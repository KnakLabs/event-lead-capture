"""Google Sheets access: one tab per event, rows addressed by their Slack thread.

The sheet is the only place lead state lives — there is no database. A row is
written the moment a badge is read and updated in place as notes arrive and on
push, so each AgentCore invocation (which shares nothing with the last but the
sheet) can pick a half-finished capture back up.

A tab rather than a spreadsheet per event, deliberately: a service account that
*creates* a spreadsheet also owns it, and nobody can open that file until the
code shares it explicitly. A new tab inside an already-shared spreadsheet is
visible immediately to everyone who has the parent.

    A..H  Timestamp | Name | Company | Email | Job Title | Sales Notes |
          MOPs Notes | Marketo Lead
    I     Slack Thread  — the row key, and a way back to the conversation
    J     State         — JSON: status, card ts, phone, Marketo id, transcripts

Everything machine-only lives in that one State blob, so adding a field later
doesn't change the shape Marketing Ops reads.
"""

import json
import re
import threading

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from config import GOOGLE_SERVICE_ACCOUNT_INFO
from config import GOOGLE_SHEET_ID as SHEET_ID
from conventions import SHEET_COLUMNS as COLUMNS

API_BASE = 'https://sheets.googleapis.com/v4/spreadsheets'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

KEY_COLUMN = 'Slack Thread'
STATE_COLUMN = 'State'
HEADERS = (*COLUMNS, KEY_COLUMN, STATE_COLUMN)

# A thread can hold several prospects, so the row key is `{thread_ts}#{slug}`.
# Kept out of the Slack ts itself, which never contains this character, so a key
# still reads as the thread it came from.
KEY_SEPARATOR = '#'

LAST_COLUMN_LETTER = chr(ord('A') + len(HEADERS) - 1)  # 'J'
KEY_INDEX = len(COLUMNS)  # 8 -> column I
STATE_INDEX = len(COLUMNS) + 1  # 9 -> column J

# Sheets rejects these in a tab title, and caps the title at 100 characters.
_ILLEGAL_TAB_CHARS = re.compile(r'[:\\/?*\[\]]')
MAX_TAB_TITLE = 100

# Reentrant: _ensure_tab holds this while calling _request, which locks too.
# The worker thread and a second capture can reach the tab cache at once.
_lock = threading.RLock()
_session: AuthorizedSession | None = None
# Which tabs exist, for read paths. Populated wholesale by `_tab_exists`.
_known_tabs: set[str] = set()
# Which tabs this container has confirmed have a header row. Deliberately
# separate: a read through `_tab_exists` marks a tab known long before any write
# touches it, so sharing one set would let `_ensure_tab` return early and skip
# the header check on every tab that already existed.
_tabs_with_headers: set[str] = set()


class SheetsError(RuntimeError):
    """The sheet could not be read or written."""


def tab_title(program_name: str) -> str:
    """The tab a program's leads go in. Same name, made legal for Sheets."""
    title = _ILLEGAL_TAB_CHARS.sub('-', (program_name or '').strip())
    return title[:MAX_TAB_TITLE] or 'Unfiled'


def append_row(program_name: str, values: list[str]) -> None:
    """Add a row, creating the tab and its header row if this is the first lead."""
    title = tab_title(program_name)
    _ensure_tab(title)
    _request(
        'POST',
        f'/{SHEET_ID}/values/{_range(title)}:append',
        params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'},
        json={'values': [values]},
    )


def find_row(program_name: str, key: str) -> tuple[int, list[str]] | None:
    """The 1-based row number and cell values for `key`, or None.

    One read per lookup. At expo-floor volumes that is cheaper than any cache
    would be worth, and it means two reps capturing in the same channel always
    see each other's rows.
    """
    title = tab_title(program_name)
    if not _tab_exists(title):
        return None

    for offset, row in enumerate(_read_values(title)):
        if len(row) > KEY_INDEX and row[KEY_INDEX] == key:
            return offset + 1, row  # +1: sheet rows are 1-based
    return None


def rows_for_thread(program_name: str, thread_ts: str) -> list[tuple[int, list[str]]]:
    """Every row belonging to one Slack thread, in sheet order.

    A thread holds one row per prospect — three badges photographed together are
    three rows — so the key is `{thread_ts}#{slug}`. Rows written before that
    existed carry a bare `thread_ts`, and there may be several of them sharing
    it; they match here too and are told apart by their Name column.
    """
    title = tab_title(program_name)
    if not (thread_ts and _tab_exists(title)):
        return []

    prefix = f'{thread_ts}{KEY_SEPARATOR}'
    return [
        (offset + 1, row)
        for offset, row in enumerate(_read_values(title))
        if len(row) > KEY_INDEX
        and (row[KEY_INDEX] == thread_ts or row[KEY_INDEX].startswith(prefix))
    ]


def all_rows(program_name: str) -> list[tuple[int, list[str]]]:
    """Every lead row in a program's tab, header excluded."""
    title = tab_title(program_name)
    if not _tab_exists(title):
        return []
    rows = _read_values(title)
    return [
        (offset + 1, row)
        for offset, row in enumerate(rows)
        if offset > 0 and any((cell or '').strip() for cell in row)
    ]


def update_row(program_name: str, row_number: int, values: list[str]) -> None:
    title = tab_title(program_name)
    padded = values + [''] * (len(HEADERS) - len(values))
    cells = f'A{row_number}:{LAST_COLUMN_LETTER}{row_number}'
    _request(
        'PUT',
        f'/{SHEET_ID}/values/{_range(title, cells)}',
        params={'valueInputOption': 'RAW'},
        json={'values': [padded[: len(HEADERS)]]},
    )


def encode_state(state: dict) -> str:
    return json.dumps(state, separators=(',', ':'))


def decode_state(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _read_values(title: str) -> list[list[str]]:
    payload = _request('GET', f'/{SHEET_ID}/values/{_range(title, f"A1:{LAST_COLUMN_LETTER}")}')
    return payload.get('values') or []


def _credentials():
    return service_account.Credentials.from_service_account_info(
        GOOGLE_SERVICE_ACCOUNT_INFO, scopes=SCOPES
    )


def _http() -> AuthorizedSession:
    """Authorised session. google-auth refreshes the token as needed."""
    global _session
    with _lock:
        if _session is None:
            _session = AuthorizedSession(_credentials())
        return _session


def _request(method: str, path: str, **kwargs) -> dict:
    try:
        response = _http().request(method, f'{API_BASE}{path}', timeout=30, **kwargs)
    except Exception as error:  # noqa: BLE001 — wrap transport failures so callers handle one type
        raise SheetsError(f'Could not reach Google Sheets: {error}') from error
    if response.status_code >= 400:
        raise SheetsError(f'{method} {path} failed ({response.status_code}): {response.text[:300]}')
    return response.json() or {}


def _range(title: str, cells: str = 'A1') -> str:
    """A1 range for a tab. Titles are quoted; internal quotes doubled."""
    quoted = title.replace("'", "''")
    return f"'{quoted}'!{cells}"


def _tab_exists(title: str) -> bool:
    if title in _known_tabs:
        return True
    with _lock:
        _known_tabs.update(_fetch_tab_titles())
        return title in _known_tabs


def _ensure_tab(title: str) -> None:
    """Create the tab if this event has no tab yet, and make sure it has headers."""
    if title in _tabs_with_headers:
        return

    with _lock:
        if title in _tabs_with_headers:  # another thread may have got here first
            return
        existing = _fetch_tab_titles()
        _known_tabs.update(existing)

        if title not in existing:
            _request(
                'POST',
                f'/{SHEET_ID}:batchUpdate',
                json={'requests': [{'addSheet': {'properties': {'title': title}}}]},
            )
        # Written whenever row 1 is empty, not only on the turn the tab is
        # created: if the create succeeds and this PUT then fails, the retry
        # finds the tab already there, and a tab MOps made by hand has no
        # headers either. Either way the columns would stay unlabelled forever.
        if not (_read_values(title) or [[]])[0]:
            _request(
                'PUT',
                f'/{SHEET_ID}/values/{_range(title, f"A1:{LAST_COLUMN_LETTER}1")}',
                params={'valueInputOption': 'RAW'},
                json={'values': [list(HEADERS)]},
            )
        _known_tabs.add(title)
        _tabs_with_headers.add(title)


def _fetch_tab_titles() -> set[str]:
    payload = _request('GET', f'/{SHEET_ID}', params={'fields': 'sheets.properties.title'})
    return {
        sheet['properties']['title']
        for sheet in payload.get('sheets', [])
        if sheet.get('properties', {}).get('title')
    }
