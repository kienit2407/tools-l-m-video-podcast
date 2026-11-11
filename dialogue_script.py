"""Read plain Mia/Alex dialogue for voice assignment and subtitles."""
from pathlib import Path
import re

TURN_LABEL = re.compile(
    r'^\s*(MIA|ALEX|Speaker\s*[12])\s*[:：]\s*(.*)$',
    re.IGNORECASE,
)

# Script tags are local authoring hints, not text to speak or show in captions.
EMOTION_STYLES = {
    'happy': 'Warmly cheerful and upbeat',
    'sad': 'Quietly sad and reflective',
    'angry': 'Upset and firm, without shouting',
    'excited': 'Brightly excited and enthusiastic',
    'nervous': 'A little nervous and hesitant',
    'confident': 'Confident and assured',
    'calm': 'Calm and reassuring',
    'serious': 'Serious and thoughtful',
    'playful': 'Lightly playful and teasing',
    'worried': 'Concerned and worried',
    'curious': 'Curious and interested',
    'relieved': 'Gently relieved',
}
LEADING_TAG = re.compile(r'^\[([^\]]+)\]\s*')
OTHER_TAG = re.compile(r'\[[^\]]+\]')


def read_turns(paths):
    turns = []
    for source in paths:
        path = Path(source)
        first_turn = len(turns)
        for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            if not line.strip():
                continue
            match = TURN_LABEL.match(line)
            if match:
                label, speech = match.groups()
                speaker = 'Mia' if re.sub(r'\s', '', label).lower() in ('mia', 'speaker1') else 'Alex'
                emotion = None
                tag = LEADING_TAG.match(speech)
                if speech.lstrip().startswith('[') and not tag:
                    raise ValueError(f'{path}:{number}: invalid emotion tag at the start of speech')
                if tag:
                    emotion = tag.group(1).strip().lower()
                    if emotion not in EMOTION_STYLES:
                        allowed = ', '.join(f'[{name}]' for name in EMOTION_STYLES)
                        raise ValueError(f'{path}:{number}: unknown emotion tag [{emotion}]. Use: {allowed}')
                    speech = speech[tag.end():]
                    if speech.lstrip().startswith('['):
                        raise ValueError(f'{path}:{number}: use only one emotion tag per turn')
                if OTHER_TAG.search(speech):
                    raise ValueError(f'{path}:{number}: place the emotion tag only at the start of a turn')
                if not speech.strip():
                    raise ValueError(f'{path}:{number}: missing speech')
                turn = {'speaker': speaker, 'text': speech.strip()}
                if emotion:
                    turn['emotion'] = emotion
                turns.append(turn)
            elif re.match(r'^\s*[A-Za-z][\w \[\]-]{0,80}[:：]', line):
                raise ValueError(f'{path}:{number}: invalid speaker label; use Mia: or Alex:')
            elif len(turns) > first_turn:
                if OTHER_TAG.search(line):
                    raise ValueError(f'{path}:{number}: place the emotion tag immediately after Mia: or Alex:')
                turns[-1]['text'] += ' ' + line.strip()
            else:
                raise ValueError(f'{path}:{number}: missing Mia:/Alex: label')
    if not turns:
        raise ValueError('No dialogue found')
    return turns


def format_turn(turn):
    tag = f'[{turn["emotion"]}] ' if turn.get('emotion') else ''
    return f'{turn["speaker"]}: {tag}{turn["text"]}'
