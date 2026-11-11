"""Reusable Band 7 Radio CTA recordings and locally synthesized transition sounds."""
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import wave


CTA_TEXT = {
    'opening': 'Welcome to Band 7 Radio! Subscribe for more English topics.',
    'closing': 'Subscribe for more English topics.',
}
OVERVIEW_TEXT = (
    "You'll hear the same short conversation three times. "
    'Part one: English transcript. Part two: English and Vietnamese subtitles. '
    'Part three: just listen.'
)
OVERVIEW_PARTS = (
    ('PHẦN 1', 'Nghe và đọc lời thoại tiếng Anh'),
    ('PHẦN 2', 'Nghe với phụ đề Anh – Việt'),
    ('PHẦN 3', 'Chỉ nghe, không phụ đề'),
)
OVERVIEW_DURATION = 2.0
PART_DURATION = 2.5
FIRST_PART_DURATION = 1.5
FADE_SECONDS = 0.3
EXIT_GAP = 0.12
SAMPLE_RATE = 48000


def wav_duration(path):
    with wave.open(str(path), 'rb') as audio:
        count = audio.getnframes()
        if count <= 0 or len(audio.readframes(count)) != count * audio.getnchannels() * audio.getsampwidth():
            raise ValueError(f'Empty or truncated audio: {path}')
        return count / audio.getframerate()


def cta_locations(root, config, *, texts=None):
    """The cache follows spoken content and Mia's voice, independently of episodes."""
    texts = CTA_TEXT if texts is None else texts
    settings = {'version': 1, 'text': texts, **config}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]
    folder = Path(root) / 'assets' / 'cta' / digest
    return {name: folder / (name + '.wav') for name in texts}, settings


def describe_cta(root, config, names=('opening', 'closing'), *, texts=None):
    paths, _ = cta_locations(root, config, texts=texts)
    paths = {name: paths[name] for name in names}
    missing = []
    for name, path in paths.items():
        if path.is_file():
            wav_duration(path)
        else:
            missing.append(name)
    print('CTA: reuse cached recordings.' if not missing else
          f'CTA: {len(missing)} short {config["provider"]} TTS request(s) needed once: {", ".join(missing)}.',
          flush=True)
    return paths


def write_pcm(path, samples, rate=SAMPLE_RATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial.wav')
    pcm = array('h', (round(max(-1.0, min(1.0, value)) * 32767) for value in samples))
    if sys.byteorder != 'little':
        pcm.byteswap()
    with wave.open(str(temporary), 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm.tobytes())
    wav_duration(temporary)
    temporary.replace(path)


def prepare_cta(root, config, names=('opening', 'closing'), *, texts=None):
    texts = CTA_TEXT if texts is None else texts
    paths = describe_cta(root, config, names, texts=texts)
    _, settings = cta_locations(root, config, texts=texts)
    for name, path in paths.items():
        if path.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / 'inputs.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')
        (path.parent / (name + '.txt')).write_text('Mia: ' + texts[name] + '\n', encoding='utf-8')
        print(f'Create reusable {name} CTA (Mia); one request, no automatic retry.', flush=True)
        turn = {'speaker': 'Mia', 'text': texts[name]}
        if config['provider'] == 'gemini':
            import generate
            key = os.environ.get('GEMINI_API_KEY', '').strip() or generate.GEMINI_API_KEY.strip()
            if not key:
                raise RuntimeError('GEMINI_API_KEY is required to create the reusable CTA voice.')
            payload = generate.build_payload([turn], config['model'], config['voice'], 'Puck',
                                             level=config.get('level', 'b1-b2'))
            data = generate.call_api(payload, key, timeout=120, max_attempts=1)
            temporary = path.with_suffix('.partial.wav')
            temporary.write_bytes(data)
            wav_duration(temporary)
            temporary.replace(path)
        elif config['provider'] == 'elevenlabs':
            import generate_elevenlabs as eleven
            voice_settings = {'model': config['model'], 'voices': {'Mia': config['voice']}}
            pcm, _ = eleven.call_api(eleven.build_payload([turn], voice_settings), eleven.api_key())
            temporary = path.with_suffix('.partial.wav')
            with wave.open(str(temporary), 'wb') as audio:
                audio.setparams((1, 2, eleven.SAMPLE_RATE, 0, 'NONE', 'not compressed'))
                audio.writeframes(pcm)
            wav_duration(temporary)
            temporary.replace(path)
        else:
            raise ValueError(f'Unsupported CTA provider: {config["provider"]}')
    return paths


def read_custom_ding(path, ffmpeg):
    """Decode a short mono bell, remove leading silence and fade its tail; no tempo change."""
    result = subprocess.run(
        [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-i', str(path),
         '-af', 'silenceremove=start_periods=1:start_duration=0.01:start_threshold=-45dB:start_silence=0.005',
         '-t', '1.35', '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 'f32le', 'pipe:1'],
        capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f'Cannot decode custom ding {path}: {result.stderr.decode(errors="replace")[:500]}')
    samples = array('f')
    samples.frombytes(result.stdout)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples or not all(math.isfinite(s) for s in samples) or max(abs(s) for s in samples) < .0001:
        raise ValueError(f'Custom ding is empty or silent: {path}')
    # Keep a custom recording at the same soft peak level as the original bell.
    gain = .12 / max(abs(s) for s in samples)
    fade = min(round(.08 * SAMPLE_RATE), len(samples))
    attack = min(round(.003 * SAMPLE_RATE), len(samples))
    return [s * gain * min(1, i / attack, (len(samples)-1-i) / fade)
            for i, s in enumerate(samples)]


def prepare_part_sfx(root, duration=PART_DURATION, *, ffmpeg='ffmpeg'):
    """Prefer ding.mp3 on entry; rebuild mixed tracks whenever its content changes."""
    folder = Path(root) / 'assets' / 'sfx'
    track = folder / f'part_card_{round(duration * 1000)}ms.wav'
    marker = track.with_suffix('.json')
    custom = folder / 'ding.mp3'
    digest = None
    if custom.is_file():
        with custom.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
    settings = {'version': 2, 'duration': duration, 'sample_rate': SAMPLE_RATE,
                'ding_source': 'ding.mp3' if digest else 'synthesized', 'ding_sha256': digest,
                'fade_seconds': FADE_SECONDS, 'exit_gap': EXIT_GAP}
    cached = None
    if marker.is_file():
        try:
            cached = json.loads(marker.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    if track.is_file() and cached == settings:
        if abs(wav_duration(track) - duration) > 1 / SAMPLE_RATE:
            raise ValueError(f'Part SFX must be {duration} seconds: {track}')
        return track
    if digest:
        bell = read_custom_ding(custom, ffmpeg)
    else:
        bell = []
        for i in range(round(1.35 * SAMPLE_RATE)):
            t = i / SAMPLE_RATE
            attack = min(1.0, t / 0.006)
            tail = min(1.0, (1.35 - t) / 0.08)
            value = sum(amp * math.sin(2 * math.pi * 1046.5 * ratio * t) * math.exp(-decay * t)
                        for ratio, amp, decay in ((1, 1, 3.8), (2.756, .28, 6), (5.404, .07, 12)))
            bell.append(.105 * value * attack * tail)
    noise = random.Random(7)
    swish, low, broad = [], 0.0, 0.0
    for i in range(round(FADE_SECONDS * SAMPLE_RATE)):
        progress = i / (FADE_SECONDS * SAMPLE_RATE)
        sample = noise.uniform(-1, 1)
        low += .04 * (sample - low)
        broad += (.12 + .4 * progress) * (sample - broad)
        swish.append((broad - low) * math.sin(math.pi * progress) ** 1.6)
    peak = max(abs(s) for s in swish)
    swish = [s * .075 / peak for s in swish]
    mixed = [0.0] * round(duration * SAMPLE_RATE)
    for samples, start in ((bell, .03), (swish, duration - EXIT_GAP - FADE_SECONDS)):
        offset = round(start * SAMPLE_RATE)
        for i, sample in enumerate(samples):
            if offset + i < len(mixed):
                mixed[offset + i] += sample
    # Leave a clean gap before the lesson starts, including the shorter Part 1 card.
    for i in range(round((duration - EXIT_GAP) * SAMPLE_RATE), len(mixed)):
        mixed[i] = 0.0
    write_pcm(folder / 'ding.wav', bell)
    write_pcm(folder / 'swish.wav', swish)
    write_pcm(track, mixed)
    temporary = marker.with_suffix('.partial.json')
    temporary.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    temporary.replace(marker)
    (folder / 'README.txt').write_text(
        'Optional custom bell: put/replace ding.mp3 here. Original MP3 is never changed.\n'
        'Mixed Part tracks rebuild automatically when the MP3 content changes.\n'
        'ding.wav: prepared bell, maximum 1.35s, short fade-out, soft peak level.\n'
        'Without ding.mp3, the bell is synthesized locally. swish.wav stays synthesized.\n'
        'Part cards: bell at 0.03s, swish during the exit fade, quiet final 0.12s.\n'
        'Part 1 lasts 1.5s; Parts 2 and 3 last 2.5s.\n',
        encoding='utf-8')
    return track
