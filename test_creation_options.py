import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageFont

from dialogue_script import read_turns
import generate
import one_click as video
import video_extras as extras


class CreationOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.font = Path(video.choose_font_path())

    def test_terminal_only_asks_for_level_and_defaults_to_landscape(self):
        args = SimpleNamespace(interactive=True, dry_run=False, demo=False, preview_extras=False,
                               level=None, video_format=None)
        with patch('builtins.input', side_effect=['wrong', '1']) as prompt, patch('sys.stdout', new_callable=io.StringIO):
            video.choose_creation_options(args)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual((args.level, args.video_format), ('a1-a2', 'landscape'))
        with patch('builtins.input') as prompt:
            video.choose_creation_options(args)
            prompt.assert_not_called()

    def test_dry_run_never_waits_for_input(self):
        args = SimpleNamespace(interactive=True, dry_run=True, demo=False, preview_extras=False,
                               level=None, video_format=None)
        with patch('builtins.input') as prompt:
            video.choose_creation_options(args)
            prompt.assert_not_called()
        self.assertEqual((args.level, args.video_format), ('b1-b2', 'landscape'))

    def test_beginner_changes_delivery_and_batch_budget_without_changing_words(self):
        script = self.root/'en.txt'
        script.write_text('\n'.join(f'{"Mia" if i % 2 == 0 else "Alex"}: ' +
                                    ' '.join(['hello'] * 20) for i in range(55)))
        options = video.gemini_part_options('gemini-3.8-flash-tts', 'Kore', 'Puck', level='a1-a2')
        self.assertLess(options['max_words'], generate.MAX_ENGLISH_WORDS)
        groups = video.plan_parts([script], **options)
        self.assertGreater(len(groups), 1)
        self.assertEqual([t for group in groups for t in group], read_turns([script]))
        for group in groups:
            payload = generate.build_payload(group, 'gemini-3.8-flash-tts', 'Kore', 'Puck', level='a1-a2')
            self.assertLessEqual(generate.estimate_input_tokens(payload), generate.INPUT_TOKEN_BUDGET)
            self.assertLessEqual(sum(len(t['text'].split()) for t in group), options['max_words'])
            self.assertEqual([t['text'] for t in payload['input'][0]['content']], [t['text'] for t in group])
            self.assertTrue(all('A1-A2' in t['annotations'][0]['style'] for t in payload['input'][0]['content']))

    def test_default_payload_keeps_the_previous_b1_delivery(self):
        for level in ('a1-a2', 'b1-b2'):
            payload = generate.build_payload([{'speaker': 'Alex', 'text': 'Hello.', 'emotion': 'curious'}],
                                             'gemini-3.8-flash-tts', 'Kore', 'Puck', level=level)
            style = payload['input'][0]['content'][0]['annotations'][0]['style']
            self.assertIn(level.upper(), style)
            self.assertIn('American', style)
        self.assertEqual(generate.max_english_words(), generate.MAX_ENGLISH_WORDS)

    def test_old_episode_can_reuse_b1_but_changing_level_keeps_marker_untouched(self):
        original = {'version': 2, 'sources': [{'name': 'en.txt', 'sha256': 'same'}]}
        video.check_run_inputs(self.root, original)
        video.check_run_inputs(self.root, {**original, 'english_level': 'b1-b2'})
        marker = self.root/'inputs.json'
        before = marker.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'NEW --episode'):
            video.check_run_inputs(self.root, {**original, 'english_level': 'a1-a2'})
        self.assertEqual(marker.read_bytes(), before)

    def test_beginner_level_reaches_the_tts_subprocess(self):
        part = self.root/'part001.txt'
        part.write_text('Mia: Hello.')
        commands = []
        def stop_before_api(command, **kwargs):
            commands.append(command)
            raise RuntimeError('stop before API')
        with patch.object(video, 'run', side_effect=stop_before_api):
            with self.assertRaisesRegex(RuntimeError, 'stop before API'):
                video.ensure_audio(self.root, [part], self.root, self.root/'audio.wav',
                                   Path(generate.__file__), 2.6, model='gemini-3.8-flash-tts',
                                   mia_voice='Kore', alex_voice='Puck', level='a1-a2')
        self.assertEqual(commands[0][-2:], ['--level', 'a1-a2'])

    def test_both_formats_render_with_identical_audio_and_captions(self):
        cues = [video.Cue(1, 'Mia', 'Hello.', 0, 0, .2, 2, 'Xin chào.')]
        audio = self.root/'audio.wav'
        with patch.object(video, 'render_videos', side_effect=['wide.mp4', 'tall.mp4']) as render:
            destinations = video.render_formats(cues, 3, audio, self.root, self.root, self.font,
                                                quality='1080p', video_format='both')
        self.assertEqual(destinations, ['wide.mp4', 'tall.mp4'])
        self.assertEqual([c.kwargs['resolution'] for c in render.call_args_list], [(1920, 1080), (1080, 1920)])
        self.assertTrue(all(c.args[0] is cues and c.args[2] is audio for c in render.call_args_list))

    def test_portrait_captions_fit_the_safe_region_and_leave_landscape_independent(self):
        cue = video.Cue(1, 'Mia', 'Could you help me find the nearest supermarket? ' * 4,
                        0, 9, .2, 4, 'Bạn có thể giúp mình tìm siêu thị gần nhất không? ' * 4)
        portrait = self.root/'vertical.ass'
        video.write_ass([cue], portrait, self.font, layout=video.PORTRAIT)
        text = portrait.read_text()
        self.assertIn('PlayResX: 1080\nPlayResY: 1920', text)
        positions = re.findall(r'\\pos\((\d+),([\d.]+)\)\\fs(\d+)\}(.*)', text)
        self.assertTrue(positions)
        for x, y, size, line in positions:
            self.assertEqual(int(x), 540)
            self.assertGreaterEqual(float(y), video.PORTRAIT.bilingual_top)
            self.assertLessEqual(float(y)+int(size), video.PORTRAIT.bilingual_bottom)
            font_path = video.VI_FONT_PATH if re.search(r'[ạảầếểịốớợựửưđ]', line) else self.font
            font = ImageFont.truetype(str(font_path), int(size))
            self.assertLessEqual(font.getlength(line), video.PORTRAIT.width-2*video.PORTRAIT.margin_x)
        landscape = self.root/'horizontal.ass'
        video.write_ass([cue], landscape, self.font)
        self.assertIn('PlayResX: 1920\nPlayResY: 1080', landscape.read_text())

    def test_portrait_cards_wrap_within_the_frame(self):
        for mode in ('intro', 'bilingual_card', 'listening_card', 'outro'):
            path = self.root/f'{mode}.png'
            video.make_background(path, mode, '', self.font, layout=video.PORTRAIT)
            with Image.open(path) as image:
                self.assertEqual(image.size, (1080, 1920))
                bounds = image.getbbox()
                self.assertIsNotNone(bounds)
                self.assertGreaterEqual(bounds[0], video.PORTRAIT.margin_x-5)
                self.assertLessEqual(bounds[2], video.PORTRAIT.width-video.PORTRAIT.margin_x+5)
                self.assertGreater(bounds[1], 220)
                self.assertLess(bounds[3], 1480)

    def scroll_events(self, cues, duration):
        path = self.root/'scroll.ass'
        video.write_scroll_ass(cues, duration, path, self.font)
        return [line for line in path.read_text().splitlines() if line.startswith('Dialogue:')]

    def line_center_at(self, events, text, seconds):
        def as_seconds(value):
            h, m, s = value.split(':')
            return int(h)*3600+int(m)*60+float(s)
        for event in events:
            fields = event.split(',')
            start, end = map(as_seconds, fields[1:3])
            if event.endswith('}'+text) and start <= seconds <= end:
                move = re.search(r'\\move\([^,]+,([^,]+),[^,]+,([^\)]+)\)', event)
                a, b = map(float, move.groups())
                return a+(b-a)*(seconds-start)/(end-start)+video.LANDSCAPE.en_size/2
        self.fail(f'No visible event for {text!r} at {seconds}')

    def test_slow_opening_stays_near_middle_even_when_later_turns_are_faster(self):
        cues = [video.Cue(1, 'Mia', 'Emma asked a dating group to check a man before their first date.',
                          0, 13, .12, 6.11),
                video.Cue(2, 'Alex', 'She asked strangers to check her date? That sounds serious.',
                          14, 24, 6.59, 10.87),
                video.Cue(3, 'Mia', 'She posted his photo, first name, and dating profile.',
                          25, 34, 11.57, 16.87)]
        cues += [video.Cue(i+4, 'Alex' if i % 2 == 0 else 'Mia', f'Really, number {i}?',
                           35+3*i, 37+3*i, 18+2*i, 19.5+2*i) for i in range(20)]
        events = self.scroll_events(cues, 58)
        lines = video.wrap_text(cues[0].en, ImageFont.truetype(str(self.font), 88), 1720)
        self.assertGreater(len(lines), 1)
        ending_y = self.line_center_at(events, lines[-1], cues[0].end)
        self.assertGreater(ending_y, 430)
        self.assertLess(ending_y, 560)
        opening_y = self.line_center_at(events, lines[0], cues[0].start)
        self.assertGreater(opening_y, 550)
        self.assertLess(opening_y, 640)

    def test_motion_is_continuous_monotone_and_changes_speed_gradually(self):
        anchors = [(2, 200), (5, 80), (5.3, 30), (9, -160), (13, -350)]
        points = video.smooth_scroll_points(anchors, 15, 260, -410)
        speeds = [(a[1]-b[1])/(b[0]-a[0]) for a, b in zip(points, points[1:])]
        self.assertTrue(all(speed > 0 for speed in speeds))
        self.assertLess(max(speeds), 100)
        self.assertLess(max(abs(a-b) for a,b in zip(speeds,speeds[1:])), 12)
        self.assertEqual(points[0][0], 0)
        self.assertEqual(points[-1][0], 15)

    def test_long_transcript_scrolls_every_line_past_the_reading_area(self):
        text = ' '.join(f'word{i}' for i in range(90))
        cue = video.Cue(1, 'Mia', text, 0, 89, .2, 12)
        lines = video.wrap_text(text, ImageFont.truetype(str(self.font), video.LANDSCAPE.en_size),
                                video.W-2*video.TEXT_MARGIN_X)
        self.assertGreater(len(lines), 2)
        events = self.scroll_events([cue], 12.5)
        self.assertAlmostEqual(self.line_center_at(events, lines[0], 0), 600, delta=30)
        self.assertAlmostEqual(self.line_center_at(events, lines[-1], 12.5), 500, delta=30)
        visible = {event.split('}', 1)[1] for event in events}
        self.assertTrue(set(lines).issubset(visible))

    def test_real_word_timings_anchor_each_wrapped_line(self):
        text = 'Emma asked a dating group to check a man before their first date.'
        words = text.split()
        cue = video.Cue(1, 'Mia', text, 0, len(words)-1, 0, 10,
                        word_times=[(.3+i*.5, .5+i*.5) for i in range(len(words))])
        font = ImageFont.truetype(str(self.font), 88)
        lines = video.wrap_text(text, font, 1720)
        _, anchors = video.scroll_rows_and_anchors([cue], font, video.LANDSCAPE)
        first_count = len(lines[0].split())
        self.assertAlmostEqual(anchors[0][0], (cue.word_times[0][0]+cue.word_times[first_count-1][1])/2)
        self.assertAlmostEqual(anchors[-1][0], (cue.word_times[first_count][0]+cue.word_times[-1][1])/2)

    def test_alignment_interpolates_words_missing_from_asr(self):
        words = 'Today we will go to the small park near home'.split()
        recognized = [{'word': word, 'start': .4+i*.45, 'end': .65+i*.45}
                      for i, word in enumerate(words) if i not in (0, 5, 9)]
        cues = [video.Cue(1, 'Mia', 'Today we will', 0, 2),
                video.Cue(2, 'Alex', 'go to the small park', 3, 7),
                video.Cue(3, 'Mia', 'near home', 8, 9)]
        score = video.align_cues(cues, words, recognized, 5.5, .2)
        self.assertAlmostEqual(score, .7)
        self.assertTrue(all(0 <= c.start < c.end <= 5.5 for c in cues))
        self.assertTrue(all(a.end <= b.start for a, b in zip(cues, cues[1:])))
        self.assertEqual([c.en for c in cues], ['Today we will', 'go to the small park', 'near home'])
        self.assertEqual([len(c.word_times) for c in cues], [3, 5, 2])

    def test_cta_uses_a_separate_cache_for_beginner_delivery(self):
        config = {'provider': 'gemini', 'model': 'gemini-3.8-flash-tts',
                  'voice': 'Kore', 'delivery': video.GEMINI_EN_DELIVERY}
        old, _ = extras.cta_locations(self.root, config)
        beginner, _ = extras.cta_locations(self.root, {**config, 'level': 'a1-a2'})
        self.assertNotEqual(old, beginner)


if __name__ == '__main__':
    unittest.main()
