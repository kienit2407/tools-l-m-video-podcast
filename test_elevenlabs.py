import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import wave

import generate_elevenlabs as eleven
import one_click


CONFIG = {'model': 'eleven_v4', 'voices': {'Mia': 'female-voice-id', 'Alex': 'male-voice-id'}}
PCM = b'\x64\x00' * 2400


def make_wav(path, frames=72000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x64\x00' * frames)


class ElevenLabsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root/'en.txt'
        self.script.write_text('Alex: Hello Mia.\nMia: Hello Alex.\nMia: Welcome back.\nAlex: Thank you.')
        self.output = self.root/'tts'

    def test_joint_request_preserves_name_mapping_and_default_delivery(self):
        groups = eleven.split_dialogue([self.script])
        self.assertEqual(len(groups), 1)
        payload = eleven.build_payload(groups[0], CONFIG)
        self.assertEqual([t['voice_id'] for t in payload['inputs']],
                         ['male-voice-id', 'female-voice-id', 'female-voice-id', 'male-voice-id'])
        self.assertEqual([t['text'] for t in payload['inputs']],
                         ['Hello Mia.', 'Hello Alex.', 'Welcome back.', 'Thank you.'])
        self.assertEqual(set(payload), {'model_id', 'inputs'})

    def test_character_chunks_preserve_every_word_and_speaker(self):
        self.script.write_text('Mia: ' + 'An ordinary word. '*300 + '\nAlex: The ending.')
        groups = eleven.split_dialogue([self.script])
        self.assertGreater(len(groups), 1)
        self.assertTrue(all(sum(len(t['text']) for t in g) <= 2000 for g in groups))
        expected = [(t['speaker'], w) for t in eleven.read_turns([self.script]) for w in t['text'].split()]
        actual = [(t['speaker'], w) for g in groups for t in g for w in t['text'].split()]
        self.assertEqual(actual, expected)

    def test_plain_script_needs_only_speaker_and_spoken_words(self):
        self.script.write_text('Mia: Really?\nAlex: Of course.')
        self.assertEqual(eleven.build_payload(eleven.split_dialogue([self.script])[0], CONFIG)['inputs'],
                         [{'text': 'Really?', 'voice_id': 'female-voice-id'},
                          {'text': 'Of course.', 'voice_id': 'male-voice-id'}])
        self.assertEqual(eleven.read_turns([self.script]),
                         [{'speaker': 'Mia', 'text': 'Really?'}, {'speaker': 'Alex', 'text': 'Of course.'}])

    def test_resume_preserves_paid_parts_and_carries_request_context(self):
        self.script.write_text('Mia: One small sentence.\nAlex: Another tiny line.\nMia: Final short phrase.')
        with patch.object(eleven, 'MAX_CHARACTERS', 30), \
             patch.object(eleven, 'call_api', side_effect=[(PCM, 'request-one'), RuntimeError('quota')]) as api:
            with self.assertRaisesRegex(RuntimeError, 'quota'):
                eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
            self.assertEqual(api.call_count, 2)
        first = (self.output/'part001.wav').read_bytes()
        with patch.object(eleven, 'MAX_CHARACTERS', 30), \
             patch.object(eleven, 'call_api', side_effect=[(PCM, 'request-two'), (PCM, 'request-three')]) as api:
            joined = eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
            self.assertEqual(api.call_count, 2)
            payload = api.call_args_list[0].args[0]
            self.assertEqual(payload['previous_request_ids'], ['request-one'])
            self.assertEqual(payload['previous_text'], 'One small sentence.')
            self.assertEqual(payload['future_text'], 'Final short phrase.')
            self.assertEqual((self.output/'part001.wav').read_bytes(), first)
            with wave.open(str(joined), 'rb') as audio:
                # No inserted gaps, resampling or tempo changes in the join.
                self.assertEqual(audio.readframes(audio.getnframes()), PCM*3)
        self.assertNotIn('not-a-real-key', (self.output/'inputs.json').read_text())
        with patch.object(eleven, 'MAX_CHARACTERS', 30), patch.object(eleven, 'call_api') as api:
            self.assertEqual(eleven.generate_audio([self.script], self.output, CONFIG, key=''), joined)
            api.assert_not_called()

    def test_changed_voice_or_script_cannot_reuse_cached_audio(self):
        with patch.object(eleven, 'call_api', return_value=(PCM, 'request-one')):
            eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
        original = (self.output/'part001.wav').read_bytes()
        changed = {'model': CONFIG['model'], 'voices': {**CONFIG['voices'], 'Mia': 'other-voice-id'}}
        with patch.object(eleven, 'call_api') as api:
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                eleven.generate_audio([self.script], self.output, changed, key='not-a-real-key')
            self.script.write_text('Mia: A different script.')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
            api.assert_not_called()
        self.assertEqual((self.output/'part001.wav').read_bytes(), original)

    def test_truncated_cached_audio_stops_before_any_new_request(self):
        with patch.object(eleven, 'call_api', return_value=(PCM, 'request-one')):
            eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
        wav = self.output/'part001.wav'
        wav.write_bytes(wav.read_bytes()[:-100])
        with patch.object(eleven, 'call_api') as api:
            with self.assertRaisesRegex(RuntimeError, 'Truncated'):
                eleven.generate_audio([self.script], self.output, CONFIG, key='not-a-real-key')
            api.assert_not_called()

    def test_http_quota_error_is_not_retried_and_does_not_expose_key(self):
        key = 'not-a-real-key'
        failure = HTTPError(eleven.API_URL, 429, 'quota', {}, io.BytesIO(f'quota {key}'.encode()))
        with patch.object(eleven.request, 'urlopen', side_effect=failure) as urlopen:
            with self.assertRaises(RuntimeError) as caught:
                eleven.call_api({'inputs': []}, key)
            self.assertIn('429', str(caught.exception))
            self.assertNotIn(key, str(caught.exception))
            urlopen.assert_called_once()

    def test_pcm_response_transport_and_validation(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Type': 'audio/pcm', 'request-id': 'request-one'}
        response.read.return_value = PCM
        with patch.object(eleven.request, 'urlopen', return_value=response) as urlopen:
            pcm, request_id = eleven.call_api({'inputs': []}, 'not-a-real-key')
            self.assertEqual((pcm, request_id), (PCM, 'request-one'))
            self.assertIn('output_format=pcm_24000', urlopen.call_args.args[0].full_url)
            response.read.return_value = b'odd'
            with self.assertRaisesRegex(RuntimeError, 'incomplete PCM'):
                eleven.call_api({'inputs': []}, 'not-a-real-key')
            response.headers['Content-Type'] = 'application/json'
            with self.assertRaisesRegex(RuntimeError, 'PCM audio'):
                eleven.call_api({'inputs': []}, 'not-a-real-key')

    def test_dry_run_never_calls_api_or_creates_output(self):
        result = subprocess.run([sys.executable, 'generate_elevenlabs.py', '--parts', str(self.script),
                                 '--outdir', str(self.output), '--dry-run'],
                                cwd=Path(__file__).resolve().parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No API calls', result.stdout)
        self.assertFalse(self.output.exists())

    def test_one_click_elevenlabs_and_completed_resume_never_enter_gemini(self):
        self.script.write_text('Mia: Hello.\nAlex: Welcome.')
        vi = self.root/'vi.txt'
        vi.write_text('Mia: Xin chào.\nAlex: Chào mừng.')
        make_wav(self.root/'assets/intro.wav')
        args = ['one_click.py', '--script', str(self.script), '--script-vi', str(vi),
                '--tts-provider', 'elevenlabs', '--episode', 'TEST', '--lead', '0']
        commands = []

        def run(command, **kwargs):
            commands.append([str(x) for x in command])
            make_wav(Path(command[-1]))

        recognized = [{'word': 'Hello', 'start': .2, 'end': .8}, {'word': 'Welcome', 'start': 1, 'end': 1.8}]
        with patch.object(one_click, '__file__', str(self.root/'one_click.py')), \
             patch.object(one_click, 'find_ffmpeg', return_value='ffmpeg'), \
             patch.object(one_click, 'run', side_effect=run), \
             patch.object(one_click, 'ensure_audio', side_effect=AssertionError('Gemini must not run')), \
             patch.object(eleven, 'configuration', return_value=CONFIG), \
             patch.object(eleven, 'call_api', return_value=(PCM, 'request-one')) as api, \
             patch.object(one_click, 'transcribe_words', return_value=recognized), \
             patch.object(one_click, 'render_videos', return_value=self.root/'video.mp4'), \
             patch.object(sys, 'argv', args):
            one_click.main()
            one_click.main()
            api.assert_called_once()
        self.assertFalse(any('atempo' in part or 'generate.py' in part for c in commands for part in c))
        settings = json.loads((self.root/'episodes/TEST/work/inputs.json').read_text())
        self.assertEqual(settings['elevenlabs']['voices'], CONFIG['voices'])

    def test_gemini_restored_delivery_reuses_complete_cache_but_rejects_mixed_parts(self):
        self.script.write_text('Mia: Hello.\nAlex: Welcome.')
        vi = self.root/'vi.txt'
        vi.write_text('Mia: Xin chào.\nAlex: Chào mừng.')
        make_wav(self.root/'assets/intro.wav')
        work = self.root/'episodes/TEST/work'
        work.mkdir(parents=True)
        settings = {'version': 2, 'sources': [{'name': self.script.name, 'sha256': one_click.file_hash(self.script)}],
                    'lead': 0., 'tts_model': 'gemini-3.8-flash-tts', 'mia_voice': 'Kore', 'alex_voice': 'Puck',
                    'whisper_model': 'base', 'script_vi': one_click.file_hash(vi),
                    'reuse_audio': None, 'reuse_tts': None,
                    'gemini_delivery': 'contextual-conversation-v1'}
        one_click.save_json(work/'inputs.json', settings)
        make_wav(work/'tts/part001.wav')
        original_part = work/'input_parts/part001.txt'
        original_part.parent.mkdir()
        original_part.write_text('Mia: Original cached dialogue.')
        args = ['one_click.py', '--script', str(self.script), '--script-vi', str(vi), '--episode', 'TEST', '--lead', '0']
        recognized = [{'word': 'Hello', 'start': .2, 'end': .8}, {'word': 'Welcome', 'start': 1, 'end': 1.8}]
        with patch.object(one_click, '__file__', str(self.root/'one_click.py')), \
             patch.object(one_click, 'find_ffmpeg', return_value='ffmpeg'), \
             patch.object(one_click, 'ensure_audio', return_value=3) as gemini, \
             patch.object(eleven, 'generate_audio') as eleven_audio, \
             patch.object(one_click, 'transcribe_words', return_value=recognized), \
             patch.object(one_click, 'render_videos', return_value=self.root/'video.mp4'), \
             patch.object(sys, 'argv', args):
            with self.assertRaisesRegex(RuntimeError, 'NEW --episode'):
                one_click.main()
            gemini.assert_not_called()
            make_wav(self.root/'episodes/TEST/final/00_AUDIO.wav')
            one_click.main()
            self.assertEqual(gemini.call_args.kwargs['mia_voice'], 'Kore')
            self.assertEqual(gemini.call_args.kwargs['alex_voice'], 'Puck')
            self.assertEqual(json.loads((work/'inputs.json').read_text()), settings)
            self.assertEqual(original_part.read_text(), 'Mia: Original cached dialogue.')
            with patch.object(eleven, 'configuration', return_value=CONFIG), \
                 patch.object(sys, 'argv', args+['--tts-provider', 'elevenlabs']):
                with self.assertRaisesRegex(RuntimeError, 'NEW --episode'):
                    one_click.main()
            eleven_audio.assert_not_called()


if __name__ == '__main__':
    unittest.main()
