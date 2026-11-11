#!/usr/bin/env python3
"""Hanzi Radio: Mandarin HSK 5-6 script + Vietnamese translation -> three-pass MP4."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import wave

import one_click as video

ROOT = Path(__file__).resolve().parent
HANZI_ROOT = ROOT/'hanzi_radio'
CHANNEL = 'Hanzi Radio'
INTRO_TEXT = '大家好，欢迎收听汉字电台，Hanzi Radio！今天我们一起练习中文听力。'

# Optional channel-specific keys. Leave blank to share the existing English tool's key.
GEMINI_API_KEY = ''
ELEVENLABS_API_KEY = ''
GEMINI_MODEL = 'gemini-3.8-flash-tts'
MIA_VOICE = 'Kore'
ALEX_VOICE = 'Puck'

# Optional ElevenLabs voices for Mandarin. The key is shared with generate_elevenlabs.py.
ELEVENLABS_MIA_VOICE_ID = ''
ELEVENLABS_ALEX_VOICE_ID = ''

CARDS = {
    'intro': ('', '欢迎来到 Hanzi Radio！\n一起练习中文听力。', ''),
    'english_card': ('第 1 遍，共 3 遍', '边看边听', 'HSK 5–6 · 阅读中文文本，注意语气和节奏。'),
    'bilingual_card': ('第 2 遍，共 3 遍', '中文与越南语字幕', '结合译文，理解句子的意思和表达方式。'),
    'listening_card': ('第 3 遍，共 3 遍', '只听，不看字幕', '专注听力，试着理解整段对话。'),
    'outro': ('', '每天进步一点。\n下次再见！', ''),
}


def choose_chinese_font():
    bundled = HANZI_ROOT/'assets/NotoSansSC-Bold.ttf'
    if bundled.is_file():
        return bundled
    system = Path('/System/Library/Fonts/STHeiti Medium.ttc')
    if system.is_file():
        # The Mac collection starts with Traditional Chinese. Use its Simplified Chinese face.
        # Keep the prepared working copy in the user's temporary cache, outside the project.
        from fontTools.ttLib import TTFont
        cache = Path(tempfile.gettempdir())/'hanzi_radio_fonts'/str(system.stat().st_mtime_ns)
        destination = cache/'HeitiSC-Medium.ttf'
        if not destination.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            with TTFont(str(system), fontNumber=1) as font:
                font.save(destination.with_suffix('.partial.ttf'))
            destination.with_suffix('.partial.ttf').replace(destination)
        return destination
    for path in [Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]:
        if path.is_file():
            return path
    raise RuntimeError('Chinese font missing. Set --font-path to a Chinese .ttf/.otf/.ttc font.')


def intro_location(config):
    settings = {'channel': CHANNEL, 'text': INTRO_TEXT, **config}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return HANZI_ROOT/'assets/intro'/digest/'intro.wav', settings


def copy_audio(source, destination, lead, ffmpeg):
    if destination.is_file():
        return video.wav_duration(destination)
    temporary = destination.with_suffix('.partial.wav')
    video.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', source,
               '-af', f'adelay={int(lead*1000)}', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', temporary])
    video.wav_duration(temporary)
    temporary.replace(destination)
    return video.wav_duration(destination)


def create_track(paths, work, destination, config, lead, ffmpeg):
    if destination.is_file():
        print('Reuse complete Hanzi audio:', destination.name, flush=True)
        return video.wav_duration(destination)
    if config['provider'] == 'elevenlabs':
        import generate_elevenlabs as eleven
        raw = eleven.generate_audio(paths, work/'elevenlabs',
                                    {'model': config['model'], 'voices': config['voices']})
        return copy_audio(raw, destination, lead, ffmpeg)
    parts = video.prepare_parts(paths, work, language='zh')
    return video.ensure_audio(ROOT, parts, work, destination, ROOT/'generate.py', lead,
                              model=config['model'], mia_voice=config['voices']['Mia'],
                              alex_voice=config['voices']['Alex'], ffmpeg=ffmpeg, language='zh')


def prepare_intro(destination, settings, config, ffmpeg):
    if destination.is_file():
        video.wav_duration(destination)
        return destination
    folder = destination.parent
    folder.mkdir(parents=True, exist_ok=True)
    video.check_run_inputs(folder, settings)
    script = folder/'intro_script.txt'
    script.write_text('Mia: '+INTRO_TEXT+'\n', encoding='utf-8')
    print('Create the Mandarin intro once; later episodes reuse it.', flush=True)
    create_track([script], folder, destination, config, 0, ffmpeg)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', type=Path, default=HANZI_ROOT/'kich_ban_tieng_trung/kich_ban.txt')
    parser.add_argument('--script-vi', type=Path)
    parser.add_argument('--episode', help='Unique episode, e.g. HZ01_GEMINI')
    parser.add_argument('--title', help='Accepted for compatibility; not drawn on the video')
    parser.add_argument('--tts-provider', choices=('gemini', 'elevenlabs'), default='gemini')
    parser.add_argument('--tts-model')
    parser.add_argument('--mia-voice')
    parser.add_argument('--alex-voice')
    parser.add_argument('--out-root', type=Path, default=HANZI_ROOT/'episodes')
    parser.add_argument('--intro-audio', type=Path, help='Existing Mandarin intro WAV; otherwise create and cache one')
    parser.add_argument('--reuse-audio', type=Path, help='Existing unpadded Mandarin WAV; skip dialogue TTS')
    parser.add_argument('--font-path', type=Path, help='Chinese transcript/card font (.ttf/.otf/.ttc)')
    parser.add_argument('--vi-font-path', type=Path, default=video.VI_FONT_PATH)
    parser.add_argument('--logo', type=Path)
    parser.add_argument('--whisper-model', default='base')
    parser.add_argument('--lead', type=float, default=2.6)
    parser.add_argument('--resolution', choices=('720p', '1080p', '1440p'), default='1080p')
    parser.add_argument('--fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--dry-run', action='store_true', help='Validate locally; never call TTS or download models')
    args = parser.parse_args()
    for name, value in [('GEMINI_API_KEY', GEMINI_API_KEY), ('ELEVENLABS_API_KEY', ELEVENLABS_API_KEY)]:
        if value.strip() and not os.environ.get(name, '').strip():
            os.environ[name] = value.strip()
    if not math.isfinite(args.lead) or args.lead < 0:
        parser.error('--lead must be finite and nonnegative.')
    episode = args.episode or 'HZ01_'+args.tts_provider.upper()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', episode):
        parser.error('--episode must use letters, digits, underscores and hyphens.')
    script = args.script.expanduser().resolve()
    translated = (args.script_vi or HANZI_ROOT/'kich_ban_tieng_viet'/script.name).expanduser().resolve()
    if script.suffix.lower() != '.txt' or translated.suffix.lower() != '.txt':
        parser.error('Use UTF-8 .txt scripts.')
    cues, units = video.parse_parts([script], language='zh')
    if not any(re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', cue.en) for cue in cues):
        parser.error('The Mandarin script must contain Chinese characters.')
    bilingual = video.read_bilingual_cues([script], translated, language='zh')
    print(f'Hanzi Radio: {len(cues)} matching ZH/VI turns; {len(units)} speech units. Target: HSK 5–6.', flush=True)
    if args.tts_provider == 'gemini':
        config = {'provider': 'gemini', 'model': args.tts_model or GEMINI_MODEL,
                  'voices': {'Mia': args.mia_voice or MIA_VOICE, 'Alex': args.alex_voice or ALEX_VOICE},
                  'delivery': 'mandarin-context-v1'}
    else:
        config = {'provider': 'elevenlabs', 'model': args.tts_model or 'eleven_v4',
                  'voices': {
                      'Mia': args.mia_voice or os.environ.get('HANZI_ELEVENLABS_MIA_VOICE_ID', '').strip() or ELEVENLABS_MIA_VOICE_ID,
                      'Alex': args.alex_voice or os.environ.get('HANZI_ELEVENLABS_ALEX_VOICE_ID', '').strip() or ELEVENLABS_ALEX_VOICE_ID,
                  }, 'delivery': 'elevenlabs-mandarin-v1'}
    for dependency in ('PIL', 'fontTools', 'faster_whisper', 'certifi'):
        if importlib.util.find_spec(dependency) is None:
            raise RuntimeError('Missing dependencies. Run: python3 -m pip install -r requirements.txt')
    from PIL import Image, ImageFont
    primary_font = (args.font_path or choose_chinese_font()).expanduser().resolve()
    vi_font = args.vi_font_path.expanduser().resolve()
    for path in (primary_font, vi_font):
        ImageFont.truetype(str(path), 60)
        video.caption_font_info(path)
    from fontTools.ttLib import TTFont
    with TTFont(str(primary_font), fontNumber=0) as font:
        required = ''.join(c.en for c in cues) + ''.join(''.join(row) for row in CARDS.values())
        missing = sorted({c for c in required if not c.isspace() and ord(c) not in font.getBestCmap()})
    if missing:
        raise RuntimeError('Chinese font lacks characters: '+''.join(missing[:25])+'. Choose another --font-path.')
    logo = args.logo.expanduser().resolve() if args.logo else None
    if logo:
        with Image.open(logo) as image:
            image.verify()
    ffmpeg = video.find_ffmpeg()
    cached_intro, intro_settings = intro_location(config)
    intro = args.intro_audio.expanduser().resolve() if args.intro_audio else cached_intro
    if args.intro_audio or intro.exists():
        video.wav_duration(intro)
    raw_audio = args.reuse_audio.expanduser().resolve() if args.reuse_audio else None
    if raw_audio:
        video.wav_duration(raw_audio)
    if args.dry_run:
        from dialogue_script import read_turns
        characters = sum(len(t['text']) for t in read_turns([script]))
        print(f'Chinese font: {primary_font.name}; recognizer language: zh; dialogue: {characters} characters.')
        if not args.intro_audio and not cached_intro.exists() and not raw_audio:
            print('First live run also creates one reusable Mandarin intro (one short TTS request).')
        if config['provider'] == 'elevenlabs' and not all(config['voices'].values()) and not raw_audio:
            print('CONFIG REQUIRED: set two Mandarin ElevenLabs Voice IDs in one_click_hanzi.py or CLI.')
        print('READY: local validation finished. No API calls, model downloads or video rendering.')
        return
    base = args.out_root.expanduser().resolve()/episode
    work, final = base/'work', base/'final'
    work.mkdir(parents=True, exist_ok=True)
    final.mkdir(exist_ok=True)
    settings = {'version': 1, 'channel': CHANNEL, 'language': 'zh',
                'source': video.file_hash(script), 'script_vi': video.file_hash(translated),
                'tts': config, 'lead': args.lead, 'whisper_model': args.whisper_model,
                'reuse_audio': video.file_hash(raw_audio) if raw_audio else None}
    video.check_run_inputs(work, settings)
    if not intro.exists() and not args.intro_audio:
        if raw_audio:
            print('No cached Mandarin intro; showing the intro card silently (no TTS requested).')
            intro = None
        else:
            intro = prepare_intro(cached_intro, intro_settings, config, ffmpeg)
    audio = final/'00_AUDIO.wav'
    if raw_audio:
        duration = copy_audio(raw_audio, audio, args.lead, ffmpeg)
    else:
        duration = create_track([script], work, audio, config, args.lead, ffmpeg)
    recognized = video.transcribe_words(audio, args.whisper_model, work/'asr_words.json', language='zh')
    score = video.align_cues(cues, units, recognized, duration, args.lead, language='zh')
    for cue, paired in zip(cues, bilingual):
        paired.start, paired.end = cue.start, cue.end
    video.save_srt(cues, work/'chinese.srt', 'en')
    video.save_srt(bilingual, work/'vietnamese.srt', 'vi')
    video.save_srt(bilingual, work/'bilingual.srt', 'both')
    video.save_json(work/'timings.json', [c.__dict__ for c in bilingual])
    destination = video.render_videos(cues, duration, audio, final, work, primary_font, ffmpeg=ffmpeg,
                                      title=args.title or episode, logo=logo, vi_font_path=vi_font,
                                      resolution={'720p':(1280,720),'1080p':(1920,1080),'1440p':(2560,1440)}[args.resolution],
                                      fps=args.fps, bilingual_cues=bilingual, intro_audio=intro,
                                      channel_name=CHANNEL, cards=CARDS, language='zh')
    (work/'QA_README.txt').write_text(f'Chinese character/word alignment: {score:.1%}.\n'
                                    'Listen and check Chinese/Vietnamese captions before publishing.\n')
    print('SUCCESS:', destination)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, ImportError, wave.Error, subprocess.CalledProcessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Stopped. Completed Hanzi audio and captions are preserved; rerun the same command.', file=sys.stderr)
        sys.exit(130)
