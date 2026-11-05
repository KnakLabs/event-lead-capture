# Event Lead Capture

A Slack agent for capturing leads at a trade show, built at
[Knak](https://knak.com?utm_source=github&utm_medium=referral&utm_campaign=knak-labs-ong).

A salesperson photographs a prospect's badge in Slack, says a few words about the
conversation, and the lead lands in the right Marketo event program and on a
tracking sheet — without anyone typing a form.

It runs on a laptop or a Replit container with one command. The only accounts it
needs are Slack, OpenAI, Clay, Marketo and a Google service account.

> **One rule that is not negotiable: no email, no lead.** If enrichment can't find
> a work email, nothing is created in Marketo. The row is flagged in the sheet's
> **MOPs Notes** column instead, for a human to deal with.

---

## What a capture looks like

```
Rep      @Lead Capture  [badge.jpg] [badge.jpg] [badge.jpg]
                        all three from GLX — evaluating us against Adobe,
                        want a demo in September

Agent    Filed Dana McAllister, Priya Raman and Iris Okonkwo, all at GLX.
         Found emails for Dana and Priya and pushed both to Marketo
         (1000001, 1000002). Clay couldn't place Iris, so she's flagged for
         Marketing Ops.

         [a card per person, showing what was captured]
```

The rep says nothing else. They are standing in front of the next prospect.

## What it does

**Reads badge photos.** Several at once — three badges in one message is three
people, each with their own row. It reads name, company and job title, and
ignores the conference name and sponsor logos printed on the lanyard. A photo
too blurred to read is reported rather than guessed at.

**Transcribes voice notes.** Walking between meetings, a rep can record a Slack
voice note instead of typing. This is often the difference between notes being
captured and not.

**Reads the whole thread.** It acts only when tagged, but each time it is, it
re-reads the thread — including photos posted before anyone remembered to tag
it. So the real pattern works: photo first, conversation, notes twenty minutes
later.

**Decides what belongs in the sheet.** "Sorry, one sec" and "can you grab me a
coffee" don't end up in the record.

**Finds work emails.** One Clay run for the whole group, not one per person.

**Acts without asking.** Rows created, emails found, leads pushed, then one short
message saying what happened. Corrections come afterwards by tagging it again —
it updates the sheet *and* re-pushes to Marketo so the record doesn't go stale.

**Tidies badge capitals.** Badges are printed in caps so they can be read across
a room. `ANNALIIA VOLKOV` becomes `Annaliia Volkov` before it reaches Marketo, so
`{{lead.firstName}}` doesn't greet her as "ANNALIIA". Only capitalisation changes,
never a character, so an unusual spelling survives.

**Resolves duplicate Marketo records.** When one email matches several records,
it picks one — Salesforce Contact over Lead over neither, then most recently
updated, then earliest created — instead of stranding the rep behind a
data-quality problem they can't fix from a booth.

---

## Setup

You need to edit **two** files: `.env` for credentials and account ids, and
`conventions.py` for how your Marketo is organised.

### 1. Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

### 2. Create the Slack app

**Order matters.** Slack verifies a Request URL the moment you save it, and the
server can't answer until it has the signing secret — which doesn't exist until
the app is created. So the app is created first, without any URLs, and the
Request URL goes in at step 7 once the server is running.

**From the manifest** (does everything below in one step):

api.slack.com/apps → **Create New App** → **From an app manifest** → pick the
workspace → paste `slack_app_manifest.yml` → **Create**.

#### The scopes it needs

The manifest requests these. If you're building the app by hand instead, add
them under **OAuth & Permissions → Bot Token Scopes** — every one maps to a call
the agent actually makes, so a missing scope surfaces as a failed capture rather
than a warning:

| Scope | Why it's needed |
| --- | --- |
| `app_mentions:read` | The trigger. An @mention in an event channel is the only thing that wakes the agent |
| `channels:read` | Read the channel's **name**, which is what identifies the Marketo program |
| `groups:read` | The same, for a private event channel |
| `channels:history` | Read the whole thread when tagged, not just the message that tagged it |
| `groups:history` | The same, for a private event channel |
| `chat:write` | Post and update the lead cards and the agent's replies |
| `files:read` | Download the badge photos and the voice-note audio |
| `reactions:write` | The 👀 / 👂 acknowledgement, so the rep knows the photo landed |

Because only `app_mentions:read` is granted, the app never receives ordinary
channel messages — it sees a message only when someone tags it. Untagged replies
are still read, from the thread, the next time it *is* tagged.

There is **no app-level token** and Socket Mode stays off: requests arrive over
HTTPS and are authenticated with the signing secret.

Two scopes you might expect and don't need: `users:read` (the agent never looks
up who a Slack id belongs to) and `im:history` / `im:write` (captures happen in
channels, not DMs).

#### Then collect two credentials

| Where in Slack | What | Goes in `.env` as |
| --- | --- | --- |
| **Basic Information** → App Credentials → Signing Secret → *Show* | 32-char hex | `SLACK_SIGNING_SECRET` |
| **OAuth & Permissions** → *Install to Workspace*, then copy the Bot User OAuth Token | starts `xoxb-` | `SLACK_BOT_TOKEN` |

**Install to Workspace** is also what grants the scopes — the app does nothing
until you do it. If you add a scope later, Slack makes you reinstall before it
takes effect.

Finally, **invite the bot to each event channel**. Being in the channel is the
only gate: there's no allowlist, because a channel is created per event and
enumerating them would mean a code change before every show.

The app isn't finished here. **Event Subscriptions** — turning it on, pasting
the Request URL and subscribing to `app_mention` — is the last piece, and it
needs a running server and a tunnel to point at, so it lives in
[step 7](#7-run-it).

#### Optional: a user token, for automated testing

Skip this unless you want to run `scripts/send_test_messages.py` (see
[Testing](#testing)). Everything else works without it.

The agent ignores mentions posted by a bot — that guard is what stops it
replying to its own replies — so a script that drives it has to post as a
*person*. That needs a user token, which is a separate token from the bot one:

**OAuth & Permissions → User Token Scopes** → add **both** of these:

| User scope | Why it's needed |
| --- | --- |
| `files:write` | Upload the badge photos and the voice note |
| `chat:write` | Post a plain message — the threaded mention in test 2 |

Then **Reinstall to Workspace** and copy the token beginning `xoxp-` into
`SLACK_USER_TOKEN`. Reinstalling reissues the token, so copy it again even if
you had one before.

Both are required. With only `files:write` the uploads succeed and the test
that replies in a thread fails on its own with `missing_scope`, part-way through
a run — the confusing failure, so it's worth checking the token has both.

It is deliberately not in `slack_app_manifest.yml`. The manifest asks only for
what the agent needs to run, and a scope that lets an app post as you is not
something to grant by default.

### 3. Build the Clay function

The agent calls one Clay routine, and it has to exist before you can fill in
`.env` — the routine id is what you paste there.

In Clay: **Functions** → new function. Its inputs and output are matched **by
name, exactly**. Clay accepts an unrecognised input key silently, so a typo here
fails at enrichment time rather than at the API:

| Column | Kind | Purpose |
| --- | --- | --- |
| `Full Name` | Input | From the badge |
| `Company` | Input | From the badge |
| `Official Domain` | Claygent (web research) | Turns the company name into a bare domain |
| `Work Email` | Email finder | The output, and the only column the agent reads |

**`Official Domain`** exists because an email finder needs a domain and a badge
gives you a trading name. It is a Claygent web-research column — the build this
was written against uses Claude 4.6 Sonnet — prompted to return the primary
domain in a strict format: `companyname.com`, with no protocol, no `www.`, no
subdomain, no path and no guessed country TLD. The strictness earns its keep: a
stray `https://` fails the lookup on a company Clay had already placed. When it
can't decide it writes `notFound`, which flows through to an empty `Work Email`.

**`Work Email`** takes `Full Name` + `Official Domain` and returns the address.
Use whichever finder or waterfall your workspace has — the agent only cares that
the column is named `Work Email` and holds an address.

Then open **Integrations** on the function:

1. Tick **API & CLI**. The public API is the only way this calls the routine, and
   that box is what exposes it there.
2. Copy the **Routine ID**. It already carries the `function:` prefix
   (`function:t_...`) — keep it. A bare `t_...` is rejected as an invalid tool id.
3. Paste it into `CLAY_ROUTINE_ID` in `.env`.

Leave **MCP for reps** and **Claygent** unticked. Neither is how this calls it.

#### How enrichment behaves

- **It is asynchronous.** A POST starts a run and results are polled every 5s for
  up to 180s (`CLAY_POLL_INTERVAL_SECONDS` / `CLAY_POLL_TIMEOUT_SECONDS`, both
  overridable). This is the slowest thing the agent does, and the reason there is
  a queue and a worker rather than a straight request and response.
- **The whole group is one run.** Three badges photographed together go in as a
  three-item array, so the rep waits once instead of three times.
- **Not found is not a failure.** Clay coming back empty is a fact about that
  person: the lead is flagged for Marketing Ops and never enriched again, because
  re-running it on each nudge burns a credit and re-appends the same note. A Clay
  *error* — dropped wifi, a 500, a timeout — is retryable and reported as such.
  The code keeps the two strictly apart.
- **Every lookup costs a credit**, so a lead with a blank name or company is
  rejected before the request rather than spending one on something Clay would
  reject anyway.

### 4. Fill in `.env`

Copy `.env.example` to `.env`. Every value is documented in the file. The ones
that need explaining:

| Variable | Where it comes from |
| --- | --- |
| `CLAY_API_KEY` | Clay → Settings → Account → API keys |
| `CLAY_ROUTINE_ID` | The function's Routine ID, from step 3. Keep the `function:` prefix |
| `MARKETO_BASE_URL` | Marketo → Admin → Web Services, minus the `/rest` suffix |
| `MARKETO_UI_HOST` | The host you see when Marketo is open in a browser — **not** the REST host above |
| `GOOGLE_SHEET_ID` | From the sheet URL: `docs.google.com/spreadsheets/d/<THIS>/edit` |

### 5. Edit `conventions.py`

This is the file people forget. It describes how *your* Marketo is laid out, and
it ships with the values this was built against as worked examples — not
defaults that will work anywhere:

| Setting | What it is |
| --- | --- |
| `EVENTS_ROOT_TEMPLATE` | The folder your event programs live under, one per year |
| `PROGRAM_NAME_PATTERN` | Which program names count as events |
| `PROGRAM_STATUS` | The membership status a captured lead is given |
| `NOTES_FIELD` | The API name of the field the rep's notes are written to |
| `SHEET_COLUMNS` | The header row of every event's tab |
| `TRANSCRIPTION_VOCABULARY` | Product names to nudge the transcriber towards |

`NOTES_FIELD` is the one that bites: it must exist in your instance and be a
`text` field rather than `string`, or Marketo truncates a long voice-note
transcript without telling you.

### 6. Google Sheets access

Three things, none of which is "configure scopes on the service account" —
scopes are requested by the code, not set on the account:

1. The **Google Sheets API enabled** on the project. A disabled API returns a 403
   that reads like a permissions error but isn't.
2. The sheet **shared with the service account's `client_email` as Editor**.
   Viewer reads fine and then fails on the first append.
3. `GOOGLE_SERVICE_ACCOUNT_FILE` pointing at the key file, or
   `GOOGLE_SERVICE_ACCOUNT_JSON` holding the same JSON inline.

Domain-wide delegation is *not* needed — that's for impersonating users, and the
sheet is shared with the account directly.

### 7. Run it

Two terminals:

```bash
python run_local.py
```

```bash
ngrok http 3000
```

Then in Slack: **Event Subscriptions** → toggle **On** → Request URL
`https://<your-ngrok>.ngrok-free.app/slack/events`. Wait for **Verified ✓**, add
**`app_mention`** under *Subscribe to bot events*, and **Save Changes**.

**Interactivity: leave it off.** There are no buttons, so there's no second URL
to keep in sync. Free ngrok issues a new URL on each restart, and Event
Subscriptions is the only place it needs re-pasting.

Health check, including queue depth and dead letters:

```bash
curl -s localhost:3000/healthz
```

---

## Before each event

**1. Create the Marketo program**, following whatever pattern `conventions.py`
matches.

**2. Create a Slack channel with the same name**, lowercased. Slack will force
the lowercasing and turn spaces into hyphens — just paste the program name in:

```
Marketo program:  FY2026-Q3-Knak Hosted Event-Dreamforce-Party
Slack channel:   #fy2026-q3-knak-hosted-event-dreamforce-party
```

Invite the bot to the channel and add the reps working the booth. That's it —
the channel name is how the agent knows which program to file into, so nothing is
guessed and nobody is asked "which event is this?".

**A tab appears in your sheet automatically**, named after the program, the first
time a lead is captured.

| Column | What's in it |
| --- | --- |
| **Timestamp** | When the badge was captured |
| **Name** / **Company** / **Job Title** | Read off the badge |
| **Email** | Found by Clay |
| **Sales Notes** | What the rep said, typed or spoken, summarised |
| **MOPs Notes** | **The column to watch.** Anything needing a human — usually "no work email found" |
| **Marketo Lead** | A direct link to the person. Empty means it never went |

Two more columns, **Slack Thread** and **State**, sit to the right. They're
bookkeeping and can be hidden. `Slack Thread` is `{thread_ts}#{name-slug}` —
that's what lets one thread hold several people.

---

## Hosting

Built to run on a laptop or **Replit**: Flask, a background worker, and a SQLite
queue, with no managed services.

**It does not fit a serverless host like Vercel as-is.** Slack needs an
acknowledgement within 3 seconds, while a capture can take a couple of minutes
waiting on Clay — which is why there's a queue and a worker at all. Serverless
functions have no persistent disk for that queue, no long-lived worker, and
timeouts shorter than the work. Running it there means an external queue and
splitting the capture into resumable steps.

## How the event is decided

The channel name is the answer. Both names are reduced to a canonical form
before comparing — lowercased, with runs of punctuation collapsed to one hyphen —
which absorbs what Slack forces on a name.

If the exact match fails, a channel name that is a prefix of **exactly one**
program is accepted, because Slack caps channel names at 80 characters. An
ambiguous prefix is refused rather than guessed at, and the agent asks.

The program list is fetched once and cached for an hour; the channel→program
answer is remembered for the life of the process.

## The agent's tools

| Tool | What it does |
| --- | --- |
| `find_event_program` | Search Marketo's event programs — the recovery path when a channel name doesn't match |
| `start_lead` | Create a row, once per badge |
| `update_lead` | Set columns on one lead, or the shared notes on all of them |
| `find_emails` | One Clay run for everyone who still needs an email |
| `push_to_marketo` | Create or update the lead. **Refuses without an email** |
| `discard_lead` | Mark a row discarded |
| `list_event_leads` | Read the whole tab, for questions about leads in other threads |
| `reply_to_rep` | The only tool that writes to Slack |

Constraints are enforced in the tools, not the prompt: `push_to_marketo` returns
an `Error:` string the model has to deal with rather than being trusted to
remember the rule. `start_lead` refuses a name the thread already holds, which is
what makes re-reading an old photo harmless.

## Architecture

```
Slack ──HTTPS──> ngrok ──> Flask (verify: signature, filter, enqueue)
                                    │  acks in well under 3s
                             SQLite queue  (FIFO per thread, dedupe,
                                    │       visibility timeout, dead-letter)
                             worker thread ──> agent  ──> OpenAI + tools
```

**Why HTTP and not Socket Mode.** Socket Mode needs a process running 24 hours a
day to be reachable, and events that arrive while nothing is connected are never
delivered — no queue, no retry. For one event a month that's mostly idle burn
*and* worse reliability.

**A thread can hold several leads.** A row is keyed by `{thread_ts}#{slug}`, so
three badges photographed together are three addressable rows. Tools take a
`who`; leaving it out means everyone in the thread.

**There is no database.** Lead state lives in the sheet, so the sheet is both the
working store and the record Marketing Ops reads.

**Only two things write to Slack** — the cards, and `reply_to_rep`. Every other
tool returns its result to the model, so the agent speaks with one voice.

## Testing

### Unit tests

```bash
pip install -r requirements-dev.txt
pytest
```

123 tests, hermetic — no network, no credentials, no API keys. They cover the
row-addressing that lets one thread hold several people, badge recasing, Marketo
duplicate resolution, reading photos posted before the tag, the program matching
in `conventions.py`, and the ingress: signature verification, the replay window,
Slack retries, FIFO ordering per thread, and dead-lettering.

### End-to-end, against a real channel

`scripts/send_test_messages.py` posts three captures into a channel and lets the
whole path run — Slack, the queue, badge reading, transcription, Clay, Marketo,
the sheet. It is the only way to exercise what unit tests can't: threading,
attachments arriving as real Slack files, and the agent's replies.

```bash
python scripts/send_test_messages.py C0123456789
```

A channel **name** works too, but Slack has no lookup-by-name API — the script
has to page through every channel in the workspace, which on a large one is
slow and rate limited. The id is instant. Find it in Slack: open the channel →
click its name → it's at the foot of the About tab.

**Before the first run:**

1. A `SLACK_USER_TOKEN` in `.env`, carrying **both** `chat:write` and
   `files:write` — see
   [Optional: a user token](#optional-a-user-token-for-automated-testing). The
   script refuses to start without a token rather than posting messages the
   agent will ignore. A token missing `chat:write` gets further and then fails
   mid-run on the threaded reply.
2. The bot invited to that channel.
3. A Marketo program matching the channel name, as with any event.
4. The server running (`python run_local.py`) and a tunnel pointed at it.

It reads the agent's own Slack id from your bot token at runtime, so it works
against any workspace's app without editing anything.

| Test | What it sends | What it proves |
| --- | --- | --- |
| 1 | Three badges + notes, in one tagged message | Three people become three rows and one Clay run, not one merged lead |
| 2 | A badge with **no** mention, then a mention in the thread | A photo posted before anyone remembered to tag the agent is still picked up |
| 3 | A voice note + a bare mention | Transcription, and notes with no badge at all |

Tests are sent back to back. The queue is FIFO per rep, so the agent still
works through them one at a time — the replies just interleave in the channel.
`--gap N` spaces the sends out when you want to read them in order.

Pick which tests to run with `--tests`: `all` (the default), a single number,
or a list — `--tests 2`, `--tests 1,3`.

Test files live in `test_files/`. Audio can be any format Slack accepts — `.m4a`
is what a phone records and what the transcription model takes directly. A
*native* Slack voice note can't be created through the API, but the agent
detects audio by mimetype as well as by Slack's `slack_audio` subtype, so an
uploaded file follows the same path.

## Known gaps

- **A voice note posted without tagging the agent is not transcribed.** Photos
  posted earlier in the thread are picked up on the next tag; audio is only
  transcribed on the turn it arrives.
- **Clay returns the email only** — no job title, company or LinkedIn. Those come
  from the badge. Adding a column in Clay is not enough on its own: the code
  reads the one output column by name, so a second one needs wiring up in
  `agent/clay.py`.
- **A brand's internal capital can't be recovered** — `PAYNEARME` becomes
  `Paynearme`, not `PayNearMe`. The rep can correct it.
- **ngrok's free URL changes on restart**, so Event Subscriptions needs
  re-pasting each time.

## Built at Knak

[Knak](https://knak.com?utm_source=github&utm_medium=referral&utm_campaign=knak-labs-ong)
is the agentic production layer for enterprise marketing: where marketers and AI
agents build on-brand emails and landing pages, then sync them to the team's
marketing automation platform.

This agent is one of the internal tools we build on top of that stack. The
conventions it ships with — the Marketo folder scheme in `conventions.py`, the
program-naming pattern, the notes field — are ours, left in as worked examples
rather than stripped out.

## License

MIT — see [LICENSE](LICENSE).
