import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from PIL import ImageFont

from dialogue_script import read_turns
import generate
import one_click as video
import one_click_hanzi as hanzi


def make_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x64\x00' * 72000)


class HanziTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root/'zh.txt'
        self.vi = self.root/'vi.txt'
        self.script.write_text('MIA：你好。\nALEX: 欢迎！', encoding='utf-8')
        self.vi.write_text('MIA: Xin chào.\nALEX: Chào mừng.', encoding='utf-8')

    def test_fullwidth_colons_and_speaker_identity(self):
        self.assertEqual(read_turns([self.script]), [
            {'speaker': 'Mia', 'text': '你好。'}, {'speaker': 'Alex', 'text': '欢迎！'}])

    def test_chinese_units_are_characters_without_losing_latin_names(self):
        self.assertEqual(video.speech_units('你好，Hanzi Radio！２０２６年。', 'zh'),
                         ['你', '好', 'Hanzi', 'Radio', '2026', '年'])
        self.assertEqual(video.norm('你好！', 'zh'), '你好')
        self.assertEqual(video.norm("That's!"), 'thats')

    def test_chinese_cues_pair_using_characters_instead_of_spaces(self):
        cues, units = video.parse_parts([self.script], language='zh')
        paired = video.read_bilingual_cues([self.script], self.vi, language='zh')
        self.assertEqual(units, list('你好欢迎'))
        self.assertEqual([(c.first,c.last) for c in cues], [(0,1),(2,3)])
        self.assertEqual([(c.first,c.last) for c in paired], [(0,1),(2,3)])
        self.assertEqual(cues[0].en, '你好。')
        self.vi.write_text('Alex: Xin chào.\nMia: Chào mừng.')
        with self.assertRaisesRegex(ValueError, 'Speaker mismatch'):
            video.read_bilingual_cues([self.script], self.vi, language='zh')

    def test_chinese_chunk_limit_preserves_all_text_and_speakers(self):
        text = 'Mia: '+('我们需要先弄清楚自己的目标。'*20)+'\nAlex: 我同意你的看法。'
        self.script.write_text(text)
        parts = video.prepare_parts([self.script], self.root, language='zh', max_characters=80)
        self.assertGreater(len(parts),1)
        before = [(t['speaker'],c) for t in read_turns([self.script]) for c in t['text']]
        after = [(t['speaker'],c) for t in read_turns(parts) for c in t['text']]
        self.assertEqual(before,after)
        self.assertTrue(all(sum(len(t['text']) for t in read_turns([p])) <= 80 for p in parts))

    def test_chinese_wrap_avoids_punctuation_at_line_start(self):
        font=ImageFont.truetype(str(hanzi.choose_chinese_font()),40)
        text='她说：“我们不能只关注结果，也应该认真思考过程。”'
        lines=video.wrap_text(text,font,160)
        self.assertEqual(''.join(lines),text)
        self.assertTrue(all(font.getlength(line) <= 160 for line in lines))
        self.assertTrue(all(line[0] not in '，。！？）》】”' for line in lines))
        self.assertTrue(all(line[-1] not in '（《【“' for line in lines))

    def test_mandarin_payload_preserves_transcript_and_supported_schema(self):
        turns=read_turns([self.script])
        payload=generate.build_payload(turns,'gemini-3.8-flash-tts','Kore','Puck',language='zh')
        config=payload['generation_config']['speech_config']
        self.assertEqual(set(config),{'speakers'})
        self.assertEqual(config['speakers'],[{'speaker':'Mia','voice':'Kore'},{'speaker':'Alex','voice':'Puck'}])
        self.assertEqual([t['text'] for t in payload['input'][0]['content']], ['你好。','欢迎！'])
        for item in payload['input'][0]['content']:
            style=item['annotations'][0]['style']
            self.assertIn('Mandarin',style)
            self.assertNotIn('British',style)
            self.assertNotIn('American',style)
        en=generate.build_payload([{'speaker':'Mia','text':'Hello.'}],'test','Kore','Puck')
        self.assertIn('British',en['input'][0]['content'][0]['annotations'][0]['style'])

    def test_whisper_is_chinese_and_distributes_multi_character_word_timestamps(self):
        seen={}
        class FakeWhisper:
            def __init__(self,*args,**kwargs):
                pass
            def transcribe(self,audio,**kwargs):
                seen.update(kwargs)
                words=[SimpleNamespace(word='你好，',start=1.,end=2.),
                       SimpleNamespace(word='欢迎',start=2.,end=3.)]
                return [SimpleNamespace(words=words)],None
        fake=SimpleNamespace(WhisperModel=FakeWhisper)
        with patch.dict(sys.modules,{'faster_whisper':fake}):
            words=video.transcribe_words(self.root/'audio.wav','base',self.root/'asr.json',language='zh')
        self.assertEqual(seen['language'],'zh')
        self.assertEqual([w['word'] for w in words],list('你好欢迎'))
        self.assertEqual([(w['start'],w['end']) for w in words],[(1,1.5),(1.5,2),(2,2.5),(2.5,3)])
        cues,units=video.parse_parts([self.script],language='zh')
        score=video.align_cues(cues,units,words,4,0,language='zh')
        self.assertEqual(score,1.)
        self.assertLess(cues[0].start,cues[0].end)
        self.assertLessEqual(cues[0].end,cues[1].start)

    def test_intro_cache_depends_on_voice_and_channel_not_episode(self):
        config={'provider':'gemini','model':'test','voices':{'Mia':'Kore','Alex':'Puck'}}
        first,settings=hanzi.intro_location(config)
        changed,_=hanzi.intro_location({**config,'voices':{'Mia':'other','Alex':'Puck'}})
        self.assertNotEqual(first,changed)
        self.assertEqual(settings['channel'],'Hanzi Radio')
        self.assertEqual(hanzi.intro_location(config)[0],first)

    def test_complete_audio_returns_without_tts(self):
        destination=self.root/'00_AUDIO.wav'
        make_wav(destination)
        with patch.object(video,'ensure_audio') as tts:
            self.assertEqual(hanzi.create_track([self.script],self.root,destination,
                                               {'provider':'gemini'},0,'ffmpeg'),3.)
            tts.assert_not_called()

    def test_gemini_child_process_receives_chinese_language(self):
        work=self.root/'work';work.mkdir()
        commands=[]
        def run(command,**kwargs):
            commands.append([str(x) for x in command])
            if '--parts' in command:
                source=Path(command[command.index('--parts')+1])
                folder=Path(command[command.index('--outdir')+1])
                make_wav(folder/(source.stem+'.wav'))
            else:
                make_wav(Path(command[-1]))
        config={'provider':'gemini','model':'test','voices':{'Mia':'Kore','Alex':'Puck'}}
        with patch.object(video,'run',side_effect=run):
            hanzi.create_track([self.script],work,self.root/'audio.wav',config,0,'ffmpeg')
        tts=next(c for c in commands if '--parts' in c)
        self.assertEqual(tts[-2:],['--language','zh'])

    def test_reuse_audio_creates_no_intro_or_dialogue_tts(self):
        source=self.root/'raw.wav';make_wav(source)
        out=self.root/'episodes'
        recognized=[{'word':c,'start':.1+i*.4,'end':.4+i*.4} for i,c in enumerate('你好欢迎')]
        def copy_audio(raw,destination,lead,ffmpeg):
            make_wav(destination)
            return 3.
        args=['one_click_hanzi.py','--script',str(self.script),'--script-vi',str(self.vi),
              '--reuse-audio',str(source),'--out-root',str(out),'--lead','0']
        with patch.object(hanzi,'HANZI_ROOT',self.root/'hanzi'), \
             patch.object(video,'find_ffmpeg',return_value='ffmpeg'), \
             patch.object(hanzi,'copy_audio',side_effect=copy_audio), \
             patch.object(hanzi,'prepare_intro') as intro, \
             patch.object(hanzi,'create_track') as tts, \
             patch.object(video,'transcribe_words',return_value=recognized) as asr, \
             patch.object(video,'render_videos',return_value=out/'video.mp4') as render, \
             patch.object(sys,'argv',args):
            hanzi.main()
            intro.assert_not_called();tts.assert_not_called()
            self.assertEqual(asr.call_args.kwargs['language'],'zh')
            self.assertEqual(render.call_args.kwargs['channel_name'],'Hanzi Radio')
            self.assertIsNone(render.call_args.kwargs['intro_audio'])
            self.assertEqual(render.call_args.kwargs['cards'],hanzi.CARDS)
        self.assertTrue((out/'HZ01_GEMINI/work/chinese.srt').is_file())
        self.assertFalse((out/'HZ01_GEMINI/work/english.srt').exists())

    def test_dry_run_creates_no_output_or_api_requests(self):
        out=self.root/'output'
        result=subprocess.run([sys.executable,'one_click_hanzi.py','--script',str(self.script),
                               '--script-vi',str(self.vi),'--out-root',str(out),'--dry-run'],
                              cwd=Path(__file__).resolve().parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('No API calls',result.stdout)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
