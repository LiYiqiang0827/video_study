#!/usr/bin/env python3
"""Build a same-name, same-folder video PPT from frozen reviewed analysis.

Compresses screenshot-linked stock/index clips, exports and validates the PPT,
and resumes by verifying receipts and hashes. No ASR or model calls.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from tool_paths import external_path

SCRIPTS = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def dump(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def run(command: list[str], log: Path) -> None:
    with log.open('w') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'{Path(command[1]).name} failed; inspect {log}')


def verified_receipt(path: Path, output_hash: str) -> dict:
    receipt = json.loads(path.read_text())
    if (receipt.get('finalSha256') != output_hash
            or receipt.get('packageIntegrity', {}).get('status') != 'pass'
            or receipt.get('presentationLayout', {}).get('exitCode') != 0
            or receipt.get('firstPartyImport', {}).get('passed') is not True):
        raise ValueError('PPT lacks a matching passed finalizer receipt')
    return receipt


def verify_cached_inputs(state: dict) -> None:
    """A closed PPT can only be reused with unchanged registered assets."""
    inputs = state['result'].get('inputs', {})
    for group in ('frames', 'videos'):
        assets = inputs.get(group)
        if not isinstance(assets, list):
            raise ValueError(f'Completed PPT lacks its {group} input ledger')
        for asset in assets:
            path = external_path(asset['path']).resolve(strict=True)
            if sha(path) != asset['sha256']:
                raise ValueError(f'Completed PPT input changed: {path}; use a new build directory')
    manifest = external_path(state['media_dir']) / 'media_manifest.json'
    if sha(manifest) != inputs.get('media_manifest_sha256'):
        raise ValueError('Completed media manifest changed; use a new build directory')


def build(args: argparse.Namespace) -> dict:
    analysis = external_path(args.analysis).resolve(strict=True)
    plan = external_path(args.plan).resolve(strict=True)
    content = json.loads(analysis.read_text())
    metadata = content['metadata']
    source = external_path(metadata['source_video_path']).resolve(strict=True)
    if analysis.parent.parent != source.parent:
        raise ValueError('Analysis must be in a direct child of the source video folder')
    if sha(source) != metadata['source_video_sha256']:
        raise ValueError('Source video differs from frozen analysis')
    transcript = external_path(content['source']['input_path']).resolve(strict=True)
    if sha(transcript) != content['source']['input_sha256']:
        raise ValueError('Source transcript differs from frozen analysis')
    output = external_path(args.output) if args.output else source.with_suffix('.pptx')
    if output.suffix.lower() != '.pptx':
        raise ValueError('Output must be a .pptx')
    workspace = external_path(args.build_dir)
    if workspace.is_relative_to(output.parent) or workspace.is_relative_to(source.parent):
        raise ValueError('Build directory must be outside the final video folder')
    node = args.node or os.environ.get('RUNTIME_NODE') or shutil.which('node')
    if not node:
        raise RuntimeError('Set RUNTIME_NODE or use --node')
    for name in ('RUNTIME_NODE_MODULES', 'PRESENTATIONS_SKILL_DIR', 'RUNTIME_PYTHON'):
        if not os.environ.get(name):
            raise RuntimeError(f'Missing environment {name}')
    contract = {'schema_version': 'yuncong_video_ppt_build_v1',
                'analysis_sha256': sha(analysis), 'plan_sha256': sha(plan),
                'source_video_sha256': sha(source), 'output': str(output),
                'options': {'duration': args.duration, 'crf': args.crf, 'fps': args.fps},
                'tool_sha256': {name: sha(SCRIPTS / name) for name in (
                    'build_video_ppt.py', 'prepare_ppt_video_clips.py',
                    'export_video_analysis_pptx.mjs', 'embed_ppt_videos.py')}}
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / '.build.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This PPT build is already running') from None
        state_path = workspace / 'STATUS.json'
        saved = json.loads(state_path.read_text()) if state_path.exists() else None
        if saved and saved.get('contract') != contract:
            raise ValueError('Build inputs/options changed; use a new build directory')
        if saved and saved.get('state') == 'complete':
            verify_cached_inputs(saved)
            if not output.is_file() or sha(output) != saved['result']['sha256']:
                raise ValueError('Completed PPT is missing or changed')
            receipt_path = Path(saved['validation_path'])
            if sha(receipt_path) != saved['validation_sha256']:
                raise ValueError('Completed validation receipt changed')
            verified_receipt(receipt_path, saved['result']['sha256'])
            return {**saved['result'], 'reused': True, 'model_calls': 0}
        if output.exists():
            raise FileExistsError('Existing PPT preserved; inspect its previous receipt or use --output')
        media_key = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()[:12]
        media_dir = analysis.parent / f'ppt_media_{media_key}'
        state = {'contract': contract, 'state': 'running', 'stage': 'compressing',
                 'media_dir': str(media_dir), 'model_calls': 0}
        dump(state_path, state)
        try:
            run([sys.executable, str(SCRIPTS / 'prepare_ppt_video_clips.py'),
                 '--analysis', str(analysis), '--plan', str(plan), '--output-dir', str(media_dir),
                 '--duration', str(args.duration), '--crf', str(args.crf), '--fps', str(args.fps)],
                workspace / 'compression.log')
            existing = list(workspace.glob('export_attempt_*'))
            attempt = workspace / f'export_attempt_{len(existing) + 1:03}'
            state.update(stage='exporting', attempt_dir=str(attempt))
            dump(state_path, state)
            run([node, str(SCRIPTS / 'export_video_analysis_pptx.mjs'),
                 '--analysis', str(analysis), '--plan', str(plan),
                 '--media-manifest', str(media_dir / 'media_manifest.json'),
                 '--output', str(output), '--build-dir', str(attempt)], workspace / 'export.log')
            result = json.loads((attempt / 'EXPORT_RESULT.json').read_text())
            if result.get('state') != 'exported' or result['sha256'] != sha(output):
                raise ValueError('Final PPT does not match the export receipt')
            if contract['analysis_sha256'] != sha(analysis) or contract['plan_sha256'] != sha(plan):
                raise ValueError('Reviewed inputs changed during build')
            receipt_path = attempt / 'VALIDATION.json'
            verified_receipt(receipt_path, result['sha256'])
            state.update(state='complete', stage='complete', result=result,
                         validation_path=str(receipt_path), validation_sha256=sha(receipt_path))
            dump(state_path, state)
            return {**result, 'reused': False, 'model_calls': 0}
        except Exception as error:
            state.update(state='failed', error=str(error))
            dump(state_path, state)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', required=True)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--build-dir', required=True, help='External work directory, outside the video folder')
    parser.add_argument('--output', help='Default: source MP4 filename with .pptx, in the same folder')
    parser.add_argument('--node', help='Default: RUNTIME_NODE or installed node')
    parser.add_argument('--duration', type=float, default=32)
    parser.add_argument('--crf', type=int, default=28)
    parser.add_argument('--fps', type=float, default=15)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'state': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
