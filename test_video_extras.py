import io
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import generate
import one_click as video
import video_extras as extras


CONFIG = {'provider': 'gemini', 'model': 'gemini-3.8-flash-tts',
          'voice': 'Kore', 'delivery': video.GEMINI_EN_DELIVERY}


def wav_bytes(seconds=1):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x64\x00' * round(seconds * 24000))
    return stream.getvalue()


class VideoExtrasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        key = patch.object(generate, 'GEMINI_API_KEY', 'test-key')
        key.start()
        self.addCleanup(key.stop)

    def test_cta_resume_never_regenerates_completed_voice_after_failure(self):
        with patch.object(generate, 'call_api', side_effect=[wav_bytes(), RuntimeError('quota')]) as api:
            with self.assertRaisesRegex(RuntimeError, 'quota'):
                extras.prepare_cta(self.root, CONFIG)
            self.assertEqual(api.call_count, 2)
            self.assertTrue(all(c.kwargs['max_attempts'] == 1 for c in api.call_args_list))
        paths, _ = extras.cta_locations(self.root, CONFIG)
        original = paths['opening'].read_bytes()
        with patch.object(generate, 'call_api', return_value=wav_bytes()) as api:
            extras.prepare_cta(self.root, CONFIG)
            api.assert_called_once()
        with patch.object(generate, 'call_api') as api:
            extras.prepare_cta(self.root, CONFIG)
            api.assert_not_called()
        self.assertEqual(paths['opening'].read_bytes(), original)
        self.assertNotIn('key', (paths['opening'].parent/'inputs.json').read_text().lower())

    def test_cta_cache_changes_with_voice_and_provider(self):
        first, _ = extras.cta_locations(self.root, CONFIG)
        for change in ({'voice': 'another'}, {'model': 'another'}, {'provider': 'elevenlabs'}):
            changed, _ = extras.cta_locations(self.root, {**CONFIG, **change})
            self.assertNotEqual(changed, first)

    def test_overview_voice_is_cached_separately_from_existing_greeting_and_outro(self):
        with patch.object(generate, 'call_api', return_value=wav_bytes()):
            original = extras.prepare_cta(self.root, CONFIG)
        saved = {name: path.read_bytes() for name, path in original.items()}
        texts = {'overview': extras.OVERVIEW_TEXT}
        with patch.object(generate, 'call_api', return_value=wav_bytes()) as api:
            overview = extras.prepare_cta(self.root, CONFIG, ('overview',), texts=texts)
            api.assert_called_once()
            spoken = api.call_args.args[0]['input'][0]['content'][0]['text']
            self.assertEqual(spoken, extras.OVERVIEW_TEXT)
        self.assertNotEqual(overview['overview'].parent, original['opening'].parent)
        with patch.object(generate, 'call_api') as api:
            extras.prepare_cta(self.root, CONFIG)
            extras.prepare_cta(self.root, CONFIG, ('overview',), texts=texts)
            api.assert_not_called()
        self.assertEqual({name: path.read_bytes() for name, path in original.items()}, saved)
        inputs = json.loads((overview['overview'].parent/'inputs.json').read_text())
        self.assertEqual(inputs['text'], texts)

    def test_dry_description_creates_no_assets_or_requests(self):
        with patch.object(generate, 'call_api') as api:
            extras.describe_cta(self.root, CONFIG)
            api.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_truncated_cta_is_rejected_before_request(self):
        paths, _ = extras.cta_locations(self.root, CONFIG)
        path = paths['opening']
        path.parent.mkdir(parents=True)
        path.write_bytes(wav_bytes()[:-30])
        with patch.object(generate, 'call_api') as api:
            with self.assertRaisesRegex(ValueError, 'truncated'):
                extras.prepare_cta(self.root, CONFIG)
            api.assert_not_called()

    def test_sfx_match_card_duration_and_end_before_narration(self):
        for seconds in (extras.FIRST_PART_DURATION, extras.PART_DURATION):
            track = extras.prepare_part_sfx(self.root, seconds)
            self.assertAlmostEqual(extras.wav_duration(track), seconds)
            with wave.open(str(track), 'rb') as audio:
                audio.setpos(round((seconds-extras.EXIT_GAP)*audio.getframerate()))
                tail = audio.readframes(audio.getnframes())
                self.assertTrue(tail and not any(tail))
                audio.setpos(round(.04*audio.getframerate()))
                self.assertTrue(any(audio.readframes(2000)))
                audio.setpos(round((seconds-.3)*audio.getframerate()))
                self.assertTrue(any(audio.readframes(2000)))

    def test_replacing_custom_ding_rebuilds_both_tracks_but_unchanged_file_reuses_them(self):
        source = self.root/'assets/sfx/ding.mp3'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'first')
        durations = (extras.FIRST_PART_DURATION, extras.PART_DURATION)
        with patch.object(extras, 'read_custom_ding', return_value=[.04]*4800):
            tracks = [extras.prepare_part_sfx(self.root, d) for d in durations]
        before = [path.read_bytes() for path in tracks]
        with patch.object(extras, 'read_custom_ding') as decode:
            for duration in durations:
                extras.prepare_part_sfx(self.root, duration)
            decode.assert_not_called()
        # Same name and same size, different content: both cached mixes must change.
        source.write_bytes(b'later')
        with patch.object(extras, 'read_custom_ding', return_value=[.08]*4800) as decode:
            for duration in durations:
                extras.prepare_part_sfx(self.root, duration)
            self.assertEqual(decode.call_count, 2)
        self.assertTrue(all(path.read_bytes() != old for path, old in zip(tracks, before)))
        for track in tracks:
            settings = json.loads(track.with_suffix('.json').read_text())
            self.assertEqual(settings['ding_source'], 'ding.mp3')
            self.assertEqual(settings['ding_sha256'], hashlib.sha256(b'later').hexdigest())
        self.assertEqual(source.read_bytes(), b'later')

    def test_invalid_custom_ding_keeps_previous_mix_and_reports_failure(self):
        track = extras.prepare_part_sfx(self.root)
        original = track.read_bytes()
        (self.root/'assets/sfx/ding.mp3').write_bytes(b'invalid')
        with patch.object(extras, 'read_custom_ding', side_effect=ValueError('Cannot decode')):
            with self.assertRaisesRegex(ValueError, 'Cannot decode'):
                extras.prepare_part_sfx(self.root)
        self.assertEqual(track.read_bytes(), original)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required to decode MP3')
    def test_actual_mp3_trims_leading_silence_limits_length_and_preserves_source(self):
        source = self.root/'bell.mp3'
        wav = self.root/'bell.wav'
        extras.write_pcm(wav, [0 if i < 9600 else .3*math.sin(2*math.pi*440*i/48000)
                               for i in range(96000)])
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(wav), str(source)], check=True)
        original = source.read_bytes()
        samples = extras.read_custom_ding(source, 'ffmpeg')
        self.assertGreater(len(samples), 48000)
        self.assertLessEqual(len(samples), round(1.35*48000))
        self.assertLessEqual(max(abs(s) for s in samples), .120001)
        self.assertGreater(max(abs(s) for s in samples[:2400]), .05)
        self.assertAlmostEqual(samples[-1], 0)
        self.assertEqual(source.read_bytes(), original)

    def test_render_starts_with_silent_two_second_overview_and_preserves_dialogue_timings(self):
        work, final = self.root/'work', self.root/'final'
        work.mkdir(); final.mkdir()
        paths, _ = extras.cta_locations(self.root, CONFIG)
        overview, _ = extras.cta_locations(self.root, CONFIG, texts={'overview': extras.OVERVIEW_TEXT})
        paths.update(overview)
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav_bytes())
        cue = video.Cue(1, 'Mia', 'Hello there.', 0, 1, 2.7, 3.7, 'Xin chào.',
                        word_times=[(2.7, 3.1), (3.2, 3.7)])
        calls = []
        def render(*args, **kwargs):
            calls.append((args, kwargs))
            args[4].touch()
        def run(command, **kwargs):
            Path(command[-1]).touch()
        font = Path(video.choose_font_path())
        with patch.object(video, '__file__', str(self.root/'one_click.py')), \
             patch.object(extras, 'prepare_cta', return_value={'closing': paths['closing']}) as cta, \
             patch.object(video, 'render_section', side_effect=render), \
             patch.object(video, 'run', side_effect=run), \
             patch.object(video, 'write_ass') as ass, \
             patch.object(video, 'write_scroll_ass') as scroll:
            video.render_videos([cue], 5, self.root/'speech.wav', final, work, font,
                                bilingual_cues=[cue], extras_config=CONFIG, dialogue_lead=2.6)
        timeline = json.loads((final/'timeline.json').read_text())
        cta.assert_called_once_with(self.root.resolve(), CONFIG, ('closing',))
        self.assertEqual([s['section'] for s in timeline],
                         ['overview', 'english_card', 'scroll', 'bilingual_card', 'bilingual',
                          'listening_card', 'listening', 'outro'])
        self.assertEqual(timeline[0]['start'], 0.)
        self.assertEqual(timeline[0]['end'], 2.)
        self.assertEqual(timeline[1]['start'], timeline[0]['end'])
        self.assertIsNone(calls[0][0][2])
        self.assertFalse(calls[0][1]['card_motion'])
        self.assertAlmostEqual(timeline[1]['end']-timeline[1]['start'], 1.5)
        for index in (1, 3, 5):
            self.assertTrue(calls[index][1]['card_motion'])
            self.assertIn('part_card_', calls[index][0][2].name)
        for index in (2, 4, 6):
            self.assertAlmostEqual(calls[index][1]['audio_seek'], 2.4333333333)
        for mocked in (ass, scroll):
            self.assertAlmostEqual(mocked.call_args.args[0][0].start, 2.7-2.4333333333)
        self.assertEqual(cue.start, 2.7)
        shifted = scroll.call_args.args[0][0].word_times
        self.assertAlmostEqual(shifted[0][0], 2.7-2.4333333333)
        self.assertAlmostEqual(shifted[-1][1], 3.7-2.4333333333)
        self.assertEqual(cue.word_times, [(2.7, 3.1), (3.2, 3.7)])
        chapters = (final/'YOUTUBE_CHAPTERS.txt').read_text()
        self.assertIn('00:00', chapters)


if __name__ == '__main__':
    unittest.main()
