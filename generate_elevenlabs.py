#!/usr/bin/env python3
"""Mia/Alex dialogue via ElevenLabs, with resumable joint-dialogue requests."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
from urllib import error, request
import wave

from dialogue_script import read_turns

# Điền key và hai Voice ID tại đây. Không chia sẻ file sau khi điền key.
# Biến môi trường cùng tên được ưu tiên nếu bạn đã thiết lập chúng.
ELEVENLABS_API_KEY = 'sk_7426825d092a8c95e0891e05e15d9fe501f83998c8d14b5e'
MIA_VOICE_ID = '0ggs85h7hrWz8ExXnsBc'       # Giọng nữ cho Mia.
ALEX_VOICE_ID = 'GzE4TcXfh9rYCU9gVgPp'      # Giọng nam cho Alex.
MODEL_ID = 'eleven_v4'

API_URL = 'https://api.elevenlabs.io/v1/text-to-dialogue?output_format=pcm_24000'
MAX_CHARACTERS = 2000
SAMPLE_RATE = 24000
ENGINE = 'elevenlabs-dialogue-v1'


def configuration(model=None, mia_voice=None, alex_voice=None):
    return {
        'model': model or os.environ.get('ELEVENLABS_MODEL', '').strip() or MODEL_ID,
        'voices': {
            'Mia': mia_voice or os.environ.get('ELEVENLABS_MIA_VOICE_ID', '').strip() or MIA_VOICE_ID.strip(),
            'Alex': alex_voice or os.environ.get('ELEVENLABS_ALEX_VOICE_ID', '').strip() or ALEX_VOICE_ID.strip(),
        },
    }


def api_key():
    return os.environ.get('ELEVENLABS_API_KEY', '').strip() or ELEVENLABS_API_KEY.strip()


def missing_configuration(config, *, key=None):
    missing = []
    if not (api_key() if key is None else key):
        missing.append('ELEVENLABS_API_KEY')
    for speaker in ('Mia', 'Alex'):
        if not config['voices'][speaker]:
            missing.append(f'{speaker.upper()}_VOICE_ID')
    return missing


def split_dialogue(paths):
    """Keep speaker order and jointly synthesize up to 2,000 characters per request."""
    pieces = []
    for turn in read_turns(paths):
        tag = f'[{turn["emotion"]}] ' if turn.get('emotion') else ''
        limit = MAX_CHARACTERS - len(tag)
        words = turn['text'].split()
        line = []
        length = 0
        for word in words:
            if len(word) > limit:
                raise ValueError('A script word exceeds the ElevenLabs request limit. Shorten it.')
            extra = len(word) + bool(line)
            if line and length + extra > limit:
                pieces.append({'speaker': turn['speaker'], 'text': tag + ' '.join(line)})
                line, length = [], 0
            length += len(word) + bool(line)
            line.append(word)
        if line:
            pieces.append({'speaker': turn['speaker'], 'text': tag + ' '.join(line)})
    groups, current, characters = [], [], 0
    for piece in pieces:
        if current and characters + len(piece['text']) > MAX_CHARACTERS:
            groups.append(current)
            current, characters = [], 0
        current.append(piece)
        characters += len(piece['text'])
    if current:
        groups.append(current)
    return groups


def build_payload(group, config, *, previous=None, following=None, request_ids=()):
    payload = {
        'model_id': config['model'],
        'inputs': [{'text': t['text'], 'voice_id': config['voices'][t['speaker']]} for t in group],
    }
    if previous:
        payload['previous_text'] = ' '.join(t['text'] for t in previous)[-100:]
    if following:
        payload['future_text'] = ' '.join(t['text'] for t in following)[:100]
    if request_ids:
        payload['previous_request_ids'] = list(request_ids)[-3:]
    # Leave speed, stability, emotion and audio effects at the provider defaults.
    return payload


def call_api(payload, key, timeout=240):
    if not key:
        raise RuntimeError('Fill ELEVENLABS_API_KEY in generate_elevenlabs.py before generating new audio.')
    if any(character.isspace() for character in key):
        raise RuntimeError('ELEVENLABS_API_KEY contains whitespace. Paste only the key value.')
    import certifi
    req = request.Request(API_URL, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'xi-api-key': key, 'Content-Type': 'application/json'}, method='POST')
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with request.urlopen(req, timeout=timeout, context=context) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if any(kind in content_type for kind in ('json', 'text/', 'mpeg', 'wav')):
                raise RuntimeError('ElevenLabs did not return the requested PCM audio; no completed WAV saved.')
            pcm = response.read(24_000_001)
            request_id = response.headers.get('request-id') or response.headers.get('x-request-id')
    except error.HTTPError as exc:
        message = exc.read(4000).decode('utf-8', errors='replace').replace(key, '[hidden]')
        raise RuntimeError(f'ElevenLabs HTTP {exc.code}: {message[:900]}. Completed parts are preserved.') from None
    except (error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
        # An interrupted paid request may already have consumed credits. Never retry it automatically.
        reason = str(exc).replace(key, '[hidden]')
        raise RuntimeError(
            f'ElevenLabs connection interrupted: {reason}. No automatic retry. '
            'Completed parts are preserved; check ElevenLabs History before retrying an uncertain request.'
        ) from None
    if not pcm or len(pcm) % 2 or len(pcm) > 24_000_000:
        raise RuntimeError('ElevenLabs returned empty, oversized or incomplete PCM audio; no completed WAV saved.')
    return pcm, request_id


def save_json(path, data):
    temporary = path.with_suffix('.partial.json')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_wav(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
            raise RuntimeError(f'Unexpected ElevenLabs WAV format: {path}')
        frames = audio.getnframes()
        if not frames:
            raise RuntimeError(f'Empty audio: {path}')
        audio.setpos(frames-1)
        if len(audio.readframes(1)) != 2:
            raise RuntimeError(f'Truncated audio: {path}. Restore it before resuming.')
        return frames


def generate_audio(paths, output, config, *, key=None):
    if config['model'] not in ('eleven_v4', 'eleven_v3'):
        raise ValueError('Use eleven_v4 or eleven_v3 for Text to Dialogue.')
    if not all(config['voices'].values()):
        raise RuntimeError('Fill MIA_VOICE_ID and ALEX_VOICE_ID in generate_elevenlabs.py.')
    if config['voices']['Mia'] == config['voices']['Alex']:
        raise RuntimeError('Mia and Alex need two different Voice IDs.')
    groups = split_dialogue(paths)
    settings = {'engine': ENGINE, 'config': config, 'output_format': 'pcm_24000', 'groups': groups}
    output = Path(output)
    marker = output/'inputs.json'
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8')) != settings:
            raise RuntimeError('ElevenLabs script/voices/model changed. Choose a NEW --episode or --outdir.')
    elif output.exists() and any(output.glob('*.wav')):
        raise RuntimeError('Untracked ElevenLabs audio. Choose a NEW --episode or --outdir.')
    output.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        save_json(marker, settings)
    # Validate every existing part before issuing ANY paid requests.
    for index in range(len(groups)):
        wav = output/f'part{index+1:03d}.wav'
        info = wav.with_suffix('.json')
        if wav.exists():
            check_wav(wav)
            if not info.exists() or json.loads(info.read_text()).get('sha256') != file_hash(wav):
                raise RuntimeError(f'Unverified saved audio: {wav}. Preserve/restore it before resuming.')
        elif info.exists():
            raise RuntimeError(f'Audio missing for {info.name}. Restore its WAV before resuming.')
    joined = output/'joined_unpadded.wav'
    if joined.exists():
        check_wav(joined)
        print('Reuse complete ElevenLabs dialogue:', joined.name, flush=True)
        return joined
    wavs, request_ids = [], []
    for index, group in enumerate(groups):
        wav = output/f'part{index+1:03d}.wav'
        info = wav.with_suffix('.json')
        wavs.append(wav)
        if wav.exists():
            request_id = json.loads(info.read_text()).get('request_id')
            print('Reuse ElevenLabs:', wav.name, flush=True)
        else:
            count = sum(len(t['text']) for t in group)
            print(f'ElevenLabs {wav.name}: {len(group)} turns, {count} characters; generating dialogue...', flush=True)
            payload = build_payload(group, config, previous=groups[index-1] if index else None,
                                    following=groups[index+1] if index+1 < len(groups) else None,
                                    request_ids=request_ids)
            pcm, request_id = call_api(payload, api_key() if key is None else key)
            temporary = wav.with_suffix('.partial.wav')
            with wave.open(str(temporary), 'wb') as audio:
                audio.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
                audio.writeframes(pcm)
            check_wav(temporary)
            temporary.replace(wav)
            save_json(info, {'request_id': request_id, 'sha256': file_hash(wav)})
        if request_id:
            request_ids.append(request_id)
    temporary = joined.with_suffix('.partial.wav')
    with wave.open(str(temporary), 'wb') as result:
        result.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        for wav in wavs:
            with wave.open(str(wav), 'rb') as audio:
                while block := audio.readframes(SAMPLE_RATE):
                    result.writeframes(block)
    check_wav(temporary)
    temporary.replace(joined)
    return joined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parts', type=Path, nargs='+', required=True)
    parser.add_argument('--outdir', type=Path, default=Path('output_elevenlabs'))
    parser.add_argument('--model', default=None)
    parser.add_argument('--mia-voice', default=None)
    parser.add_argument('--alex-voice', default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config = configuration(args.model, args.mia_voice, args.alex_voice)
    if args.dry_run:
        groups = split_dialogue(args.parts)
        print(f'ElevenLabs: {len(groups)} dialogue requests, '
              f'{sum(len(t["text"]) for g in groups for t in g)} text characters. No API calls.')
        missing = missing_configuration(config)
        if missing:
            print('CONFIG REQUIRED in generate_elevenlabs.py: ' + ', '.join(missing))
        return
    print('Saved:', generate_audio(args.parts, args.outdir, config))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, wave.Error) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
