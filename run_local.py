"""Local runner — the only file with no deployed counterpart.

It stands in for three pieces of AWS at once so the rest of the code doesn't have
to know the difference:

    API Gateway  -> Flask route POST /slack/events
    SQS FIFO     -> src.shared.taskqueue (SQLite)
    Lambda x2    -> the verify handler called inline; the trigger handler
                    called by a worker thread

Lead state is *not* local: it lives in the event's Google Sheet tab, so this runner
holds nothing that a deployed Lambda wouldn't also have.

Point both Slack Request URLs (Event Subscriptions and Interactivity) at the same
tunnel URL + /slack/events. The verify handler tells events and interactivity
apart by content type, which keeps this to the single `routePath` that
agents.json declares.

    ngrok http 3000        ->  https://<something>.ngrok-free.app/slack/events
"""

import logging
import threading
import time

from flask import Flask, request

from agent import marketo
from config import (
    PORT,
    QUEUE_DB_PATH,
    QUEUE_DLQ_MAX_RECEIVE_COUNT,
    QUEUE_VISIBILITY_TIMEOUT_SECONDS,
)
from src.lambdas.trigger.handler import process_once
from src.lambdas.verify.handler import handle as verify_handle
from src.shared.taskqueue import TaskQueue

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('event-lead-capture')

app = Flask(__name__)

queue = TaskQueue(
    QUEUE_DB_PATH,
    visibility_timeout_seconds=QUEUE_VISIBILITY_TIMEOUT_SECONDS,
    dlq_max_receive_count=QUEUE_DLQ_MAX_RECEIVE_COUNT,
)


@app.post('/slack/events')
def slack_events():
    # get_data() gives the untouched bytes — the signature is computed over
    # those, so never let Flask parse the body first.
    raw_body = request.get_data(as_text=True)
    status, body = verify_handle(raw_body, dict(request.headers), queue)
    return body, status


@app.get('/healthz')
def healthz():
    return {'ok': True, 'queue': queue.stats()}, 200


def worker() -> None:
    """Stands in for SQS invoking the trigger lambda."""
    logger.info('Worker started')
    while True:
        try:
            if not process_once(queue):
                time.sleep(1)
        except Exception:
            logger.exception('Worker loop error')
            time.sleep(2)


def main() -> None:
    threading.Thread(target=worker, daemon=True).start()
    # Warm the program cache so the first badge of the day doesn't wait on Marketo.
    threading.Thread(target=marketo.cache.get, daemon=True).start()
    logger.info('Listening on http://127.0.0.1:%d/slack/events', PORT)
    # debug=False: the reloader would start a second worker thread.
    app.run(host='127.0.0.1', port=PORT, debug=False)


if __name__ == '__main__':
    main()
