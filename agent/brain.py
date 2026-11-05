"""The brain: the model reads a lead's Slack thread and decides what to do.

This is the piece that replaces string-concatenation with judgement. It is given
the badge extraction, the current row, and the whole thread, and it chooses between
tools — update the row, ask the rep something, or do nothing at all.

Shaped after the `ai-for-revops-lesson5` pattern: Slack message in, model decides,
tools or a reply out. There are no buttons — everything, including the confirmation
before a Marketo push, happens in conversation.

The model owns:

  * which of the seven sheet columns a thread actually says something about
  * Sales Notes that describe the prospect, not the conversation with the bot
  * correcting a misread badge field when the thread contradicts it
  * when to look up the email, when to ask, when to push, when to say nothing
  * choosing to do nothing at all, which is the common case

One rule it does **not** own, because it has to hold every time. It is enforced in
the tool in `lead.py`, returning `Error: …` so the model corrects rather than the
rule quietly failing to apply:

  * **No Marketo lead without an email.** `push_to_marketo` refuses if the row has
    none.
Asking before pushing is the model's job, guided by the prompt: it shows the rep
the summary and waits for a yes. Its own earlier messages stay in the thread it is
given, so it can tell whether it has already asked.

**Only `reply_to_rep` writes to Slack.** Every other tool does its work and returns
a result to the model, which then decides what to say. That keeps the agent speaking
with one voice instead of a mix of the model's words and canned strings.
"""

import inspect
import json
import logging
from typing import Any

from openai import OpenAI

from agent import system_prompt
from config import OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY, timeout=90.0, max_retries=2)

# Enough for update -> find_email -> ask, then a later turn's push, with room for
# one Error: correction. Too low and the loop ends mid-sequence.
MAX_TOOL_ROUNDS = 6


SYSTEM_PROMPT = system_prompt.SYSTEM_PROMPT


def _schema_for(fn) -> dict[str, Any]:
    """An OpenAI function schema from a tool's signature and docstring.

    The tools live in `lead.py`, which is kept byte-identical to the deployed
    copy so a fix in either repo ports as a plain diff. There it is decorated
    with Strands' `@tool`, which builds this same schema from the same two
    things — so deriving it here rather than hand-writing a second copy is what
    keeps the two from drifting apart silently.

    The docstring contract, which `@tool` also expects: prose first, then an
    `Args:` block of `- name: description` lines.
    """
    doc = inspect.getdoc(fn) or ''
    description, _, args_block = doc.partition('Args:')

    described: dict[str, str] = {}
    for line in args_block.splitlines():
        line = line.strip()
        if line.startswith('- ') and ':' in line:
            name, _, text = line[2:].partition(':')
            described[name.strip()] = text.strip()

    properties, required = {}, []
    for name, parameter in inspect.signature(fn).parameters.items():
        properties[name] = {'type': 'string', 'description': described.get(name, name)}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)

    return {
        'type': 'function',
        'name': fn.__name__,
        'description': description.strip(),
        'parameters': {
            'type': 'object',
            'properties': properties,
            'required': required,
            'additionalProperties': False,
        },
    }


# The one tool with no counterpart in the deployed copy. There, the model's
# final text is the reply; here every tool returns to the model and only this
# one writes to Slack, so the agent speaks with one voice.
REPLY_TOOL = {
    'type': 'function',
    'name': 'reply_to_rep',
    'description': (
        'Say something to the salesperson in the Slack thread. This is the only '
        'way they hear from you, so end every turn with it — report what you did, '
        'or answer what they asked.'
    ),
    'parameters': {
        'type': 'object',
        'properties': {'message': {'type': 'string', 'description': 'One or two sentences'}},
        'required': ['message'],
        'additionalProperties': False,
    },
}


def schemas_for(tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Schemas for the tools on offer this turn, in a stable order."""
    return [REPLY_TOOL if name == 'reply_to_rep' else _schema_for(fn) for name, fn in tools.items()]


def interpret(
    *,
    situation: str,
    thread: list[dict[str, str]],
    tools: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
) -> None:
    """Run the loop. `tools` maps a tool name to the callable that performs it.

    Each callable returns a result string for the model. An `Error: …` result is how
    a refused call is communicated, so the model can correct and try again rather
    than the failure passing silently.

    `images` are the badge photos in play this turn. The model reads them itself
    and calls `start_lead` once per person, which is what lets one message carry
    three prospects.
    """
    content: list[dict[str, Any]] = [{'type': 'input_text', 'text': _context(situation, thread)}]
    content.extend(images or [])
    conversation: list[Any] = [{'role': 'user', 'content': content}]
    schemas = schemas_for(tools)

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=conversation,
            tools=schemas,
        )

        calls = [item for item in response.output if item.type == 'function_call']
        if not calls:
            # No tools wanted. The common, correct outcome for a quiet thread.
            logger.info('Brain decided no action was needed')
            return

        # Echo the model's own output back, including reasoning items, before the
        # results — the API needs its turn intact to continue.
        conversation.extend(response.output)
        for call in calls:
            conversation.append(
                {
                    'type': 'function_call_output',
                    'call_id': call.call_id,
                    'output': _dispatch(call, tools),
                }
            )
        # Loop again so the model can react to an Error: result.
    logger.warning('Brain hit the %d-round limit', MAX_TOOL_ROUNDS)


def _dispatch(call: Any, tools: dict[str, Any]) -> str:
    name = call.name
    handler = tools.get(name)
    if handler is None:
        return f'Error: no such tool {name!r}'

    try:
        arguments = json.loads(call.arguments or '{}')
    except json.JSONDecodeError as error:
        return f'Error: arguments were not valid JSON ({error}); call the tool again'

    logger.info('Brain -> %s(%s)', name, ', '.join(sorted(arguments)))
    try:
        return handler(**arguments)
    except TypeError as error:
        return f'Error: wrong arguments for {name} ({error}); call it again correctly'


def _context(situation: str, thread: list[dict[str, str]]) -> str:
    lines = [situation.rstrip(), '', '## The Slack thread, oldest first']
    if not thread:
        lines.append('(nothing said yet)')
    for entry in thread:
        lines.append(f'[{entry["who"]}] {entry["text"]}')
    return '\n'.join(lines)
