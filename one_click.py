#!/usr/bin/env python3
"""Band 7 Radio: a dialogue TXT file -> one complete, three-pass listening video."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
import wave

from dialogue_script import format_turn, read_turns
import video_extras as extras

GEMINI_EN_DELIVERY = 'classic-long-context-v3'

COLOR_BG = '#000000'        # Màu đen (hoặc dùng '#000000')
TEXT_PRIMARY = '#FFFFFF'    # Màu trắng
TEXT_SECONDARY = '#00E5FF'  # Màu vàng hổ phách (nổi bật trên nền đen và khác màu trắng)
# Sizes use the 1920x1080 layout; FFmpeg scales the whole frame on export.
EN_FONT_SIZE = 88
VI_FONT_SIZE = 60
VI_FONT_PATH = Path(__file__).resolve().parent / 'assets/fonts/Nunito-SemiBold.ttf'
SPEAKER_FONT_SIZE = 32
TEXT_LINE_GAP = 16  # Extra pixels between text lines in the 1080p layout.
TEXT_MARGIN_X = 100
BILINGUAL_TEXT_TOP = 160
BILINGUAL_TEXT_BOTTOM = 920
W, H = 1920, 1080


@dataclass(frozen=True)
class VideoLayout:
    width: int = W
    height: int = H
    en_size: int = EN_FONT_SIZE
    vi_size: int = VI_FONT_SIZE
    margin_x: int = TEXT_MARGIN_X
    bilingual_top: int = BILINGUAL_TEXT_TOP
    bilingual_bottom: int = BILINGUAL_TEXT_BOTTOM
    scroll_top: int = 50
    scroll_bottom: int = 940
    scroll_center: int = 540
    scroll_logo_y: int = 1002
    listening_logo_y: int = 400
    listening_logo_size: int = 112
    wave_y: int = 560

    @property
    def portrait(self):
        return self.height > self.width


LANDSCAPE = VideoLayout()
PORTRAIT = VideoLayout(width=1080, height=1920, en_size=64, vi_size=48, margin_x=130,
                       bilingual_top=280, bilingual_bottom=1480,
                       scroll_top=220, scroll_bottom=1480, scroll_center=820,
                       scroll_logo_y=1600, listening_logo_y=760,
                       listening_logo_size=78, wave_y=940)


def layout_for_resolution(resolution):
    return PORTRAIT if resolution[1] > resolution[0] else LANDSCAPE


def output_resolutions(quality, video_format):
    horizontal = {'720p': (1280, 720), '1080p': (1920, 1080), '1440p': (2560, 1440)}[quality]
    vertical = horizontal[::-1]
    return {'landscape': [horizontal], 'portrait': [vertical], 'both': [horizontal, vertical]}[video_format]


def prompt_choice(title, options, default):
    print('\n' + title, flush=True)
    for index, (_, label) in enumerate(options, 1):
        print(f'  {index}. {label}', flush=True)
    while True:
        try:
            answer = input(f'Chọn 1–{len(options)} [Enter = {default}]: ').strip()
        except EOFError:
            raise RuntimeError('Chưa chọn xong. Chạy lại trong Terminal hoặc dùng --level và --video-format.') from None
        answer = answer or str(default)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer)-1][0]
        print('Lựa chọn không hợp lệ. Vui lòng nhập số trong menu.', flush=True)


def choose_creation_options(args):
    interactive = (args.interactive or sys.stdin.isatty()) and not (
        args.dry_run or args.demo or args.preview_extras)
    if args.level is None:
        args.level = (prompt_choice('CHỌN TRÌNH ĐỘ GIỌNG ĐỌC', (
            ('a1-a2', 'A1–A2: chậm, rõ, nghỉ giữa câu lâu hơn (Gemini)'),
            ('b1-b2', 'B1–B2: nhịp hội thoại tự nhiên như hiện tại'),
        ), 2) if interactive else 'b1-b2')
    if args.video_format is None:
        args.video_format = 'landscape'


@dataclass
class Cue:
    id: int
    speaker: str
    en: str
    first: int
    last: int
    start: float = 0.0
    end: float = 0.0
    vi: str = ''
    word_times: list[tuple[float, float]] | None = None


def run(cmd, *, cwd=None):
    print(' >', ' '.join(str(x) for x in cmd[:6]), '...')
    subprocess.run([str(x) for x in cmd], cwd=cwd, check=True)


def save_json(path, value):
    temporary = path.with_suffix('.partial.json')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def check_run_inputs(work, settings):
    """Lock English/audio settings; Vietnamese edits only require rendering again."""
    marker = work/'inputs.json'
    if marker.exists():
        previous = json.loads(marker.read_text(encoding='utf-8'))
        # Ignore old API translation settings to reuse audio from existing episodes.
        translation_keys = ('translate_model', 'translate_provider', 'script_vi')
        old_audio = {k: v for k, v in previous.items() if k not in translation_keys}
        new_audio = {k: v for k, v in settings.items() if k not in translation_keys}
        # Existing recordings predate level selection and use B1-B2 delivery.
        old_audio.setdefault('english_level', 'b1-b2')
        new_audio.setdefault('english_level', 'b1-b2')
        if old_audio != new_audio:
            raise RuntimeError('Script/audio/voice settings changed. Use a NEW --episode name to keep old files safe.')
        if previous != settings:
            save_json(marker, settings)
    elif (work/'script.sha256').exists():
        raise RuntimeError('This episode has a legacy cache. Use a NEW --episode name; optionally --reuse-audio with its original WAV.')
    else:
        save_json(marker, settings)


def find_ffmpeg():
    """Choose a build with ASS subtitles before generating any paid audio."""
    candidates = [shutil.which('ffmpeg')]
    if sys.platform == 'darwin':
        brew = shutil.which('brew')
        if brew:
            result = subprocess.run([brew, '--prefix', 'ffmpeg-full'], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                candidates.append(str(Path(result.stdout.strip()) / 'bin' / 'ffmpeg'))
    for binary in candidates:
        if not binary or not Path(binary).is_file():
            continue
        result = subprocess.run([binary, '-hide_banner', '-filters'], capture_output=True, text=True)
        if result.returncode == 0 and all(re.search(r'^\s*\S+\s+'+name+r'\s', result.stdout, re.MULTILINE)
                                          for name in ('ass', 'showwaves')):
            print('Using FFmpeg:', binary)
            return binary
    raise RuntimeError(
        'FFmpeg with the ASS subtitle filter is required. '
        'On Mac, run: brew install ffmpeg-full. Then re-run this same command.'
    )


def norm(word, language='en'):
    if language == 'zh':
        return ''.join(c for c in unicodedata.normalize('NFKC', word).casefold() if c.isalnum())
    return re.sub(r'[^a-z0-9]', '', word.lower())


def speech_units(text, language='en'):
    if language == 'zh':
        text = unicodedata.normalize('NFKC', text)
        return re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9]+(?:[\x27’][A-Za-z]+)*', text)
    return text.split()


def part_sort(path):
    m = re.search(r'(\d+)', path.stem)
    return (int(m.group(1)) if m else 0, path.name)


def parse_parts(paths, *, language='en'):
    """Keep one caption per Mia:/Alex: turn, regardless of word or sentence count."""
    cues, whole_words = [], []
    for turn in read_turns(paths):
        first = len(whole_words)
        words = speech_units(turn['text'], language)
        if not words:
            raise ValueError('A dialogue turn contains no spoken text.')
        whole_words.extend(words)
        text = turn['text'] if language == 'zh' else ' '.join(words)
        cues.append(Cue(len(cues)+1, turn['speaker'], text, first, len(whole_words)-1))
    return cues, whole_words


def read_bilingual_cues(paths, vietnamese_path, *, language='en'):
    """Pair whole dialogue turns, without guessing how Vietnamese words map to English."""
    if not vietnamese_path.is_file() or not vietnamese_path.read_text(encoding='utf-8-sig').strip():
        raise ValueError(f'Vietnamese script missing or empty: {vietnamese_path}. Add your Vietnamese Mia:/Alex: dialogue.')
    english_turns = read_turns(paths)
    vietnamese_turns = read_turns([vietnamese_path])
    if len(english_turns) != len(vietnamese_turns):
        source_label = 'ZH' if language == 'zh' else 'EN'
        raise ValueError(
            f'Scripts have different turn counts: {source_label}={len(english_turns)}, VI={len(vietnamese_turns)}. '
            'Keep one Vietnamese Mia:/Alex: turn for each English turn, in the same order.'
        )
    cues, first = [], 0
    for index, (en, vi) in enumerate(zip(english_turns, vietnamese_turns), 1):
        if en['speaker'] != vi['speaker']:
            raise ValueError(f'Speaker mismatch at turn {index}: EN={en["speaker"]}, VI={vi["speaker"]}.')
        last = first + len(speech_units(en['text'], language)) - 1
        cues.append(Cue(index, en['speaker'], en['text'], first, last, vi=vi['text']))
        first = last + 1
    return cues


def plan_parts(paths, max_words=350, *, language='en', max_characters=800, input_fits=None):
    """Group complete dialogue turns within audio and estimated input budgets."""
    groups, current, count = [], [], 0

    def append(piece, units, limit):
        nonlocal current, count
        if current and (count + units > limit or
                        (input_fits is not None and not input_fits(current + [piece]))):
            groups.append(current)
            current, count = [], 0
        current.append(piece)
        count += units

    for turn in read_turns(paths):
        if language == 'zh':
            text = turn['text']
            # Most requests keep complete turns together; exceptionally long turns split at punctuation.
            while text:
                end = min(len(text), max_characters)
                if len(text) > end:
                    boundary = max(text.rfind(mark, 0, end) for mark in '。！？；，')
                    if boundary >= end//2:
                        end = boundary+1
                chunk, text = text[:end], text[end:]
                append({**turn, 'text': chunk}, len(chunk), max_characters)
            continue
        words = turn['text'].split()
        while words:
            end = min(len(words), max_words)
            piece = {**turn, 'text': ' '.join(words[:end])}
            while input_fits is not None and not input_fits([piece]):
                if end == 1:
                    raise ValueError('A script word exceeds the Gemini input budget. Shorten it before generating.')
                end = max(1, end//2)
                piece = {**turn, 'text': ' '.join(words[:end])}
            append(piece, end, max_words)
            words = words[end:]
    if current:
        groups.append(current)
    # Avoid a final request containing only a few isolated lines. Moving whole
    # turns keeps every word, emotion and speaker in its original order.
    if language == 'en':
        size = lambda group: sum(len(t['text'].split()) for t in group)
        for index in range(len(groups)-1, 0, -1):
            previous, last = groups[index-1], groups[index]
            while len(previous) > 1 and size(last) < size(previous)/2:
                candidate = [previous[-1]] + last
                if size(candidate) > max_words or (input_fits is not None and not input_fits(candidate)):
                    break
                last.insert(0, previous.pop())
    return groups


def gemini_part_options(model, mia_voice, alex_voice, *, level='b1-b2'):
    import generate
    if model not in generate.LONG_CONTEXT_MODELS:
        return {}

    def fits(turns):
        payload = generate.build_payload(turns, model, mia_voice, alex_voice, level=level)
        return generate.estimate_input_tokens(payload) <= generate.INPUT_TOKEN_BUDGET

    return {'max_words': generate.max_english_words(level), 'input_fits': fits}


def prepare_parts(paths, work, max_words=350, *, language='en', max_characters=800, input_fits=None):
    folder = work / 'input_parts'
    folder.mkdir(exist_ok=True)
    groups = plan_parts(paths, max_words, language=language,
                        max_characters=max_characters, input_fits=input_fits)
    outputs = []
    for index, group in enumerate(groups, 1):
        path = folder / f'part{index:03d}.txt'
        path.write_text('\n\n'.join(format_turn(turn) for turn in group)+'\n', encoding='utf-8')
        outputs.append(path)
    return outputs


def wav_duration(path):
    with wave.open(str(path), 'rb') as wav:
        frames = wav.getnframes()
        if frames == 0:
            raise RuntimeError(f'Audio is empty: {path}')
        wav.setpos(frames-1)
        if len(wav.readframes(1)) != wav.getnchannels()*wav.getsampwidth():
            raise RuntimeError(f'Audio is truncated: {path}. Restore the complete file before resuming.')
        return frames / wav.getframerate()


def ensure_audio(root, parts, work, final_audio, generator, lead, *,
                 model, mia_voice, alex_voice, existing_tts_dir=None, ffmpeg='ffmpeg', language='en',
                 level='b1-b2'):
    # main() checks script/voice settings before allowing any cached audio reuse.
    if final_audio.is_file():
        duration = wav_duration(final_audio)
        print('Reuse complete audio (skip TTS):', final_audio.name)
        return duration
    raw = work / 'joined_unpadded.wav'
    if raw.is_file():
        wav_duration(raw)
        print('Reuse joined audio (skip TTS):', raw.name)
    else:
        tts_dir = work / 'tts'
        tts_dir.mkdir(parents=True, exist_ok=True)
        wavs = []
        for path in parts:
            output = tts_dir / (path.stem + '.wav')
            wavs.append(output)
            if output.is_file():
                wav_duration(output)
                print('Reuse existing TTS:', output.name)
                continue
            if existing_tts_dir and (existing_tts_dir / output.name).is_file():
                print('Copying existing TTS (ensure same script):', output.name)
                temporary = output.with_suffix('.partial.wav')
                shutil.copyfile(existing_tts_dir / output.name, temporary)
                wav_duration(temporary)
                temporary.replace(output)
                continue
            if not generator.is_file():
                raise FileNotFoundError(f'Cannot find {generator}. Copy one_click.py beside your WORKING generate.py.')
            command = [sys.executable, generator, '--parts', path.resolve(), '--outdir', tts_dir.resolve(),
                       '--model', model, '--mia-voice', mia_voice, '--alex-voice', alex_voice]
            if language != 'en':
                command += ['--language', language]
            elif level != 'b1-b2':
                command += ['--level', level]
            run(command, cwd=root)
            if not output.is_file():
                raise RuntimeError(f'Expected audio not created: {output}')
            wav_duration(output)
        concat = work / 'wav_list.txt'
        concat.write_text(''.join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in wavs), encoding='utf-8')
        run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', concat,
             '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', raw.with_suffix('.partial.wav')])
        raw.with_suffix('.partial.wav').replace(raw)
    run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', raw,
         '-af', f'adelay={int(lead*1000)}', '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le',
         final_audio.with_suffix('.partial.wav')])
    final_audio.with_suffix('.partial.wav').replace(final_audio)
    return wav_duration(final_audio)


def transcribe_words(audio, model_name, cache, *, offline=False, language='en'):
    if cache.exists():
        return json.loads(cache.read_text(encoding='utf-8'))
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError('Missing faster-whisper. Run: python3 -m pip install faster-whisper') from None
    print(f'Analyzing REAL speech timings with faster-whisper ({model_name}), CPU/int8...')
    model_options = {'device': 'cpu', 'compute_type': 'int8'}
    if offline:
        model_options['local_files_only'] = True
    model = WhisperModel(model_name, **model_options)
    options = {'language': language, 'word_timestamps': True, 'beam_size': 5, 'vad_filter': False}
    if language == 'zh':
        options['initial_prompt'] = '以下是普通话对话，使用简体中文。'
    segments, info = model.transcribe(str(audio), **options)
    words = []
    for segment in segments:
        for item in segment.words or []:
            s = norm(item.word, language)
            if s and item.start is not None and item.end is not None:
                if language == 'zh':
                    units = speech_units(item.word, language)
                    # Whisper can timestamp a Chinese word containing multiple characters.
                    # Distribute its duration within that word, retaining the recognizer's anchors.
                    for index, unit in enumerate(units):
                        start = float(item.start) + (item.end-item.start)*index/len(units)
                        end = float(item.start) + (item.end-item.start)*(index+1)/len(units)
                        words.append({'word': unit, 'start': start, 'end': end})
                else:
                    words.append({'word': item.word, 'start': float(item.start), 'end': float(item.end)})
    if not words:
        raise RuntimeError('Speech recognizer found no spoken words in audio')
    save_json(cache, words)
    return words


def align_cues(cues, script_words, recognized, duration, lead, *, language='en'):
    ref = [norm(w, language) for w in script_words]
    hyp = [norm(w['word'], language) for w in recognized]
    matcher = SequenceMatcher(None, ref, hyp, autojunk=False)
    times = [None] * len(ref)
    matched = 0
    for a, b, size in matcher.get_matching_blocks():
        for off in range(size):
            item = recognized[b+off]
            times[a+off] = (float(item['start']), float(item['end']))
            matched += 1
    if not ref or duration <= lead:
        raise RuntimeError('Empty script or audio shorter than the lead-in.')
    fraction = matched / len(ref)
    print(f'Matched {matched}/{len(ref)} scripted words to audio: {fraction:.0%}')
    if fraction < 0.65:
        raise RuntimeError('Low alignment score (<65%). Check that audio and scripts are for the SAME episode; will not output misleading synced videos.')

    known = [i for i, val in enumerate(times) if val is not None]
    # Interpolate times for skipped/hallucinated ASR words between secure anchors.
    for idx in range(len(times)):
        if times[idx] is not None:
            continue
        pos = bisect_left(known, idx)
        left = known[pos-1] if pos else None
        right = known[pos] if pos < len(known) else None
        if left is not None and right is not None:
            l_end, r_start = times[left][1], times[right][0]
            ratio = (idx - left) / (right - left)
            at = l_end + ratio * max(0.01, r_start - l_end)
        elif left is not None:
            at = times[left][1] + (idx-left)*0.32
        elif right is not None:
            at = max(lead, times[right][0] - (right-idx)*0.32)
        else:
            at = lead + idx * (duration-lead)/max(1, len(times))
        times[idx] = (max(0, min(duration, at)), max(0, min(duration, at+0.20)))
    prev_end = max(0, lead-0.05)
    for cue in cues:
        starts = [times[i][0] for i in range(cue.first, cue.last+1)]
        ends = [times[i][1] for i in range(cue.first, cue.last+1)]
        cue.start = max(prev_end, min(starts)-0.035)
        cue.end = max(cue.start+0.15, min(duration, max(ends)+0.08))
        cue.word_times = times[cue.first:cue.last+1]
        prev_end = cue.end
    # If ASR contains small timestamp jumps, ensure last subtitle stops within file.
    if cues[-1].end > duration+0.1:
        raise RuntimeError('Transcript duration exceeds audio duration. Inspect timestamps.')
    return fraction


def srt_time(t):
    ms = max(0, round(t*1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def save_srt(cues, fn, which):
    output = []
    for c in cues:
        label = {'en': c.en, 'vi': c.vi, 'both': c.en+'\n'+c.vi}[which]
        output.append(f'{c.id}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{label}\n')
    fn.write_text('\n'.join(output), encoding='utf-8')


def ass_color(value):
    value = value.lstrip('#')
    return '&H00'+value[4:6]+value[2:4]+value[:2]


def ass_escape(s):
    return s.replace('\\', ' ').replace('{', '(').replace('}', ')').replace('\n', ' ')


def wrap_text(text, font, width):
    """Measure pixels so English and Vietnamese both stay inside the frame."""
    if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', text):
        return wrap_chinese(text, font, width)
    lines, line = [], ''
    for word in text.split():
        candidate = (line+' '+word).strip()
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ''
        if font.getlength(word) <= width:
            line = word
            continue
        # Break unusually long tokens visually, without changing SRT content.
        for char in word:
            if line and font.getlength(line+char) > width:
                lines.append(line)
                line = ''
            line += char
    if line:
        lines.append(line)
    return lines


def wrap_chinese(text, font, width):
    """Wrap Chinese without spaces, preserving Latin words and Chinese punctuation."""
    closing = set('，。！？；：、）》】〉」』”’…,.!?;:%')
    opening = set('（《【〈「『“‘(')
    lines, line = [], ''
    tokens = re.findall(r'[A-Za-z0-9]+(?:[\x27’][A-Za-z]+)*|\s+|.', text)
    for token in tokens:
        # Oversized embedded Latin words still need to stay inside the frame.
        pieces = list(token) if font.getlength(token) > width else [token]
        for piece in pieces:
            if line and font.getlength(line+piece) > width:
                moved = ''
                while line and (line[-1] in opening or (piece[0] in closing and not moved)):
                    suffix = re.search(r'[A-Za-z0-9]+$', line) if piece[0] in closing else None
                    tail = suffix.group() if suffix else line[-1]
                    moved = tail + moved
                    line = line[:-len(tail)]
                if line.strip():
                    lines.append(line.rstrip())
                line = moved
            line += piece
            if not line.strip():
                line = ''
    if line.strip():
        lines.append(line.rstrip())
    return lines


def ass_timestamp(seconds):
    cs = max(0, round(seconds*100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


@lru_cache(maxsize=16)
def caption_font_info(font_path):
    """Match Pillow's pixel measurements to libass's Windows font metrics."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        raise RuntimeError('Missing fonttools. Run: python3 -m pip install -r requirements.txt') from None
    with TTFont(str(font_path), lazy=True, fontNumber=0) as font:
        names = font['name']
        # libass/fontconfig uses the Windows family; Pillow can report a different Mac family.
        family = next((r.toUnicode() for r in names.names
                       if r.nameID == 1 and r.platformID == 3 and r.langID == 1033),
                      names.getBestFamilyName())
        metrics = font['OS/2']
        scale = (metrics.usWinAscent + metrics.usWinDescent) / font['head'].unitsPerEm
    if not family or ',' in family or scale <= 0:
        raise ValueError(f'Unsupported subtitle font metadata: {font_path}')
    return family, scale


def ass_header(font, vi_font='Nunito', *, en_scale=1.0, vi_scale=1.0, layout=LANDSCAPE):
    return f'''[Script Info]
ScriptType: v4.00+
PlayResX: {layout.width}
PlayResY: {layout.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: EN,{font},{layout.en_size},{ass_color(TEXT_PRIMARY)},&H00000000,&H00000000,&H00000000,-1,0,0,0,{en_scale*100:.3f},{en_scale*100:.3f},0,0,1,0,0,8,{layout.margin_x},{layout.margin_x},10,1
Style: VI,{vi_font},{layout.vi_size},{ass_color(TEXT_SECONDARY)},&H00000000,&H00000000,&H00000000,-1,0,0,0,{vi_scale*100:.3f},{vi_scale*100:.3f},0,0,1,0,0,8,{layout.margin_x},{layout.margin_x},10,1
Style: Speaker,{font},{SPEAKER_FONT_SIZE},{ass_color(TEXT_SECONDARY)},&H00000000,&H00000000,&H00000000,0,0,0,0,{en_scale*100:.3f},{en_scale*100:.3f},0,0,1,0,0,8,{layout.margin_x},{layout.margin_x},10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''


def write_ass(cues, path, font_path, vi_font_path=None, *, layout=LANDSCAPE):
    from PIL import ImageFont
    vi_font_path = Path(vi_font_path) if vi_font_path else VI_FONT_PATH
    family, en_scale = caption_font_info(font_path)
    vi_family, vi_scale = caption_font_info(vi_font_path)
    width = layout.width - 2*layout.margin_x
    available_height = layout.bilingual_bottom - layout.bilingual_top
    events = []
    for c in cues:
        scale = 1.0
        while True:
            # Reduce both languages by the SAME fraction, keeping English visually larger.
            en_size, vi_size = round(layout.en_size*scale), round(layout.vi_size*scale)
            en = wrap_text(ass_escape(c.en), ImageFont.truetype(str(font_path), en_size), width)
            vi = wrap_text(ass_escape(c.vi), ImageFont.truetype(str(vi_font_path), vi_size), width)
            en_height = len(en)*(en_size+TEXT_LINE_GAP)
            vi_height = len(vi)*(vi_size+TEXT_LINE_GAP)
            height = en_height + vi_height + 36
            if height <= available_height:
                break
            if en_size <= 34 or vi_size <= 24:
                raise RuntimeError(f'Caption {c.id} is too long to display. Shorten its Vietnamese translation.')
            scale -= .02
        s, e = ass_timestamp(c.start), ass_timestamp(c.end)
        top = layout.bilingual_top + (available_height-height)/2
        for style, lines, size, y in [('EN', en, en_size, top),
                                      ('VI', vi, vi_size, top+en_height+36)]:
            for index, line in enumerate(lines):
                line_y = y + index*(size+TEXT_LINE_GAP)
                events.append(f'Dialogue: 0,{s},{e},{style},,0,0,0,,{{\\pos({layout.width//2},{line_y:.1f})\\fs{size}}}{line}')
    path.write_text(ass_header(family, vi_family, en_scale=en_scale, vi_scale=vi_scale, layout=layout)+'\n'.join(events)+'\n', encoding='utf-8')


def choose_font_path():
    preferred = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    ]
    for f in preferred:
        if Path(f).is_file(): return f
    raise RuntimeError('Could not find a font. Install Poppins or Arial locally, or adapt choose_font_path().')


def scroll_rows_and_anchors(cues, font, layout):
    """Place transcript rows and find when each spoken line reaches its midpoint."""
    line_height = layout.en_size+TEXT_LINE_GAP
    speaker_height = SPEAKER_FONT_SIZE+TEXT_LINE_GAP
    rows, anchors = [], []
    top = 80
    for cue in cues:
        text = ass_escape(cue.en)
        width = layout.width-2*layout.margin_x
        lines = wrap_text(text, font, width)
        words = text.split()
        word_ends, count = [], 0
        for word in words:
            count += len(word)
            word_ends.append(count)
        real_times = (cue.word_times if cue.word_times and len(cue.word_times) == len(words)
                      and all(font.getlength(word) <= width for word in words) else None)
        rows.append(('Speaker', cue.speaker.upper(), top, speaker_height))
        consumed = 0
        for index, line in enumerate(lines):
            y = top+speaker_height+index*line_height
            rows.append(('EN', line, y, line_height))
            characters = len(''.join(line.split()))
            if real_times:
                first = min(len(words)-1, bisect_right(word_ends, consumed))
                last = min(len(words)-1, bisect_left(word_ends, consumed+characters))
                at = (real_times[first][0]+real_times[last][1])/2
            else:
                # Old timing caches still work; a fresh alignment adds real word times.
                at = cue.start+(cue.end-cue.start)*(consumed+characters/2)/max(1, count)
            anchors.append((at, layout.scroll_center+20-(y+layout.en_size/2)))
            consumed += characters
        top += speaker_height+len(lines)*line_height+60
    return rows, anchors


def smooth_scroll_points(anchors, duration, start_offset, end_offset):
    """Smooth the complete motion over a one-second window, then keep subpixel accuracy."""
    total = max(.01, round(duration*100)/100)
    knots = [(0.0, start_offset)]
    for at, offset in anchors:
        if knots[-1][0]+.005 < at < total and offset < knots[-1][1]:
            knots.append((at, offset))
    knots.append((total, min(end_offset, knots[-1][1]-.01)))
    step = .05
    times = [round(i*step, 2) for i in range(math.ceil(total/step))]+[total]
    positions, index = [], 0
    for at in times:
        while index+1 < len(knots)-1 and at > knots[index+1][0]:
            index += 1
        (t0, y0), (t1, y1) = knots[index:index+2]
        positions.append(y0+(y1-y0)*(at-t0)/(t1-t0))
    weights = [(i, math.exp(-.5*(i*step)**2)) for i in range(-60, 61)]
    normalizer = sum(weight for _, weight in weights)
    def position(index):
        # Linear extension preserves motion at the beginning/end of the clip.
        if index < 0:
            return positions[0]+index*(positions[1]-positions[0])
        if index >= len(positions):
            return positions[-1]+(index-len(positions)+1)*(positions[-1]-positions[-2])
        return positions[index]
    points = [(at, sum(position(index+i)*weight for i, weight in weights)/normalizer)
              for index, at in enumerate(times)]
    # Keep only bends that matter visually. Linear ASS segments stay within
    # 0.05 pixels of the smoothed path, without thousands of unnecessary events.
    keep, pending = {0, len(points)-1}, [(0, len(points)-1)]
    while pending:
        first, last = pending.pop()
        t0, y0 = points[first]
        t1, y1 = points[last]
        largest, split = .05, None
        for index in range(first+1, last):
            at, y = points[index]
            error = abs(y-(y0+(y1-y0)*(at-t0)/(t1-t0)))
            if error > largest:
                largest, split = error, index
        if split is not None:
            keep.add(split)
            pending.extend(((first, split), (split, last)))
    return [points[index] for index in sorted(keep)]


def write_scroll_ass(cues, duration, path, font_path, *, layout=LANDSCAPE):
    """Keep spoken lines near the reading area with smooth, gradual motion."""
    from PIL import ImageFont
    if not cues or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError('Scrolling captions need text and a positive audio duration.')
    font = ImageFont.truetype(str(font_path), layout.en_size)
    family, en_scale = caption_font_info(font_path)
    rows, anchors = scroll_rows_and_anchors(cues, font, layout)
    first_line = rows[1][2]+layout.en_size/2
    last_line = rows[-1][2]+layout.en_size/2
    points = smooth_scroll_points(anchors, duration,
                                  layout.scroll_center+60-first_line,
                                  layout.scroll_center-40-last_line)
    tops = [row[2] for row in rows]
    bottoms = [row[2]+row[3] for row in rows]
    events = []
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        first = bisect_left(bottoms, layout.scroll_top-max(y0, y1))
        last = bisect_right(tops, layout.scroll_bottom-min(y0, y1))
        for style, value, y, height in rows[first:last]:
            motion = (f'{{\\move({layout.width//2},{y+y0:.3f},{layout.width//2},{y+y1:.3f})'
                      f'\\clip({layout.margin_x},{layout.scroll_top},'
                      f'{layout.width-layout.margin_x},{layout.scroll_bottom})}}')
            events.append(f'Dialogue: 0,{ass_timestamp(t0)},{ass_timestamp(t1)},{style},,0,0,0,,{motion}{value}')
    path.write_text(ass_header(family, en_scale=en_scale, layout=layout)+'\n'.join(events)+'\n', encoding='utf-8')


def make_background(path, mode, title, font_path, logo=None, *, channel_name='Band 7 Radio', cards=None,
                    layout=LANDSCAPE):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    width, height = layout.width, layout.height
    image = Image.new('RGB', (width, height), COLOR_BG)
    draw = ImageDraw.Draw(image)
    if mode == 'overview':
        overview_font = VI_FONT_PATH.with_name('Nunito-ExtraBold.ttf')
        draw.text((width/2, height*.20), 'MỘT ĐOẠN HỘI THOẠI NGẮN',
                  font=ImageFont.truetype(str(overview_font), 44), fill=TEXT_SECONDARY, anchor='mm')
        draw.text((width/2, height*.30), 'NGHE LẠI 3 LẦN ĐỂ LUYỆN TẬP',
                  font=ImageFont.truetype(str(overview_font), 60), fill=TEXT_PRIMARY, anchor='mm')
        for index, (label, description) in enumerate(extras.OVERVIEW_PARTS):
            y = height*(.45+index*.17)
            draw.text((width/2, y), label, font=ImageFont.truetype(str(overview_font), 32),
                      fill=TEXT_SECONDARY, anchor='mm')
            draw.text((width/2, y+height*.045), description,
                      font=ImageFont.truetype(str(overview_font), 48), fill=TEXT_PRIMARY, anchor='mm')
        image.save(path)
        return
    cards = cards if cards is not None else {
        'intro': ('', 'Welcome to Band 7 Radio!\nSubscribe for more English topics.', ''),
        'english_card': ('PART 1 OF 3', 'READ & LISTEN', 'Read the English transcript as you listen.'),
        'bilingual_card': ('PART 2 OF 3', 'TRANSCRIPT + SUBTITLES', 'Listen again with English and Vietnamese subtitles.'),
        'listening_card': ('PART 3 OF 3', 'JUST LISTEN — NO SUBTITLES', 'Focus on what you hear and test your understanding.'),
        'outro': ('', 'Subscribe for more English topics.', ''),
    }
    if mode in cards:
        label, heading, description = cards[mode]
        heading_font = ImageFont.truetype(str(font_path), 60)
        if layout.portrait:
            description_font = ImageFont.truetype(str(font_path), 40)
            label_font = ImageFont.truetype(str(font_path), 32)
            def wrapped(text, font):
                return '\n'.join(line for paragraph in text.splitlines()
                                 for line in wrap_text(paragraph, font, width-2*layout.margin_x))
            heading = wrapped(heading, heading_font)
            description = wrapped(description, description_font)
            heading_box = draw.multiline_textbbox((0, 0), heading, font=heading_font, spacing=18)
            heading_height = heading_box[3]-heading_box[1]
            center_y = height*.45
            draw.multiline_text((width/2, center_y), heading, font=heading_font,
                                fill=TEXT_PRIMARY, anchor='mm', align='center', spacing=18)
            if label:
                draw.text((width/2, center_y-heading_height/2-70), label,
                          font=label_font, fill=TEXT_SECONDARY, anchor='mm')
            if description:
                description_box = draw.multiline_textbbox((0, 0), description, font=description_font, spacing=14)
                description_height = description_box[3]-description_box[1]
                draw.multiline_text((width/2, center_y+heading_height/2+50+description_height/2), description,
                                    font=description_font, fill=TEXT_SECONDARY,
                                    anchor='mm', align='center', spacing=14)
            image.save(path)
            return
        draw.multiline_text((W/2, 500 if label else H/2), heading,
                            font=heading_font, fill=TEXT_PRIMARY, anchor='mm', align='center', spacing=18)
        if label:
            draw.text((W/2, 360), label, font=ImageFont.truetype(str(font_path), 32),
                      fill=TEXT_SECONDARY, anchor='mm')
            draw.text((W/2, 620), description, font=ImageFont.truetype(str(font_path), 40),
                      fill=TEXT_SECONDARY, anchor='mm')
        image.save(path)
        return
    if mode == 'bilingual':
        image.save(path)
        return
    if mode == 'scroll':
        logo_y, logo_size = layout.scroll_logo_y, 34
    else:
        logo_y, logo_size = layout.listening_logo_y, layout.listening_logo_size
    if logo:
        with Image.open(logo) as original:
            fitted = ImageOps.contain(original.convert('RGBA'), (logo_size*7, logo_size*2))
        image.paste(fitted, ((width-fitted.width)//2, int(logo_y-fitted.height/2)), fitted)
    else:
        logo_font_path = '/System/Library/Fonts/Supplemental/Georgia Bold.ttf'
        if not Path(logo_font_path).is_file():
            logo_font_path = str(font_path)
        draw.text((width/2, logo_y), channel_name, anchor='mm',
                  font=ImageFont.truetype(logo_font_path, logo_size),
                  fill='white', stroke_width=max(1, logo_size//22), stroke_fill=TEXT_PRIMARY)
    image.save(path)


def render_section(ffmpeg, image, audio, duration, output, work, *, subtitles=None,
                   wave_y=None, resolution=(1920, 1080), fps=30,
                   card_motion=False, audio_seek=0):
    layout = layout_for_resolution(resolution)
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-stats',
           '-loop', '1', '-framerate', str(fps), '-i', image]
    if audio:
        if audio_seek:
            cmd += ['-ss', f'{audio_seek:.6f}']
        cmd += ['-i', audio]
    else:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono']
    graph = '[0:v]format=rgba[base];'
    if card_motion:
        exit_at = duration - extras.EXIT_GAP - extras.FADE_SECONDS
        color = COLOR_BG.replace('#', '0x')
        graph = (f'[0:v]format=rgba,fade=t=in:st=0:d={extras.FADE_SECONDS}:alpha=1,'
                 f'fade=t=out:st={exit_at:.6f}:d={extras.FADE_SECONDS}:alpha=1[card];'
                 f'color=c={color}:s={layout.width}x{layout.height}:r={fps}[canvas];'
                 f"[canvas][card]overlay=x=0:y='if(lt(t,{extras.FADE_SECONDS}),"
                 f"60*(1-t/{extras.FADE_SECONDS}),-max(0,(t-{exit_at:.6f})/{extras.FADE_SECONDS})*90)':"
                 'shortest=1[base];')
    graph += '[1:a]apad,aresample=48000[padded];'
    if wave_y is not None:
        graph += (f'[padded]asplit=2[sound][wave];[wave]'
                  f'showwaves=s=480x100:mode=cline:rate={fps}:colors={TEXT_PRIMARY.replace("#", "0x")}:scale=sqrt:draw=full,'
                  f'format=rgba[w];[base][w]overlay=x=(W-w)/2:y={wave_y}:eof_action=pass[composed];')
        base, audio_map = '[composed]', '[sound]'
    else:
        base, audio_map = '[base]', '[padded]'
    if subtitles:
        graph += f'{base}ass=filename={subtitles.name}:fontsdir=fonts[text];'
        base = '[text]'
    width, height = resolution
    graph += f'{base}scale={width}:{height},setsar=1,format=yuv420p[v]'
    temporary = output.with_suffix('.partial.mkv')
    # Frame-aligned durations and PCM intermediates avoid AAC padding at each join.
    cmd += ['-filter_complex', graph, '-map', '[v]', '-map', audio_map,
            '-t', f'{duration:.6f}', '-r', str(fps), '-c:v', 'libx264', '-preset', 'veryfast',
            '-crf', '20', '-threads', '4', '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '1', temporary]
    run(cmd, cwd=work)
    temporary.replace(output)


def write_youtube_chapters(final, timeline, *, language='en'):
    """Export copy-ready chapters; part 1 includes the opening card."""
    source, pair = {'en': ('tiếng Anh', 'Anh–Việt'),
                    'zh': ('tiếng Trung', 'Trung–Việt')}[language]
    starts = {section['section']: section['start'] for section in timeline}

    def timestamp(seconds):
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'

    # Jump to the part's title card so viewers can see which pass is starting.
    text = (f'00:00 – Lần 1: Nghe với transcript {source}\n\n'
            f'{timestamp(starts["bilingual_card"])} – Lần 2: Nghe với phụ đề {pair}\n\n'
            f'{timestamp(starts["listening_card"])} – Lần 3: Thử thách nghe không phụ đề\n')
    path = final/'YOUTUBE_CHAPTERS.txt'
    path.write_text(text, encoding='utf-8')
    print('\nMỐC THỜI GIAN — sao chép vào mô tả YouTube:\n' + text, flush=True)
    print('Đã lưu:', path)
    return path


def render_videos(cues, duration, audio, final, work, font_path, *, ffmpeg='ffmpeg',
                  title='Band 7 Radio', logo=None, resolution=(1920, 1080), fps=30,
                  bilingual_cues=None, intro_audio=None, vi_font_path=None,
                  channel_name='Band 7 Radio', cards=None, language='en',
                  extras_config=None, dialogue_lead=0, extras_preview=False):
    layout = layout_for_resolution(resolution)
    if layout.portrait:
        work = work/'vertical'
        work.mkdir(parents=True, exist_ok=True)
    cta_audio, part_sfx, trim = {}, {}, 0.0
    if extras_config is not None:
        root = Path(__file__).resolve().parent
        cta_audio = extras.prepare_cta(root, extras_config, ('closing',))
        for seconds in (extras.FIRST_PART_DURATION, extras.PART_DURATION):
            part_sfx[seconds] = extras.prepare_part_sfx(root, seconds, ffmpeg=ffmpeg)
        # Remove only the artificial lead at render time; keep paid audio/ASR caches intact.
        trim = math.floor(max(0, dialogue_lead - .15) * fps) / fps
        if not extras_preview and trim:
            if trim >= duration:
                raise ValueError('Dialogue lead must be shorter than the audio.')
            cues = [replace(c, start=max(0, c.start-trim), end=max(0, c.end-trim),
                            word_times=[(max(0, a-trim), max(0, b-trim)) for a, b in c.word_times]
                            if c.word_times else None) for c in cues]
            if bilingual_cues is not None:
                bilingual_cues = [replace(c, start=max(0, c.start-trim), end=max(0, c.end-trim))
                                  for c in bilingual_cues]
            duration -= trim
    vi_font_path = Path(vi_font_path) if vi_font_path else VI_FONT_PATH
    if not extras_preview:
        write_ass(bilingual_cues if bilingual_cues is not None else cues, work/'bilingual.ass', font_path,
                  vi_font_path, layout=layout)
        write_scroll_ass(cues, duration, work/'scroll.ass', font_path, layout=layout)
    fonts = work/'fonts'
    fonts.mkdir(exist_ok=True)
    # Copy font bytes only; macOS system-font flags cannot be copied to user files.
    shutil.copyfile(font_path, fonts/Path(font_path).name)
    shutil.copyfile(vi_font_path, fonts/('vi_'+vi_font_path.name))
    regular = Path(font_path).with_name(Path(font_path).name.replace(' Bold', '').replace('-Bold', '-Regular'))
    if regular != Path(font_path) and regular.is_file():
        shutil.copyfile(regular, fonts/regular.name)
    # Every segment uses the same dimensions, frame rate, codec and audio layout.
    speech_duration = math.ceil(duration*fps)/fps
    intro_duration = math.ceil(wav_duration(intro_audio)*fps)/fps if intro_audio else 9.0
    first_card = extras.FIRST_PART_DURATION if part_sfx else 4.0
    other_card = extras.PART_DURATION if part_sfx else 4.0
    outro_duration = math.ceil((wav_duration(cta_audio['closing'])+.15)*fps)/fps if cta_audio else 5.0
    sections = [('intro', intro_duration, None, None), ('english_card', first_card, None, None),
                ('scroll', speech_duration, work/'scroll.ass', None),
                ('bilingual_card', other_card, None, None), ('bilingual', speech_duration, work/'bilingual.ass', None),
                ('listening_card', other_card, None, None), ('listening', speech_duration, None, layout.wave_y),
                ('outro', outro_duration, None, None)]
    if extras_config is not None:
        # Show the listening plan briefly, without generating opening speech.
        if not intro_audio:
            sections.pop(0)
        sections.insert(1 if intro_audio else 0, ('overview', extras.OVERVIEW_DURATION, None, None))
    if extras_preview:
        sections = [section for section in sections if section[0] not in ('scroll', 'bilingual', 'listening')]
    timeline, clips, at = [], [], 0.0
    for index, (mode, seconds, subtitles, wave_y) in enumerate(sections, 1):
        print(f'\nRender {index}/{len(sections)}: {mode} ({seconds:.1f}s)', flush=True)
        background = work/f'{mode}.png'
        make_background(background, mode, title, font_path, logo,
                        channel_name=channel_name, cards=cards, layout=layout)
        clip = work/f'{index:02d}_{mode}.mkv'
        speech = audio if mode in ('scroll', 'bilingual', 'listening') else None
        if mode == 'intro':
            speech = intro_audio
        elif mode == 'outro' and cta_audio:
            speech = cta_audio['closing']
        is_part = mode in ('english_card', 'bilingual_card', 'listening_card') and bool(part_sfx)
        if is_part:
            speech = part_sfx[seconds]
        render_section(ffmpeg, background, speech, seconds, clip, work,
                       subtitles=subtitles, wave_y=wave_y, resolution=resolution, fps=fps,
                       card_motion=is_part, audio_seek=trim if mode in ('scroll', 'bilingual', 'listening') else 0)
        clips.append(clip)
        timeline.append({'section': mode, 'start': at, 'end': at+seconds})
        at += seconds
    concat = work/'video_list.txt'
    concat.write_text(''.join(f"file '{clip.name}'\n" for clip in clips), encoding='utf-8')
    suffix = '_VERTICAL' if layout.portrait else ''
    destination = final/(('PREVIEW_CTA_SFX' if extras_preview else 'VIDEO_FINAL') + suffix + '.mp4')
    temporary = destination.with_suffix('.partial.mp4')
    print('\nJoining sections and normalizing audio...', flush=True)
    run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-stats', '-f', 'concat', '-safe', '0',
         '-i', concat, '-c:v', 'copy', '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11',
         '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', temporary], cwd=work)
    temporary.replace(destination)
    save_json(final/f'timeline{suffix}.json', timeline)
    if not extras_preview:
        write_youtube_chapters(final, timeline, language=language)
    return destination


def render_formats(*args, quality='1080p', video_format='landscape', **kwargs):
    """Render both layouts from the same paid audio and aligned captions."""
    return [render_videos(*args, resolution=resolution, **kwargs)
            for resolution in output_resolutions(quality, video_format)]


def main():
    p = argparse.ArgumentParser(description='Band 7 Radio: dialogue TXT -> one complete VIDEO_FINAL.mp4')
    source = p.add_mutually_exclusive_group()
    source.add_argument('--script', help='One UTF-8 .txt script with Mia:/Alex: dialogue')
    source.add_argument('--parts-dir', help='Existing English part*.txt folder')
    p.add_argument('--script-vi', help='Vietnamese TXT; defaults to kich_ban_tieng_viet/<English filename>')
    p.add_argument('--episode', help='Unique episode name, e.g. EP04; defaults to the script filename')
    p.add_argument('--title', help='Accepted for compatibility; episode titles are no longer drawn on video')
    p.add_argument('--intro-audio', help='Optional custom greeting WAV; default: silent 2-second overview only')
    p.add_argument('--generator', default='generate.py', help='Your existing working TTS generator')
    p.add_argument('--tts-provider', choices=('gemini', 'elevenlabs'), default='gemini',
                   help='gemini or elevenlabs; both use the same video rendering flow')
    p.add_argument('--out-root', default='episodes')
    p.add_argument('--whisper-model', default='base', help='Whisper model: base (fast), small (better)')
    p.add_argument('--tts-model', default=None, help='Defaults to the selected provider model')
    p.add_argument('--mia-voice', default=None, help='Gemini voice name or ElevenLabs Voice ID')
    p.add_argument('--alex-voice', default=None, help='Gemini voice name or ElevenLabs Voice ID')
    p.add_argument('--level', choices=('a1-a2', 'b1-b2'),
                   help='English voice delivery; Terminal asks if omitted, otherwise defaults to B1-B2')
    p.add_argument('--video-format', choices=('landscape', 'portrait', 'both'),
                   help='Optional override for older commands; normal creation defaults to landscape without a format menu')
    p.add_argument('--interactive', action='store_true', help='Show creation menus even when stdin is piped')
    p.add_argument('--lead', type=float, default=2.6, help='Silence before dialogue; existing audio must be unpadded')
    p.add_argument('--font-path', help='English transcript and card font (.ttf/.otf); Vietnamese uses --vi-font-path')
    p.add_argument('--vi-font-path', help='Vietnamese subtitle font; defaults to bundled Nunito ExtraBold')
    p.add_argument('--logo', help='Optional transparent PNG logo; otherwise draw Band 7 Radio text')
    p.add_argument('--resolution', choices=('720p', '1080p', '1440p'), default='1080p')
    p.add_argument('--fps', type=int, choices=(30, 60), default=60,
                   help='Default: 60 fps for smoother continuous transcript scrolling')
    reuse = p.add_mutually_exclusive_group()
    reuse.add_argument('--reuse-audio', help='Existing dialogue audio WITHOUT lead-in silence; skip TTS')
    reuse.add_argument('--reuse-tts-dir', help='Existing partN.wav files for the SAME --parts-dir script')
    p.add_argument('--dry-run', action='store_true', help='Validate script and dependencies without API calls or rendering')
    p.add_argument('--demo', action='store_true', help='Render a short demo with synthetic tones; no API calls')
    p.add_argument('--preview-extras', action='store_true',
                   help='Preview the silent 2-second overview, Part cards with SFX, and outro; creates a missing outro voice once')
    args = p.parse_args()
    choose_creation_options(args)
    if args.level == 'a1-a2' and (args.tts_provider != 'gemini' or args.generator != 'generate.py'):
        p.error('A1–A2 currently uses the built-in Gemini generator. Choose --tts-provider gemini.')
    print(f'Giọng đọc: {args.level.upper()} | Video: {args.video_format} | {args.resolution}', flush=True)
    root = Path(__file__).resolve().parent
    eleven_config = None
    if args.tts_provider == 'gemini':
        args.tts_model = args.tts_model or 'gemini-3.8-flash-tts'
        args.mia_voice = args.mia_voice or 'Kore'
        args.alex_voice = args.alex_voice or 'Puck'
    else:
        import generate_elevenlabs as eleven
        eleven_config = eleven.configuration(args.tts_model, args.mia_voice, args.alex_voice)
        args.tts_model = eleven_config['model']
        args.mia_voice, args.alex_voice = eleven_config['voices']['Mia'], eleven_config['voices']['Alex']
        if args.reuse_tts_dir:
            p.error('--reuse-tts-dir is for Gemini; ElevenLabs resumes its own cached dialogue parts.')
        if args.generator != 'generate.py':
            p.error('--generator custom paths are only supported with --tts-provider gemini.')
    if not math.isfinite(args.lead) or args.lead < 0:
        p.error('--lead must be a finite, non-negative number')
    extras_config = {'provider': args.tts_provider, 'model': args.tts_model,
                     'voice': args.mia_voice, 'delivery': GEMINI_EN_DELIVERY}
    if args.level != 'b1-b2':
        extras_config['level'] = args.level
    if args.preview_extras:
        if args.demo or args.script or args.script_vi or args.parts_dir or args.reuse_audio or args.reuse_tts_dir:
            p.error('--preview-extras cannot be combined with demo/script/dialogue audio inputs.')
        from PIL import ImageFont
        font_path = Path(args.font_path).expanduser().resolve() if args.font_path else Path(choose_font_path())
        vi_font_path = Path(args.vi_font_path).expanduser().resolve() if args.vi_font_path else VI_FONT_PATH
        for font_file in (font_path, vi_font_path):
            ImageFont.truetype(str(font_file), 60)
        ffmpeg = find_ffmpeg()
        intro = Path(args.intro_audio).expanduser().resolve() if args.intro_audio else None
        if intro:
            wav_duration(intro)
        if args.dry_run:
            extras.describe_cta(root, extras_config, ('closing',))
            print('READY: silent overview (2s) -> Part 1 (1.5s) -> Parts 2/3 (2.5s each) -> outro.')
            print('No API calls, no video rendered.')
            return
        base = root/'_emotion_preview/cta_sfx'
        work, final = base/'work', base/'final'
        work.mkdir(parents=True, exist_ok=True)
        final.mkdir(exist_ok=True)
        destinations = render_formats([], 0, None, final, work, font_path, ffmpeg=ffmpeg,
                                    quality=args.resolution, video_format=args.video_format, fps=args.fps,
                                    vi_font_path=vi_font_path, intro_audio=intro,
                                    extras_config=extras_config, extras_preview=True)
        for destination in destinations:
            print('PREVIEW ONLY:', destination)
        return
    if args.demo and (args.script or args.parts_dir or args.script_vi or args.reuse_audio or args.reuse_tts_dir or args.dry_run):
        p.error('--demo cannot be combined with script/audio inputs or --dry-run')
    if not args.script and not args.parts_dir and not args.demo:
        scripts = sorted((root/'kich_ban_tieng_anh').glob('*.txt'))
        if len(scripts) != 1:
            p.error('Put exactly one English .txt in kich_ban_tieng_anh, or choose a file with --script.')
        args.script = str(scripts[0])
    if args.script and args.reuse_tts_dir:
        p.error('--reuse-tts-dir requires --parts-dir; for one script use --reuse-audio instead')
    if args.script:
        script = Path(args.script).expanduser().resolve()
        if script.suffix.lower() != '.txt':
            p.error('Script must be UTF-8 .txt. Export Word/PDF to plain text first.')
        paths = [script]
    elif args.parts_dir:
        parts_dir = (root/args.parts_dir).expanduser().resolve()
        paths = sorted(parts_dir.glob('part*.txt'), key=part_sort)
    else:
        paths = []
    if not paths and not args.demo:
        p.error('No script found. Use --script your_script.txt or --parts-dir your_folder')
    episode = args.episode or (re.sub('[^A-Za-z0-9_-]', '_', paths[0].stem).strip('_-') if args.script else 'EP02') or 'EP_NEW'
    if not args.episode and args.level != 'b1-b2':
        episode += '_A1_A2'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', episode):
        p.error('--episode must contain only letters, digits, underscores and hyphens, starting with a letter or digit')
    if not args.demo:
        cues, script_words = parse_parts(paths)
        print(f'Script OK: {len(paths)} file(s), {len(cues)} captions, {len(script_words)} words.', flush=True)
        if args.script_vi:
            script_vi = Path(args.script_vi).expanduser().resolve()
        elif args.script:
            script_vi = root/'kich_ban_tieng_viet'/paths[0].name
        else:
            p.error('--parts-dir requires --script-vi with the full Vietnamese dialogue.')
        if script_vi.suffix.lower() != '.txt':
            p.error('--script-vi must be a UTF-8 .txt file.')
        bilingual_cues = read_bilingual_cues(paths, script_vi)
        print(f'EN/VI scripts OK: {len(bilingual_cues)} matching turns. No translation API needed.', flush=True)
    try:
        from PIL import Image, ImageFont
    except ImportError:
        raise RuntimeError('Missing Pillow. Run: python3 -m pip install -r requirements.txt') from None
    font_path = Path(args.font_path).expanduser().resolve() if args.font_path else Path(choose_font_path())
    font = ImageFont.truetype(str(font_path), 54)
    if ',' in font.getname()[0]:
        p.error('Font family must not contain a comma (ASS format restriction)')
    vi_font_path = Path(args.vi_font_path).expanduser().resolve() if args.vi_font_path else VI_FONT_PATH
    vi_font = ImageFont.truetype(str(vi_font_path), VI_FONT_SIZE)
    if ',' in vi_font.getname()[0]:
        p.error('Vietnamese font family must not contain a comma (ASS format restriction)')
    caption_font_info(font_path)
    caption_font_info(vi_font_path)
    logo = Path(args.logo).expanduser().resolve() if args.logo else None
    if logo:
        with Image.open(logo) as image:
            image.verify()
    ffmpeg = find_ffmpeg()
    intro_audio = (root/args.intro_audio).expanduser().resolve() if args.intro_audio else None
    if not args.demo and intro_audio:
        wav_duration(intro_audio)  # Check the intro before any paid speech generation.
    resolutions = output_resolutions(args.resolution, args.video_format)
    print('Khung xuất: ' + ', '.join(f'{w}×{h}' for w, h in resolutions), flush=True)
    batch_options = (gemini_part_options(args.tts_model, args.mia_voice, args.alex_voice, level=args.level)
                     if not eleven_config and args.generator == 'generate.py' and args.script else {})
    if args.dry_run:
        if importlib.util.find_spec('faster_whisper') is None:
            raise RuntimeError('Missing faster-whisper. Run: python3 -m pip install -r requirements.txt')
        if importlib.util.find_spec('certifi') is None:
            raise RuntimeError('Missing certifi. Run: python3 -m pip install -r requirements.txt')
        if eleven_config:
            groups = eleven.split_dialogue(paths)
            characters = sum(len(t['text']) for group in groups for t in group)
            print(f'ElevenLabs plan: {len(groups)} joint dialogue requests, {characters} text characters.')
            missing = eleven.missing_configuration(eleven_config)
            if missing and not args.reuse_audio:
                print('SCRIPTS OK; fill ' + ', '.join(missing) + ' in generate_elevenlabs.py before live generation.')
                print('No API calls, no video rendered.')
                return
        elif batch_options:
            import generate
            groups = plan_parts(paths, **batch_options)
            counts = [sum(len(t['text'].split()) for t in group) for group in groups]
            estimates = [generate.estimate_input_tokens(generate.build_payload(
                group, args.tts_model, args.mia_voice, args.alex_voice, level=args.level)) for group in groups]
            print(f'Gemini plan for NEW audio: {len(groups)} joint dialogue request(s); words per request: {counts}.')
            print(f'Maximum {generate.max_english_words(args.level)} spoken words/request; estimated input tokens: {estimates}.')
            print('Input estimates include speech styles and are not exact Google token counts.')
        extras.describe_cta(root, extras_config, ('closing',))
        print('READY: dependencies and script validated. No API calls, no video rendered.')
        return
    if args.demo:
        base = root/'_demo_complete'
        work = base/'work'; work.mkdir(parents=True, exist_ok=True)
        final = base/'final'; final.mkdir(exist_ok=True)
        demo = [Cue(1,'Mia','Alex, I need you to promise me something.',0,7,1.9,4.2,'Alex, hứa với mình một điều nhé.'),
                Cue(2,'Alex','That is a dangerous way to start a conversation.',8,16,4.3,7.3,'Cách mở đầu cuộc trò chuyện này đáng lo đấy.'),
                Cue(3,'Mia','I met someone on a dating app, and he seems really nice.',17,29,7.4,11.3,'Mình vừa quen một người qua ứng dụng hẹn hò. Anh ấy có vẻ tốt.'),
                Cue(4,'Alex','Is this a date or a university project?',30,38,11.4,14.1,'Đây là buổi hẹn hò hay bài nghiên cứu đại học vậy?')]
        audio = final/'00_AUDIO.wav'
        run([ffmpeg,'-y','-hide_banner','-loglevel','error','-f','lavfi','-i',
             'aevalsrc=0.08*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*3*t)):s=24000',
             '-t','15','-c:a','pcm_s16le',audio])
        save_srt(demo, work/'english.srt','en')
        save_srt(demo, work/'bilingual.srt','both')
        destinations = render_formats(demo, 15.0, audio, final, work, font_path, ffmpeg=ffmpeg,
                                    title='DEMO — synthetic tones, no real voices', logo=logo,
                                    quality=args.resolution, video_format=args.video_format,
                                    fps=args.fps, vi_font_path=vi_font_path)
        for destination in destinations:
            print('\nDEMO ONLY (synthetic tones and timings):', destination)
        return
    base = (root / args.out_root / episode).resolve()
    work = base / 'work'; work.mkdir(parents=True, exist_ok=True)
    final = base / 'final'; final.mkdir(exist_ok=True)
    generator = (root / args.generator).resolve()
    existing_audio = Path(args.reuse_audio).expanduser().resolve() if args.reuse_audio else None
    reuse_tts = Path(args.reuse_tts_dir).expanduser().resolve() if args.reuse_tts_dir else None
    settings = {'version': 2, 'sources': [{'name': path.name, 'sha256': file_hash(path)} for path in paths],
                'lead': args.lead, 'tts_model': args.tts_model, 'mia_voice': args.mia_voice,
                'alex_voice': args.alex_voice, 'whisper_model': args.whisper_model,
                'script_vi': file_hash(script_vi),
                'reuse_audio': file_hash(existing_audio) if existing_audio else None,
                'reuse_tts': {path.stem: file_hash(reuse_tts/(path.stem+'.wav')) for path in paths} if reuse_tts else None}
    if args.level != 'b1-b2':
        settings['english_level'] = args.level
    if eleven_config:
        settings['elevenlabs'] = {'engine': eleven.ENGINE, **eleven_config}
    elif args.generator == 'generate.py' and not existing_audio and not reuse_tts:
        # Keep old audio intact and prevent mixing it with the new dialogue delivery.
        settings['gemini_delivery'] = GEMINI_EN_DELIVERY
        complete = next((p for p in (final/'00_AUDIO.wav', work/'joined_unpadded.wav') if p.is_file()), None)
        marker = work/'inputs.json'
        if complete is not None and marker.is_file():
            # Rendering a complete old recording needs no new TTS. Keep its
            # actual delivery marker; other script/voice settings still match.
            wav_duration(complete)
            previous = json.loads(marker.read_text(encoding='utf-8'))
            if previous.get('gemini_delivery'):
                settings['gemini_delivery'] = previous['gemini_delivery']
    check_run_inputs(work, settings)
    if importlib.util.find_spec('faster_whisper') is None:
        raise RuntimeError('Missing faster-whisper. Run: python3 -m pip install -r requirements.txt')
    audio = final / '00_AUDIO.wav'
    if eleven_config and not existing_audio:
        if audio.is_file():
            # Completed episodes do not need new credits or a key to render again.
            wav_duration(audio)
        else:
            existing_audio = eleven.generate_audio(paths, work/'elevenlabs', eleven_config)
    if existing_audio:
        if not audio.exists():
            temporary = audio.with_suffix('.partial.wav')
            run([ffmpeg,'-y','-hide_banner','-loglevel','error','-i',existing_audio,
                 '-af',f'adelay={int(args.lead*1000)}', '-ac','1','-ar','24000','-c:a','pcm_s16le',temporary])
            temporary.replace(audio)
        duration = wav_duration(audio)
    elif eleven_config:
        duration = wav_duration(audio)
    else:
        complete_cached = audio.is_file() or (work/'joined_unpadded.wav').is_file()
        tts_paths = ([] if complete_cached else prepare_parts(paths, work, **batch_options)) if args.script else paths
        duration = ensure_audio(root, tts_paths, work, audio, generator, args.lead,
                                model=args.tts_model, mia_voice=args.mia_voice, alex_voice=args.alex_voice,
                                existing_tts_dir=reuse_tts, ffmpeg=ffmpeg, level=args.level)

    recognized = transcribe_words(audio, args.whisper_model, work/'asr_words.json')
    confidence = align_cues(cues, script_words, recognized, duration, args.lead)
    # Whole-turn subtitles share the exact start/end of their English cue group.
    starts = {cue.first: cue.start for cue in cues}
    ends = {cue.last: cue.end for cue in cues}
    for cue in bilingual_cues:
        cue.start, cue.end = starts[cue.first], ends[cue.last]
    save_srt(cues, work/'english.srt','en')
    save_srt(bilingual_cues, work/'vietnamese.srt','vi')
    save_srt(bilingual_cues, work/'bilingual.srt','both')
    save_json(work/'timings.json', [c.__dict__ for c in bilingual_cues])
    destinations = render_formats(cues, duration, audio, final, work, font_path, ffmpeg=ffmpeg,
                                title=args.title or episode, logo=logo, quality=args.resolution,
                                video_format=args.video_format, fps=args.fps,
                                bilingual_cues=bilingual_cues, intro_audio=intro_audio, vi_font_path=vi_font_path,
                                extras_config=extras_config, dialogue_lead=args.lead)
    (work/'QA_README.txt').write_text(f'Alignment matched {confidence:.1%} of words.\nListen through and visually check subtitle timings before publishing.\n')
    for destination in destinations:
        print('\nSUCCESS:', destination)
    print('One complete video: silent overview (2s) -> Part 1 -> Part 2 -> Part 3 -> spoken outro.')
    print('Editable SRT and timing files:', work)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, ImportError, wave.Error, subprocess.CalledProcessError) as err:
        print('ERROR:', err, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nStopped. Completed audio and subtitle caches are preserved; re-run the same command.', file=sys.stderr)
        sys.exit(130)
