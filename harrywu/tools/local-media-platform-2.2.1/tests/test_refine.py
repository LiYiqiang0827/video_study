import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from media_platform.refine import (align, merge_candidates, normalized, flag_group,
    validate_decisions, conservative_decisions, safe_child, validate_config, disk_check,
    BudgetStop, object_hash, windows, exclusive_lock, source_and_artifacts, include_new_turns, Engine, write_results, audio_timeline)


def row(id,a,b,text,window="one"):
    return dict(id=id,start=a,end=b,text=text,window=window,source_sha256="source",review=False)


class RefineTests(unittest.TestCase):
    def test_audio_start_accounts_for_container_origin(self):
        self.assertEqual(audio_timeline({'format':{'start_time':'.023021'}},
            {'start_time':'.023021','duration':'12'})['start'],0)
        self.assertEqual(audio_timeline({'format':{}},{'start_time':'2','duration':'1'})['end'],3)
        self.assertIsNone(audio_timeline({'format':{}},{'duration':'N/A'})['end'])

    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg integration test')
    def test_delayed_audio_preserves_source_timestamp(self):
        from media_platform.ingest import digest,probe
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();out=root/'out';out.mkdir();source=root/'delay.mp4'
            subprocess.run(['ffmpeg','-nostdin','-y','-loglevel','error','-f','lavfi','-i','color=c=black:s=16x16:r=10:d=4',
                '-itsoffset','2','-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=1',
                '-c:v','mpeg4','-c:a','aac',str(source)],check=True,capture_output=True)
            info=probe(source);audio=next(s for s in info['streams'] if s['codec_type']=='audio')
            args=SimpleNamespace(device='cpu',compute_type='int8',language='ja')
            manifest={'artifacts':{},'source':str(source),'source_sha256':digest(source),'audio_stream':audio['index'],
                'config':validate_config({'min_free_bytes':1}),'audio_timeline':audio_timeline(info,audio)}
            engine=Engine(args,manifest,out,work);identity={'id':'test'}
            engine.loaded=object_hash(identity)
            engine.model=SimpleNamespace(transcribe=lambda *a,**k:([SimpleNamespace(start=0,end=.5,text='はい',avg_logprob=-.1,no_speech_prob=.1)],SimpleNamespace(language='ja')))
            record=engine.recognize(windows(0,4,60,0,'delayed')[0],identity,False)
            self.assertGreater(record['segments'][0]['start'],1.8)
            self.assertEqual(record['no_decoded_samples_intervals'][0],[0,manifest['audio_timeline']['start']])
            empty=engine.recognize(windows(0,1,60,0,'before-audio')[0],identity,False)
            self.assertEqual(empty['segments'],[])
            self.assertEqual(empty['no_decoded_samples_intervals'],[[0,1]])

    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg integration test')
    def test_real_container_tail_never_reaches_model_and_partial_window_is_clamped(self):
        from media_platform.ingest import digest,probe
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();out=root/'out';out.mkdir()
            source=root/'tail.mp4'
            subprocess.run(['ffmpeg','-nostdin','-y','-loglevel','error','-f','lavfi','-i','color=c=black:s=16x16:r=10:d=4',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=1.5',
                '-c:v','mpeg4','-c:a','aac',str(source)],check=True,capture_output=True)
            info=probe(source);audio=next(s for s in info['streams'] if s['codec_type']=='audio')
            args=SimpleNamespace(device='cpu',compute_type='int8',language='ja')
            manifest={'artifacts':{},'source':str(source),'source_sha256':digest(source),'audio_stream':audio['index'],
                'config':validate_config({'min_free_bytes':1}),'audio_timeline':audio_timeline(info,audio)}
            engine=Engine(args,manifest,out,work)
            identity={'id':'must-not-load'}
            with patch('media_platform.refine.resource_snapshot',side_effect=AssertionError('Empty audio must not load model')):
                empty=engine.recognize(windows(2,4,60,0,'empty')[0],identity,False)
            self.assertEqual(empty['decoded_frames'],0)
            self.assertEqual(empty['segments'],[])
            self.assertEqual(empty['no_decoded_samples_intervals'],[[2,4]])
            self.assertEqual(empty['recognition_status'],'skipped_no_decodable_audio')
            self.assertEqual(manifest['temporary_files'],[])
            fake_segment=SimpleNamespace(start=0,end=8,text='音声候補',avg_logprob=-.1,no_speech_prob=.1)
            fake_model=SimpleNamespace(transcribe=lambda *a,**k:([fake_segment],SimpleNamespace(language='ja')))
            engine.loaded=object_hash(identity);engine.model=fake_model
            partial=engine.recognize(windows(1,4,60,0,'partial')[0],identity,False)
            self.assertGreater(partial['decoded_frames'],0)
            self.assertLess(partial['decoded_interval'][1],1.7)
            self.assertEqual(partial['segments'][0]['end'],round(partial['decoded_interval'][1],3))
            self.assertEqual(manifest['temporary_files'],[])

    def test_many_to_many_and_missing_short_response(self):
        a=[row("a",0,4,"今日は楽しい"),row("b",5,6,"うん")]
        b=[row("c",0,2,"今日は"),row("d",2,4,"楽しい")]
        groups=align(a,b)
        self.assertEqual(len(groups),2)
        self.assertEqual(groups[0]["baseline_text"],groups[0]["second_text"])
        self.assertEqual(groups[1]["baseline_text"],"うん")
        self.assertIn("possible_missing_turn",flag_group(groups[1],[],1))

    def test_negation_and_kanji_not_normalized_away(self):
        self.assertNotEqual(normalized("行かない"),normalized("行く"))
        self.assertNotEqual(normalized("姫奈"),normalized("ひな"))
        self.assertEqual(normalized("Ａ、 １。"),normalized("A1"))
        g=align([row("a",0,2,"行かない")],[row("b",0,2,"行く")])[0]
        self.assertIn("substantive_conflict",flag_group(g,[],1))
        self.assertIn("number_negation_or_quote",flag_group(g,[],1))

    def test_no_midpoint_or_fuzzy_drop(self):
        rows=[row("a",59,62,"はい、そうです", "left"),
              row("b",59.1,62.1,"はい、そうです", "right"),
              row("c",61,63,"はい", "right"),row("d",62.5,63.5,"はい","left"),
              row("e",59,62,"いいえ、違います","third")]
        kept,events=merge_candidates(rows)
        self.assertEqual(len(kept),4)
        self.assertEqual(len(events),1)
        self.assertTrue(any(r["text"]=="いいえ、違います" for r in kept))
        self.assertEqual(sum(r["text"]=="はい" for r in kept),2)

    def test_complete_input_windows(self):
        w=windows(1,143,60,5,"second")
        self.assertEqual(w[0]["start"],1)
        self.assertEqual(w[-1]["end"],143)
        self.assertTrue(all(a["end"]>=b["start"] for a,b in zip(w,w[1:])))

    def test_retry_only_short_turn_enters_review(self):
        groups=align([row('a',0,2,'先の話')],[row('b',0,2,'先の話')])
        groups=include_new_turns(groups,[row('r',4,4.5,'うん','retry')],[])
        self.assertEqual(len(groups),2)
        self.assertEqual(groups[1]['second_text'],'うん')
        self.assertIn('supplementary_asr_only',groups[1]['flags'])

    def test_registered_cleanup_preserves_other_files_and_refuses_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();out=root/'out';out.mkdir()
            (work/'registered.wav').write_bytes(b'a');(work/'unrelated.wav').write_bytes(b'b')
            m={'temporary_files':['registered.wav'],'artifacts':{}}
            engine=Engine(None,m,out,work);engine.cleanup()
            self.assertFalse((work/'registered.wav').exists())
            self.assertTrue((work/'unrelated.wav').exists())
            m['temporary_files']=['../unrelated.wav']
            with self.assertRaises(ValueError):engine.cleanup()

    def packet(self):
        a=row("a",0,2,"いいえ")
        b=row("b",0,2,"はい")
        return dict(run_id="run",source_sha256="source",interval=[0,2],candidates={"a":a,"b":b},groups=[dict(
            id="g",start=0,end=2,baseline_text="いいえ",second_text="はい",baseline_refs=["a"],second_refs=["b"],retry_refs=[])])

    def test_agent_wrong_id_omission_modality_invention_rejected(self):
        packet=self.packet()
        valid=conservative_decisions(packet)
        valid["actual_modalities"]=["text"]
        valid["reviewer"]="test"
        validate_decisions(packet,valid)
        for mutate in [lambda d:d.update(run_id="another"),
            lambda d:d.update(actual_modalities=["audio"]),
            lambda d:d.update(decisions=[]),
            lambda d:d["decisions"][0].update(selected_refs=["a","a"],refined_text="いいえいいえ"),
            lambda d:d["decisions"][0].update(candidate_refs=["absent"]),
            lambda d:d["decisions"][0].update(refined_text="架空の発言"),
            lambda d:d["decisions"][0].update(remaining_uncertainty=[])]:
            bad=copy.deepcopy(valid)
            mutate(bad)
            with self.assertRaises(ValueError): validate_decisions(packet,bad)

    def test_outside_candidate_time_rejected(self):
        packet=self.packet()
        packet["candidates"]["a"]["end"]=20
        answer=conservative_decisions(packet)
        answer["actual_modalities"]=["text"]
        with self.assertRaises(ValueError): validate_decisions(packet,answer)

    def test_config_bounds_and_disk_limits(self):
        with self.assertRaises(ValueError): validate_config({"max_rounds":3})
        with self.assertRaises(ValueError): validate_config({"max_seconds":float("nan")})
        with tempfile.TemporaryDirectory() as temp:
            config=validate_config({"min_free_bytes":1})
            with patch("media_platform.refine.shutil.disk_usage",return_value=shutil._ntuple_diskusage(100,99,1)):
                with self.assertRaises(BudgetStop): disk_check(temp,temp,config,1,1)

    def test_path_traversal_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(ValueError):safe_child(root,"../escape")
            self.assertEqual(safe_child(root,"x"),root/"x")
            target=root/"outside"
            target.mkdir()
            link=root/"link"
            if os.name=="nt":
                # Native PowerShell creates a junction; no destructive cross-shell command.
                command="New-Item -ItemType Junction -Path '"+str(link).replace("'","''")+"' -Target '"+str(target).replace("'","''")+"' | Out-Null"
                subprocess.run(["powershell","-NoProfile","-Command",command],check=True,capture_output=True)
            else: link.symlink_to(target,target_is_directory=True)
            with self.assertRaises(ValueError):safe_child(root,"link/file")
            link.rmdir() if os.name=="nt" else link.unlink()

    def test_source_and_artifact_tamper(self):
        from media_platform.ingest import digest
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"source.wav"; source.write_bytes(b"original")
            artifact=root/"candidate.json"; artifact.write_text("{}")
            manifest=dict(source=str(source),source_sha256=digest(source),artifacts={artifact.name:digest(artifact)})
            source_and_artifacts(root,manifest)
            artifact.write_text("changed")
            with self.assertRaises(ValueError):source_and_artifacts(root,manifest)
            artifact.write_text("{}")
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):source_and_artifacts(root,manifest)

    def test_process_lock_rejects_second_owner_and_releases_after_exception(self):
        script="from media_platform.refine import exclusive_lock\nwith exclusive_lock(): print('acquired')"
        with self.assertRaisesRegex(RuntimeError,'simulated stop'):
            with exclusive_lock():
                result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Another ASR process',result.stderr)
                raise RuntimeError('simulated stop')
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_completed_window_reuse_and_changed_parameters_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'out';out.mkdir();work=root/'work';work.mkdir()
            identity={'id':'local-model'};win=windows(0,20,60,5,'second')[0]
            name='candidates/'+win['id']+'.json'
            record={'window':win,'model_identity_hash':object_hash(identity),'vad':True,'initial_prompt':None,'segments':[]}
            engine=Engine(None,{'artifacts':{}},out,work);engine.checkpoint(name,record)
            with patch.object(engine,'limits',side_effect=AssertionError('Expensive path must not run')):
                self.assertEqual(engine.recognize(win,identity,True),record)
            with self.assertRaises(ValueError):engine.recognize(win,identity,False)

    def test_required_completed_images_survive_text_refinalization(self):
        for image_status,expected in [('not_run','partial'),('completed','completed')]:
            with self.subTest(image_status=image_status),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                packet=self.packet();packet['groups'][0]['flags']=[]
                answer=conservative_decisions(packet);answer['actual_modalities']=['text']
                manifest={'run_id':'r','source':'sample.mp4','source_sha256':'source','artifacts':{},
                    'config':{'image_review_required':True},'coverage':{'audio_review':'completed',
                    'text_review':'completed','image_review':image_status,'video_review':'not_run'}}
                engine=Engine(None,manifest,root,root)
                write_results(engine,packet,answer,True)
                self.assertEqual(manifest['status'],expected)
                self.assertIn('图片复核：'+image_status,(root/'unresolved.md').read_text('utf-8'))

    def test_supplementary_hallucination_can_be_explicitly_withheld(self):
        packet=self.packet();packet['groups'][0]['flags']=['supplementary_asr_only']
        answer=conservative_decisions(packet);answer['actual_modalities']=['text']
        answer['decisions'][0].update(abstain=True,selected_refs=[],refined_text='',decision='unresolved',
            reason='Only supplementary candidates; speech not confirmed',remaining_uncertainty=['Possible ending hallucination'])
        validate_decisions(packet,answer)
        packet['groups'][0]['flags']=[];answer['packet_sha256']=object_hash(packet)
        with self.assertRaises(ValueError):validate_decisions(packet,answer)


if __name__=="__main__": unittest.main()
