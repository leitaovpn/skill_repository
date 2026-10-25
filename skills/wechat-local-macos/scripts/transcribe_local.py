#!/usr/bin/env python3
"""Resume local Qwen ASR from a voice_export manifest. No model or audio downloads."""
import argparse
import collections
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time
import wave
from zoneinfo import ZoneInfo

from voice_export import private_directory, private_write


def save(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))


def model_fingerprint(model):
    files = sorted(model.glob('*.safetensors'))
    if not (model/'config.json').is_file() or not files:
        raise RuntimeError('local_model_missing_config_or_safetensors')
    digest = hashlib.sha256()
    # Include tokenizer files as well; a changed model/tokenizer must not reuse a cache.
    for path in sorted(set(files + list(model.glob('*.json')) + list(model.glob('*.txt')))):
        digest.update(path.name.encode())
        with path.open('rb') as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def load_audio(item, source_root, output):
    import numpy as np
    import pilk
    from scipy.signal import resample_poly
    source = Path(item['audio_path']).resolve(strict=True)
    if source.parent != (source_root/'source-audio').resolve():
        raise RuntimeError('audio_outside_selected_manifest')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != item['audio_sha256']:
        raise RuntimeError('audio_hash_changed')
    wav = output/'audio-wav'/(digest+'.wav')
    if not wav.exists():
        pcm = wav.with_suffix('.pcm')
        try:
            pilk.decode(str(source), str(pcm), pcm_rate=24000)
            raw = pcm.read_bytes()
            if not raw or len(raw) % 2:
                raise RuntimeError('invalid_decoded_pcm')
            temporary = wav.with_suffix('.wav.new')
            try:
                with wave.open(str(temporary), 'wb') as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(24000)
                    f.writeframes(raw)
                temporary.chmod(0o600)
                temporary.replace(wav)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            pcm.unlink(missing_ok=True)
    with wave.open(str(wav), 'rb') as f:
        if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 24000):
            raise RuntimeError('unexpected_wav_format')
        samples = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').astype(np.float32)/32768
    duration = len(samples)/24000
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    return resample_poly(samples, 2, 3).astype(np.float32), duration, rms, wav


def quality_flags(text, duration, rms, expected=None, truncated=False):
    clean = ''.join(c for c in text if c.isalnum())
    flags = []
    if not clean:
        flags.append('no_recognized_speech')
    if duration and len(clean)/duration > 14:
        flags.append('unusually_many_characters')
    if any(len(clean) >= width*4 and clean == (clean[:width]*(len(clean)//width+1))[:len(clean)] for width in range(1, min(16, len(clean)//4)+1)):
        flags.append('repetitive_output')
    if rms < .0001:
        flags.append('very_low_audio_level')
    if expected and abs(duration-expected) > max(.6, expected*.1):
        flags.append('duration_mismatch')
    if truncated:
        flags.append('generation_truncated')
    if any(x in text for x in ('感谢观看', '感謝觀看', '字幕由', '请点赞订阅', '請點讚訂閱')):
        flags.append('possible_stock_phrase_hallucination')
    return flags


def render(output, manifest, results, timezone):
    rows = []
    for item in manifest['messages']:
        row = {k: v for k, v in item.items() if k not in ('audio_path',)}
        row.update(results.get(item['message_id'], {}))
        if item['status'] == 'audio_available' and item['message_id'] not in results:
            row.update(status='pending', text='')
        row['time'] = dt.datetime.fromtimestamp(item['timestamp'], ZoneInfo(timezone)).isoformat()
        rows.append(row)
    summary = {'total_messages': len(rows), 'status_counts': dict(collections.Counter(r['status'] for r in rows)),
               'recognized_nonempty': sum(bool(r.get('text', '').strip()) for r in rows),
               'audio_uploaded': False, 'human_verified': False}
    save(output/'voice-transcripts.json', {'summary': summary, 'messages': rows})
    save(output/'progress.json', summary)
    lines = ['# 本地语音转写', '', '机器转写，未经人工逐条复听。缺失音频不补写；未标记存疑也可能识别错误。发送人是账号标签，不能单独证明实际说话人。', '', json.dumps(summary, ensure_ascii=False), '']
    for row in rows:
        lines.extend([f"## {row['time']} · {row['sender']} · {row['status']}", '',
                      *['> '+line for line in (row.get('text') or '【未取得可用文字】').splitlines()], ''])
        if row.get('quality_flags'):
            lines.extend(['复听提示：'+', '.join(row['quality_flags']), ''])
        if row.get('wav_file'):
            lines.extend(['[本地原音频](<'+row['wav_file']+'>)', ''])
        lines.extend(['消息编号：`'+row['message_id']+'`', ''])
    private_write(output/'语音转写.md', ('\n'.join(lines)+'\n').encode('utf-8'))
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--model', type=Path, required=True, help='Existing local Qwen3-ASR MLX model directory')
    ap.add_argument('--output-dir', type=Path, required=True)
    ap.add_argument('--language', default='Chinese')
    ap.add_argument('--max-new-tokens', type=int, default=512)
    ap.add_argument('--timezone', default='Asia/Shanghai')
    args = ap.parse_args()
    if not 32 <= args.max_new_tokens <= 4096:
        ap.error('--max-new-tokens must be 32..4096')
    os.umask(0o077)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    source = args.manifest.resolve(strict=True)
    manifest = json.loads(source.read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('messages'), list):
        raise RuntimeError('unsupported_voice_manifest')
    output = private_directory(args.output_dir)
    for name in ['audio-wav', 'transcription-cache']:
        private_directory(output/name)
    model = args.model.resolve(strict=True)
    config = {'model_sha256': model_fingerprint(model), 'language': args.language, 'max_new_tokens': args.max_new_tokens,
              'engine': 'mlx-qwen3-asr', 'engine_version': importlib.metadata.version('mlx-qwen3-asr')}
    from mlx_qwen3_asr import Session
    session = Session(model=str(model))
    results = {}
    render(output, manifest, results, args.timezone)
    for item in manifest['messages']:
        if item['status'] != 'audio_available':
            continue
        cache_id = hashlib.sha256(json.dumps(dict(config, audio_sha256=item['audio_sha256']), sort_keys=True).encode()).hexdigest()
        cache = output/'transcription-cache'/(cache_id+'.json')
        if cache.exists():
            previous = json.loads(cache.read_text())
            if previous.get('status') != 'failed' and Path(previous.get('wav_file', '')).is_file():
                results[item['message_id']] = previous
                continue
        try:
            started = time.monotonic()
            audio, duration, rms, wav = load_audio(item, source.parent, output)
            response = session.transcribe(audio, language=args.language, max_new_tokens=args.max_new_tokens)
            text = response.text.strip()
            flags = quality_flags(text, duration, rms, item.get('expected_seconds'), response.truncated)
            result = dict(config, audio_sha256=item['audio_sha256'], status='needs_review' if flags else 'machine_transcribed',
                          text=text, quality_flags=flags, duration_seconds=round(duration, 3),
                          wav_file=str(wav), rms=round(rms, 6), human_verified=False, confidence_score_available=False,
                          elapsed_seconds=round(time.monotonic()-started, 3))
        except Exception as error:
            result = dict(config, status='failed', text='', error_type=type(error).__name__, human_verified=False)
        save(cache, result)
        results[item['message_id']] = result
        if len(results) % 20 == 0:
            print(json.dumps(render(output, manifest, results, args.timezone)), flush=True)
    print(json.dumps(render(output, manifest, results, args.timezone)), flush=True)


if __name__ == '__main__':
    main()
