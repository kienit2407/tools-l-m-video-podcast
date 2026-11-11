#!/usr/bin/env python3
"""Generate two-speaker WAV podcast clips with Gemini 3.8 TTS.

Uses only the Python standard library. Speaker labels are converted to
speech_metadata annotations, so Gemini will not speak them aloud.
"""
import argparse
import base64
import json
import io
import math
import os
from pathlib import Path
import re
import sys
import time
import wave
from urllib import request, error
import ssl
import certifi

from dialogue_script import EMOTION_STYLES, read_turns

API_URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'
LABEL = re.compile(r'^\s*(MIA|ALEX|Speaker\s*[12])\s*:\s*(.*)$', re.IGNORECASE)
# GEMINI_API_KEY= 'AQ.Ab8RN6Lj-aaKnWWJ1JDa-xHh7_si0NEASv-bxkj0J4EcldR_pQ'
GEMINI_API_KEY= 'AQ.Ab8RN6JSxpMBifokDA7KrMeCPmDLMfeUdGL0PvqcDA_-G1notw' 


def read_dialogue(path):
    return read_turns([path])


# Gemini 3.8 TTS: 8,192 input tokens; 16,384 output audio tokens at 25/s.
# Leave 10% headroom for pauses and uncertainty in local input estimates.
LONG_CONTEXT_MODELS = frozenset({'gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts'})
INPUT_TOKEN_LIMIT = 8192
OUTPUT_TOKEN_LIMIT = 16384
INPUT_TOKEN_BUDGET = int(INPUT_TOKEN_LIMIT * .9)
MAX_ENGLISH_WORDS = int(OUTPUT_TOKEN_LIMIT / 25 * .9 / 60 * 130)
ENGLISH_LEVELS = {
    'a1-a2': {
        'min_wpm': 90,
        'style': ('Speak slowly and clearly for A1-A2 beginner English learners, roughly 90-105 spoken words per minute. '
                  'Use gentle sentence stress, clear word boundaries and longer pauses between phrases and sentences. '
                  'Keep a warm, natural voice; do not rush, exaggerate or change the script.'),
    },
    'b1-b2': {
        'min_wpm': 130,
        'style': ('Natural, unhurried back-and-forth delivery, roughly 130-145 spoken words per minute. '
                  'Clear B1-B2 pronunciation, natural sentence stress and brief phrase pauses.'),
    },
}


def max_english_words(level='b1-b2'):
    """Slower delivery needs smaller requests to stay inside the audio budget."""
    return int(OUTPUT_TOKEN_LIMIT / 25 * .9 / 60 * ENGLISH_LEVELS[level]['min_wpm'])


def estimate_input_tokens(payload):
    """Conservative offline English estimate, not Google's exact tokenizer."""
    content = payload['input'][0]['content']
    size = sum(len(item['text'].encode('utf-8')) +
               sum(len(a.get('style', '').encode('utf-8')) for a in item.get('annotations', []))
               for item in content)
    # Google documents ~4 characters/token. Use 3.5 bytes/token, plus an
    # allowance for speaker/annotation structure and the request's framing.
    return math.ceil(size / 3.5) + 8*len(content) + 128


def build_payload(turns, model, mia_voice, alex_voice, *, language='en', level='b1-b2'):
    content = []
    # Restore the characters from the original EP02 generator. The reference
    # Studio directions target 130–145 WPM; this is a delivery request, not a
    # guaranteed rate or an audio time-stretch.
    english_pace = ENGLISH_LEVELS[level]['style']
    for turn in turns:
        emotion = turn.get('emotion')
        if emotion:
            delivery = EMOTION_STYLES[emotion]
            if language == 'zh':
                style = f'{delivery}. Speak standard Mandarin Chinese at a natural conversational pace.'
            else:
                accent = 'British' if turn['speaker'] == 'Mia' else 'American'
                style = f'{delivery}. {accent} English accent. {english_pace}'
        elif language == 'zh':
            style = ('Warm, attentive standard Mandarin Chinese. ' if turn['speaker'] == 'Mia' else
                     'Engaged, thoughtful standard Mandarin Chinese. ')
            style += ('Clear tones, natural connected speech and sentence stress for HSK 5-6 listeners. '
                      'React to the dialogue with context-appropriate emotion at a natural conversational pace.')
        else:
            style = ('Warm, friendly and expressive British English accent. Sometimes curious or lightly playful. '
                     if turn['speaker'] == 'Mia' else
                     'Friendly, relaxed American English accent. Witty but not exaggerated. ')
            style += english_pace
        content.append({
            'type': 'text', 'text': turn['text'],
            'annotations': [{ 'type': 'speech_metadata', 'speaker': turn['speaker'], 'style': style }]
        })
    payload = {
        'model': model,
        'input': [{ 'type': 'user_input', 'content': content }],
        'response_format': {'type': 'audio'},
        'generation_config': {
            'speech_config': {
                'speakers': [
                    {'speaker': 'Mia', 'voice': mia_voice},
                    {'speaker': 'Alex', 'voice': alex_voice},
                ]
            }
        }
    }
    if language == 'en' and model in LONG_CONTEXT_MODELS:
        payload['generation_config']['max_output_tokens'] = OUTPUT_TOKEN_LIMIT
    return payload


def call_api(payload, key, timeout=600, *, max_attempts=4):
    req = request.Request(API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
        method='POST')
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    for attempt in range(max_attempts):
        try:
            with request.urlopen(req, timeout=timeout, context=ssl_context) as response:
                obj = json.loads(response.read().decode('utf-8'))
            break
        except error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            try:
                msg = json.loads(body).get('error', {}).get('message', body)
            except ValueError:
                msg = body
            daily_quota = any(marker in msg.lower() for marker in
                              ('quota_exceeded', 'daily quota', 'requests per day'))
            if exc.code in (408, 429, 500, 502, 503, 504) and not daily_quota and attempt < max_attempts-1:
                delay = 3*(2**attempt)
                print(f'  TTS HTTP {exc.code}; retrying in {delay}s...', flush=True)
                time.sleep(delay)
                continue
            raise RuntimeError(f'Google API HTTP {exc.code}: {msg[:700]}') from None
        except error.URLError as exc:
            raise RuntimeError(f'Cannot connect to Google API: {exc.reason}. Re-run to resume completed parts.') from None

    if obj.get('status') not in (None, 'completed'):
        raise RuntimeError(f'TTS generation did not complete: {obj.get("status")}')

    chunks = []
    for step in obj.get('steps', []):
        if step.get('type') != 'model_output':
            continue
        for part in step.get('content', []):
            if part.get('type') == 'audio' and part.get('data'):
                chunks.append(part['data'])
    if not chunks and obj.get('output_audio', {}).get('data'):
        chunks.append(obj['output_audio']['data'])
    if not chunks:
        detail = obj.get('error') or obj.get('status') or obj.get('id') or list(obj.keys())
        raise RuntimeError(f'No WAV audio returned. Response info: {detail}')
    audio = base64.b64decode(chunks[-1], validate=True)
    if not audio.startswith(b'RIFF'):
        raise RuntimeError('Audio does not appear to be WAV. Check TTS model and output format.')
    with wave.open(io.BytesIO(audio), 'rb') as wav:
        if wav.getnframes() == 0 or len(wav.readframes(wav.getnframes())) != wav.getnframes()*wav.getnchannels()*wav.getsampwidth():
            raise RuntimeError('TTS returned empty or truncated WAV audio; it has not been saved.')
    return audio


def main():
    parser = argparse.ArgumentParser(description='Automatically assign Mia and Alex voices from scene text files.')
    parser.add_argument('--dry-run', action='store_true', help='Validate dialogue and print scene counts without calling Google')
    parser.add_argument('--no-retry', action='store_true', help='Make at most one HTTP attempt per part; useful for short voice previews')
    parser.add_argument('--model', default='gemini-3.8-flash-tts')
    parser.add_argument('--language', choices=('en', 'zh'), default='en')
    parser.add_argument('--level', choices=tuple(ENGLISH_LEVELS), default='b1-b2',
                        help='English delivery level; does not rewrite the script')
    parser.add_argument('--mia-voice', default='Kore', help='Preset voice name for Mia (listen to sample first)')
    parser.add_argument('--alex-voice', default='Puck', help='Preset voice name for Alex (listen to sample first)')
    parser.add_argument('--outdir', default='output_wav')
    parser.add_argument('--parts', nargs='*', help='Files to generate, e.g. parts/part1.txt; defaults to all four')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    inputs = [Path(p) for p in args.parts] if args.parts else sorted((root / 'parts').glob('part*.txt'))
    if not inputs:
        parser.error('No input files. Add parts/part1.txt etc.')
    key = os.environ.get('GEMINI_API_KEY', '').strip() or GEMINI_API_KEY.strip()
    if not key and not args.dry_run:
        parser.error('GEMINI_API_KEY not set. See README_VI.txt. Never share your key.')
    outdir = Path(args.outdir).resolve()
    for path in inputs:
        turns = read_dialogue(path)
        count = sum(len(t['text']) if args.language == 'zh' else len(t['text'].split()) for t in turns)
        unit = 'text characters' if args.language == 'zh' else 'spoken words'
        print(f'{path.name}: {len(turns)} turns, {count} {unit}; first = {turns[0]["speaker"]}')
        output = outdir / (path.stem + '.wav')
        if output.exists() and not args.dry_run:
            print(f'  Already exists: {output}. Skipping to avoid duplicate quota usage.')
            continue
        payload = build_payload(turns, args.model, args.mia_voice, args.alex_voice,
                                language=args.language, level=args.level)
        if args.language == 'en' and args.model in LONG_CONTEXT_MODELS:
            if count > max_english_words(args.level) or estimate_input_tokens(payload) > INPUT_TOKEN_BUDGET:
                raise ValueError('Part exceeds the planned Gemini input/audio budget. '
                                 'Use one_click.py to split the script automatically before generating.')
        if args.dry_run:
            continue
        print('  Generating...', flush=True)
        wav = call_api(payload, key, max_attempts=1 if args.no_retry else 4)
        outdir.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.partial.wav')
        temporary.write_bytes(wav)
        temporary.replace(output)
        print(f'  Saved: {output} ({len(wav)//1024} KiB)', flush=True)
    if args.dry_run:
        print('Dry run complete. No API calls or WAV files created.')
    else:
        print('Done. WAV files are ready.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, wave.Error) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
