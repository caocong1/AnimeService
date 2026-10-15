"""Launched in a pinned adapter environment; sees no reference answers."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata as metadata
import json
import os
import time
import threading
from pathlib import Path

from .evidence import audit_wav, peak_rss_bytes, save_new, validate_observation

for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'):
    os.environ[key] = '1'
os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--request', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    req = json.loads(a.request.read_bytes())
    model_path = Path(req['model']).resolve(strict=True)
    if not model_path.is_dir():
        raise ValueError('Only an existing local model directory is allowed')
    started = time.perf_counter()
    versions = {}
    for key in ('torch','transformers','torchvision','pillow','numpy','librosa','soundfile',
                'tokenizers','huggingface-hub','clearvoice','rotary-embedding-torch',
                'faster-whisper','ctranslate2','qwen-asr'):
        try:
            versions[key]=metadata.version(key)
        except metadata.PackageNotFoundError:
            pass
    if req['adapter']=='reazon':
        import sherpa_onnx
        versions['sherpa-onnx']=metadata.version('sherpa-onnx')
        model=sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_path/'encoder-epoch-99-avg-1.onnx'),
            decoder=str(model_path/'decoder-epoch-99-avg-1.onnx'),
            joiner=str(model_path/'joiner-epoch-99-avg-1.onnx'),
            tokens=str(model_path/'tokens.txt'),num_threads=6,sample_rate=16000,
            feature_dim=80,provider='cpu',decoding_method='modified_beam_search',
            max_active_paths=4,debug=False)
    elif req['adapter'] == 'whisper':
        from faster_whisper import WhisperModel
        for key in ('faster-whisper', 'ctranslate2'):
            versions[key] = metadata.version(key)
        model = WhisperModel(str(model_path), device=req['device'], compute_type=req['dtype'],
                             cpu_threads=6, num_workers=1, local_files_only=True)
    else:
        import torch
        torch.set_num_threads(6)
        runtime_packages=('torch','clearvoice') if req['adapter']=='moss' else (
            ('torch','torchaudio') if req['adapter']=='ps4' else ('torch','transformers'))
        for key in runtime_packages:
            versions[key] = metadata.version(key)
        dtype = {'fp32': torch.float32, 'fp16': torch.float16, 'bf16': torch.bfloat16}[req['dtype']]
        if req['device'] == 'cpu' and req['dtype'] != 'fp32':
            raise ValueError('CPU reference uses explicit FP32')
        if req['device'] == 'cuda':
            if not torch.cuda.is_available():
                raise ValueError('CUDA unavailable; do not silently fall back')
            torch.cuda.reset_peak_memory_stats()
        if req['adapter']=='ps4':
            import importlib.util
            module_spec=importlib.util.spec_from_file_location('pinned_ps4',model_path/'inference.py')
            upstream=importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(upstream)
            model=upstream.build_model(torch.device(req['device']))
            checkpoint=torch.load(model_path/'checkpoint_epoch037.pt',map_location='cpu',weights_only=True)
            state=checkpoint.get('model',checkpoint.get('state_dict',checkpoint))
            model.load_state_dict(state,strict=True)
            model=model.to(req['device'],dtype=dtype).eval()
            versions['torchaudio']=metadata.version('torchaudio')
        elif req['adapter']=='ruby_ctc':
            import sys
            # Explicitly reviewed, pinned local upstream source, included in the
            # hashed runtime manifest. No Hub code or implicit download executes.
            sys.path.insert(0,str(model_path/'source'))
            from ruby_asr import MoraCTCRecognizer
            ruby_recognizer=MoraCTCRecognizer.from_pretrained(
                str(model_path/'verbatim'),device=req['device'],dtype=dtype)
            model=ruby_recognizer.model
        elif req['adapter']=='moss':
            from clearvoice.models.mossformer2_ss.mossformer2 import MossFormer2_SS_16K
            args=argparse.Namespace(encoder_embedding_dim=512,mossformer_sequence_dim=512,
                 num_mossformer_layer=24,encoder_kernel_size=16,num_spks=2)
            model=MossFormer2_SS_16K(args).model
            state=torch.load(model_path/'last_best_checkpoint.pt',map_location='cpu',weights_only=True)
            state=state.get('model',state)
            state={k.removeprefix('module.'):v for k,v in state.items()}
            model.load_state_dict(state,strict=True)
            model=model.to(req['device'],dtype=dtype).eval()
        else:
            from transformers import AutoModelForMultimodalLM, AutoModelForSpeechSeq2Seq, AutoProcessor
            # backend=pil is forwarded to the video subprocessor in 5.17.0 and
            # raises on its read-only backend property. The tested legacy flag works.
            processor_options = {'use_fast': False} if req['adapter'] == 'gemma' else {}
            processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True,
                                                     trust_remote_code=False, **processor_options)
            model_class=AutoModelForSpeechSeq2Seq if req['adapter']=='anime_whisper' else AutoModelForMultimodalLM
            model = model_class.from_pretrained(
                str(model_path), local_files_only=True, trust_remote_code=False, dtype=dtype
            ).to(req['device']).eval()
    load_seconds = time.perf_counter() - started
    save_new(a.out / 'load.json', {'versions': versions, 'load_seconds': load_seconds,
                                'adapter': req['adapter'], 'device': req['device'], 'dtype': req['dtype']})
    for item in req['cases']:
        def expire(case_id=item['id']):
            save_new(a.out / (case_id + '.timeout.json'), {'case_id': case_id, 'status': 'timeout',
                                                       'acceptance': False})
            os._exit(124)
        deadline = threading.Timer(req.get('case_timeout_seconds', 120), expire)
        deadline.daemon = True
        deadline.start()
        print(json.dumps({'case': item['id'], 'status': 'started'}), flush=True)
        wav = Path(item['wav']).resolve(strict=True)
        audio = audit_wav(wav)
        if audio['wav_sha256'] != item['wav_sha256']:
            raise ValueError('Input changed after freeze')
        t0 = time.perf_counter()
        base = {'schema_version': 1, 'status': 'proposal_only', 'scope': ['lexical_proposal'],
                'human_listened': False, 'current_codex_listened': False, 'subtitle_acceptance': False,
                'timing_is_estimated': True, 'observed_media_span': None,
                'case_id': item['id'], 'audio': audio, 'runtime': req['adapter'], 'versions': versions,
                'device': req['device'], 'dtype': req['dtype'], 'context_mode': 'blind',
                'reference_text_visible': False, 'requested_language': item['language']}
        base['model_manifest_sha256'] = req.get('model_manifest_sha256')
        if req['adapter'] not in ('whisper','reazon'):
            base['actual_parameter_dtype']=str(next(model.parameters()).dtype)
            base['attention_implementation']=getattr(getattr(model,'config',None),'_attn_implementation',None)
        if req.get('zero_pcm_guard') and audio['peak']==0:
            base.update(text_hypothesis='',model_inference_performed=False,
                        finish_reason='deterministic_exact_zero_pcm',zero_pcm_guard=True)
        elif req['adapter']=='ps4':
            import numpy as np
            import wave
            import soundfile as sf
            enrollment=Path(item['enrollment_wav']).resolve(strict=True)
            enroll_audit=audit_wav(enrollment)
            if enroll_audit['wav_sha256']!=item['enrollment_sha256']:
                raise ValueError('Enrollment changed after freeze')
            def pcm(path):
                with wave.open(str(path),'rb') as w:
                    y=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
                gain=1/max(float(np.max(np.abs(y))),1e-12) if np.any(y) else 1.0
                return torch.from_numpy((y*gain)[None]).to(req['device'],dtype),gain
            mixture,mix_gain=pcm(wav);enroll,enroll_gain=pcm(enrollment)
            with torch.inference_mode():
                signal,_=model(mixture,enroll)
            signal=signal[0].detach().cpu().numpy()
            if len(signal)!=audio['sample_count'] or not np.isfinite(signal).all():
                raise ValueError('Extracted length/finiteness mismatch')
            gain=min(1.0,0.99/max(float(np.max(np.abs(signal))),1e-12))
            dest=a.out/(item['id']+'-extracted.wav')
            sf.write(str(dest),signal*gain,16000,subtype='PCM_16')
            base.update(scope=['separation_derivative'],text_hypothesis=None,
                        enrollment_audio=enroll_audit,enrollment_sha256=item['enrollment_sha256'],
                        input_peak_normalization_gain=mix_gain,enrollment_peak_normalization_gain=enroll_gain,
                        extracted={'path':str(dest),'gain':gain,'audio':audit_wav(dest)},
                        context_mode='candidate_speaker_enrollment_audio_no_text',
                        source_slots=1,speaker_identity_accepted=False,parameter_load_strict=True,
                        extraction_latency_samples=None,finish_reason='whole_short_window_no_splicing')
        elif req['adapter']=='ruby_ctc':
            import numpy as np
            import wave
            with wave.open(str(wav),'rb') as w:
                floats=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
            captured={}
            def capture_ctc(module,args,result):
                captured['raw_frame_argmax_ids']=result.float().argmax(dim=-1)[0].tolist()
                captured['ctc_frame_count']=int(result.shape[1])
            hook=ruby_recognizer.ctc_branch.register_forward_hook(capture_ctc)
            try:
                text=ruby_recognizer.transcribe(floats,batch_size=1)[0]
            finally:
                hook.remove()
            base.update(text_hypothesis=text,raw_tokenizer_text=text,**captured,
                        decoding_method='greedy_CTC_repeat_collapse_drop_blank',
                        language_decoder_used=False,ctc_state_strict=True,
                        finish_reason='offline_CTC_complete')
        elif req['adapter']=='reazon':
            import numpy as np
            import wave
            with wave.open(str(wav),'rb') as w:
                floats=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
            stream=model.create_stream();stream.accept_waveform(16000,floats)
            model.decode_stream(stream)
            base.update(text_hypothesis=stream.result.text,raw_tokens=list(stream.result.tokens),
                        token_timestamps_relative=list(stream.result.timestamps),
                        finish_reason='offline_decode_complete',decoding_method='modified_beam_search')
        elif req['adapter']=='moss':
            import numpy as np
            import wave
            import soundfile as sf
            with wave.open(str(wav),'rb') as w:
                floats=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
            with torch.inference_mode():
                outputs=model(torch.from_numpy(floats[None]).to(req['device'],dtype))
            streams=[]
            for i,tensor in enumerate(outputs):
                signal=tensor[0].detach().cpu().numpy()
                if len(signal)!=len(floats) or not np.isfinite(signal).all():
                    raise ValueError('Separated length/finiteness mismatch')
                gain=min(1.0,0.99/max(float(np.max(np.abs(signal))),1e-12))
                dest=a.out/(item['id']+f'-stream{i}.wav')
                sf.write(str(dest),signal*gain,16000,subtype='PCM_16')
                streams.append({'path':str(dest),'gain':gain,'audio':audit_wav(dest)})
            base.update(scope=['separation_derivative'],text_hypothesis=None,streams=streams,
                        source_slots=2,speaker_count_accepted=False,separation_latency_samples=None,
                        normalization='original_model_scale; only anti-clipping uniform gain',
                        inference_window='whole_short_window_no_chunk_splicing')
        elif req['adapter'] == 'whisper':
            segs, info = model.transcribe(str(wav), language=item['language'], beam_size=5,
                                        vad_filter=req['vad'], condition_on_previous_text=False,
                                        word_timestamps=True)
            rows = []
            for seg in segs:
                rows.append({'start': seg.start, 'end': seg.end, 'text': seg.text,
                             'avg_logprob': seg.avg_logprob, 'no_speech_prob': seg.no_speech_prob,
                             'words': [dataclasses.asdict(w) if dataclasses.is_dataclass(w) else w._asdict()
                                       for w in seg.words or []]})
            base.update(text_hypothesis=''.join(s['text'] for s in rows).strip(),
                        segments=rows, vad=req['vad'], finish_reason='generator_exhausted')
        else:
            import numpy as np
            import wave
            with wave.open(str(wav), 'rb') as w:
                floats = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768
            if req['adapter']=='anime_whisper':
                inputs=processor(floats,sampling_rate=16000,return_tensors='pt')
            elif req['adapter'] == 'qwen':
                inputs = processor.apply_transcription_request(audio=floats, language=item['language'])
            elif req['adapter'] == 'gemma':
                prompt = req.get('prompts',{}).get(item['id'],
                    'Transcribe the following speech segment in its original language. Only output the transcription; do not invent unclear words.')
                base['question']=prompt
                base['question_sha256']=hashlib.sha256(prompt.encode('utf-8')).hexdigest()
                messages = [{'role': 'user', 'content': [{'type': 'text', 'text': prompt},
                                                         {'type': 'audio', 'audio': floats}]}]
                inputs = processor.apply_chat_template(messages, tokenize=True, return_dict=True,
                                                      return_tensors='pt', add_generation_prompt=True,
                                                      enable_thinking=False)
            else:
                raise ValueError('Unknown adapter')
            inputs = inputs.to(req['device'], dtype)
            with torch.inference_mode():
                if req['adapter']=='anime_whisper':
                    output=model.generate(**inputs,language='ja',task='transcribe',
                       do_sample=False,max_new_tokens=440,no_repeat_ngram_size=0,repetition_penalty=1.0)
                else:
                    output = model.generate(**inputs, do_sample=False, max_new_tokens=512)
            generated = output if req['adapter']=='anime_whisper' else output[:, inputs['input_ids'].shape[1]:]
            tokens = generated[0].tolist()
            raw = processor.tokenizer.decode(tokens, skip_special_tokens=False)
            if req['adapter'] == 'qwen':
                parsed = processor.decode(generated, return_format='parsed')[0]
                text = parsed['transcription']
            else:
                parsed = None
                text = processor.tokenizer.decode(tokens, skip_special_tokens=True)
            eos = model.generation_config.eos_token_id
            eos = eos if isinstance(eos, list) else [eos]
            ended = bool(tokens) and tokens[-1] in eos
            base.update(text_hypothesis=text, raw_token_ids=tokens, raw_tokenizer_text=raw,
                        parsed=parsed, hit_token_limit=len(tokens) >= (440 if req['adapter']=='anime_whisper' else 512),
                        finish_reason='eos' if ended else 'limit_or_other', eos_reached=ended)
            if req['device'] == 'cuda':
                torch.cuda.synchronize()
                base['peak_vram_allocated_bytes'] = torch.cuda.max_memory_allocated()
                base['peak_vram_reserved_bytes'] = torch.cuda.max_memory_reserved()
        base['wall_seconds'] = time.perf_counter() - t0
        base['peak_rss_bytes'] = peak_rss_bytes()
        deadline.cancel()
        validate_observation(base)
        save_new(a.out / (item['id'] + '.json'), base)
        print(json.dumps({'case': item['id'], 'seconds': round(base['wall_seconds'], 2),
                          'status': 'proposal_only'}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
