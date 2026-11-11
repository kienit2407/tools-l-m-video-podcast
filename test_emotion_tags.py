from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from dialogue_script import EMOTION_STYLES, format_turn, read_turns
import generate
import generate_elevenlabs
import one_click


class EmotionTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'en.txt'
        self.vi = self.root / 'vi.txt'
        self.script.write_text('MIA: [curious] Did she call?\nALEX: [relieved] Yes, she is safe.\n'
                               'MIA: That is good news.\n', encoding='utf-8')
        self.vi.write_text('MIA: Cô ấy gọi chưa?\nALEX: Rồi, cô ấy vẫn an toàn.\n'
                           'MIA: Tin tốt quá.\n', encoding='utf-8')

    def test_twelve_basic_tags_are_removed_from_spoken_text_and_captions(self):
        self.assertEqual(len(EMOTION_STYLES), 12)
        turns = read_turns([self.script])
        self.assertEqual(turns[0], {'speaker': 'Mia', 'text': 'Did she call?', 'emotion': 'curious'})
        self.assertEqual(turns[2], {'speaker': 'Mia', 'text': 'That is good news.'})
        self.assertEqual(format_turn(turns[0]), 'Mia: [curious] Did she call?')
        cues, words = one_click.parse_parts([self.script])
        paired = one_click.read_bilingual_cues([self.script], self.vi)
        self.assertEqual(words, ['Did', 'she', 'call?', 'Yes,', 'she', 'is', 'safe.',
                                 'That', 'is', 'good', 'news.'])
        self.assertEqual([cue.en for cue in paired], [cue.en for cue in cues])
        self.assertTrue(all('[' not in cue.en for cue in cues))

    def test_gemini_uses_style_only_for_tagged_turn_and_keeps_speakers(self):
        payload = generate.build_payload(read_turns([self.script]), 'test', 'Kore', 'Puck')
        content = payload['input'][0]['content']
        self.assertEqual([item['text'] for item in content],
                         ['Did she call?', 'Yes, she is safe.', 'That is good news.'])
        self.assertIn('Curious and interested', content[0]['annotations'][0]['style'])
        self.assertIn('British English accent', content[0]['annotations'][0]['style'])
        self.assertIn('Gently relieved', content[1]['annotations'][0]['style'])
        self.assertIn('American English accent', content[1]['annotations'][0]['style'])
        self.assertIn('Warm, friendly and expressive British', content[2]['annotations'][0]['style'])
        self.assertEqual([item['annotations'][0]['speaker'] for item in content],
                         ['Mia', 'Alex', 'Mia'])

    def test_parts_and_elevenlabs_preserve_tag_without_speaking_it_in_gemini(self):
        parts = one_click.prepare_parts([self.script], self.root)
        self.assertEqual(len(parts), 1)
        self.assertEqual(read_turns(parts), read_turns([self.script]))
        group = generate_elevenlabs.split_dialogue([self.script])[0]
        self.assertEqual([item['text'] for item in group],
                         ['[curious] Did she call?', '[relieved] Yes, she is safe.',
                          'That is good news.'])
        self.assertEqual([item['speaker'] for item in group], ['Mia', 'Alex', 'Mia'])

    def test_invalid_or_multiple_tags_fail_before_audio_generation(self):
        for speech, error in [('[laughs] Hello.', 'unknown emotion tag'),
                              ('[happy] [sad] Hello.', 'only one emotion tag'),
                              ('[happy Hello.', 'invalid emotion tag'),
                              ('Hello [happy].', 'only at the start')]:
            with self.subTest(speech=speech):
                self.script.write_text('MIA: ' + speech, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, error):
                    read_turns([self.script])

    def test_tagged_chinese_turn_uses_mandarin_style_and_clean_text(self):
        self.script.write_text('MIA： [curious] 你听到了吗？', encoding='utf-8')
        turn = read_turns([self.script])[0]
        payload = generate.build_payload([turn], 'test', 'Kore', 'Puck', language='zh')
        item = payload['input'][0]['content'][0]
        self.assertEqual(item['text'], '你听到了吗？')
        self.assertIn('Mandarin Chinese', item['annotations'][0]['style'])

    def test_plain_dialogue_uses_classic_characters_without_tags_or_unsupported_mode(self):
        self.script.write_text('Mia: You remembered?\nAlex: Of course I did.', encoding='utf-8')
        payload = generate.build_payload(read_turns([self.script]), 'test', 'Kore', 'Puck')
        content = payload['input'][0]['content']
        self.assertEqual([item['text'] for item in content], ['You remembered?', 'Of course I did.'])
        self.assertIn('expressive', content[0]['annotations'][0]['style'])
        self.assertIn('relaxed American', content[1]['annotations'][0]['style'])
        self.assertIn('Witty but not exaggerated', content[1]['annotations'][0]['style'])
        self.assertTrue(all('130-145' in item['annotations'][0]['style'] for item in content))
        self.assertEqual(payload['generation_config']['speech_config'], {
            'speakers': [{'speaker': 'Mia', 'voice': 'Kore'}, {'speaker': 'Alex', 'voice': 'Puck'}]})

    def test_preview_no_retry_and_daily_limit_stop_after_one_attempt(self):
        cases = [(503, 'Unavailable', 1), (429, '10 requests per day on Free Tier', 4)]
        for status, message, attempts in cases:
            with self.subTest(status=status):
                body = io.BytesIO(json.dumps({'error': {'message': message}}).encode())
                failure = HTTPError(generate.API_URL, status, message, {}, body)
                with patch.object(generate.request, 'urlopen', side_effect=failure) as api, \
                     patch.object(generate.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(RuntimeError, f'HTTP {status}'):
                        generate.call_api({'input': []}, 'not-a-real-key', max_attempts=attempts)
                    api.assert_called_once()
                    sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
