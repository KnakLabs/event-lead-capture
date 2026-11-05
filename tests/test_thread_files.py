"""Photos posted before the rep remembered to tag the agent.

Twice on 2026-09-09 a rep posted a badge and notes, then tagged the agent in a
later message. Thread context carried only "a file existed", so the agent replied
"I don't have a badge photo yet" and the capture was unrecoverable. These pin
down that earlier images now reach the turn.
"""

from agent import thread_files


def image(file_id, name='badge.jpg'):
    return {'id': file_id, 'name': name, 'mimetype': 'image/jpeg'}


def voice(file_id):
    return {'id': file_id, 'name': 'note.m4a', 'mimetype': 'audio/mp4'}


def is_audio(f):
    return str(f.get('mimetype', '')).startswith('audio/')


def run(files, messages):
    notices = []
    return thread_files.images_in_play(files, messages, is_audio, notices), notices


def test_a_photo_posted_before_the_tag_still_reaches_the_turn():
    messages = [
        {'user': 'U1', 'ts': '1.0', 'text': 'Small team of 5 marketers', 'files': [image('F1')]}
    ]
    shown, _ = run([], messages)
    assert [f['id'] for f in shown] == ['F1']


def test_this_messages_photos_come_first():
    messages = [{'user': 'U1', 'ts': '1.0', 'text': '', 'files': [image('F_OLD')]}]
    shown, _ = run([image('F_NOW')], messages)
    assert [f['id'] for f in shown] == ['F_NOW', 'F_OLD']


def test_the_same_file_in_both_places_is_read_once():
    messages = [{'user': 'U1', 'ts': '1.0', 'text': '', 'files': [image('F1')]}]
    shown, _ = run([image('F1')], messages)
    assert [f['id'] for f in shown] == ['F1']


def test_three_badges_posted_together_all_arrive():
    shown, _ = run([image('F1'), image('F2'), image('F3')], [])
    assert len(shown) == 3


def test_the_agents_own_messages_are_never_re_read():
    messages = [
        {'user': 'UBOT', 'ts': '2.0', 'text': 'card', 'self': True, 'files': [image('F_CARD')]}
    ]
    shown, _ = run([], messages)
    assert shown == []


def test_past_voice_notes_are_left_to_their_own_turn():
    """Already transcribed into the row; re-transcribing every turn is pure cost."""
    messages = [{'user': 'U1', 'ts': '1.0', 'text': '', 'files': [voice('F_AUDIO')]}]
    shown, _ = run([], messages)
    assert shown == []


def test_audio_on_this_message_is_left_to_the_transcriber():
    shown, _ = run([voice('F_AUDIO'), image('F_BADGE')], [])
    assert [f['id'] for f in shown] == ['F_BADGE']


def test_a_long_thread_is_capped_and_the_model_is_told():
    messages = [
        {'user': 'U1', 'ts': f'{i}.0', 'text': '', 'files': [image(f'F{i}')]} for i in range(12)
    ]
    shown, notices = run([], messages)
    assert len(shown) == thread_files.MAX_TURN_IMAGES
    # Silently dropping a badge is the failure this avoids.
    assert notices and 'missing' in notices[0]


def test_the_newest_thread_photos_survive_the_cap():
    messages = [
        {'user': 'U1', 'ts': f'{i}.0', 'text': '', 'files': [image(f'F{i}')]} for i in range(12)
    ]
    shown, _ = run([], messages)
    assert [f['id'] for f in shown][0] == 'F11'


def test_nothing_attached_anywhere_is_not_an_error():
    shown, notices = run([], [{'user': 'U1', 'ts': '1.0', 'text': 'just talking'}])
    assert shown == []
    assert notices == []


def test_malformed_context_is_tolerated():
    for messages in (None, 'nonsense', [None, 'x', {'files': None}]):
        shown, _ = run([], messages)
        assert shown == []
