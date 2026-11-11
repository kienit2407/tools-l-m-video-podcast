from pathlib import Path
import tempfile
import unittest

from dialogue_script import read_turns
import generate
import one_click as video


class GeminiBatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root/'en.txt'
        self.options = video.gemini_part_options('gemini-3.8-flash-tts', 'Kore', 'Puck')

    def write_story(self, turns, words_per_turn):
        lines = []
        for i in range(turns):
            speaker = 'Mia' if i % 2 == 0 else 'Alex'
            words = [f'story{i}'] + ['ordinary']*(words_per_turn-2) + ['conversation.']
            tag = '[curious] ' if i == 0 else ''
            lines.append(f'{speaker}: {tag}{" ".join(words)}')
        self.script.write_text('\n'.join(lines), encoding='utf-8')

    def assert_preserved_and_bounded(self, groups):
        before = [(t['speaker'], t.get('emotion'), word)
                  for t in read_turns([self.script]) for word in t['text'].split()]
        after = [(t['speaker'], t.get('emotion'), word)
                 for group in groups for t in group for word in t['text'].split()]
        self.assertEqual(after, before)
        for group in groups:
            self.assertLessEqual(sum(len(t['text'].split()) for t in group), generate.MAX_ENGLISH_WORDS)
            payload = generate.build_payload(group, 'gemini-3.8-flash-tts', 'Kore', 'Puck')
            self.assertLessEqual(generate.estimate_input_tokens(payload), generate.INPUT_TOKEN_BUDGET)
            self.assertEqual(payload['generation_config']['max_output_tokens'], 16384)

    def test_complete_thousand_word_dialogue_goes_in_one_request(self):
        self.write_story(50, 20)
        groups = video.plan_parts([self.script], **self.options)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 50)
        self.assert_preserved_and_bounded(groups)

    def test_long_dialogue_preserves_turns_and_avoids_a_tiny_final_part(self):
        self.write_story(70, 20)
        groups = video.plan_parts([self.script], **self.options)
        self.assertEqual(len(groups), 2)
        counts = [sum(len(t['text'].split()) for t in group) for group in groups]
        self.assertGreaterEqual(counts[-1], counts[-2]/2)
        self.assertEqual(sum(map(len, groups)), 70)
        self.assert_preserved_and_bounded(groups)
        parts = video.prepare_parts([self.script], self.root, **self.options)
        self.assertEqual(read_turns(parts), read_turns([self.script]))

    def test_repeated_style_cost_can_limit_many_short_turns_before_word_limit(self):
        self.write_story(200, 4)
        groups = video.plan_parts([self.script], **self.options)
        self.assertGreater(len(groups), 1)
        self.assert_preserved_and_bounded(groups)

    def test_unusually_large_word_is_rejected_without_generating_audio(self):
        self.script.write_text('Mia: ' + 'x'*40000, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'input budget'):
            video.plan_parts([self.script], **self.options)

    def test_other_models_keep_legacy_limits_and_payload_schema(self):
        self.assertEqual(video.gemini_part_options('custom-model', 'Kore', 'Puck'), {})
        payload = generate.build_payload([{'speaker': 'Mia', 'text': 'Hello.'}],
                                         'custom-model', 'Kore', 'Puck')
        self.assertNotIn('max_output_tokens', payload['generation_config'])


if __name__ == '__main__':
    unittest.main()
