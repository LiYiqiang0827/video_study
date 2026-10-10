from pathlib import Path
import hashlib
import json
import shutil
import zipfile

root = Path(r'C:\Users\HarryWu_IOKI\Documents\SharedTools\media-platform')
legacy = Path(r'C:\Users\HarryWu_IOKI\Documents\推し活\media_platform')
delivery = Path(r'C:\Project Rocket\output\feiyun_double_breakout\delivery_20260930')
repo = Path(r'C:\Project Rocket\worktrees\yuncong-media-docs')
target = repo / 'docs/research/mrwu_yuncong_media_20260930'
target.mkdir(parents=True, exist_ok=True)
smoke = json.loads((delivery.parent / 'portable_gpu_smoke_20260930/manifest.json').read_text(encoding='utf-8'))
assert smoke['steps']['audio'] == 'complete' and smoke['steps']['evidence'] == 'complete'
assert smoke['params']['device'] == 'cuda' and smoke['params']['compute_type'] == 'float16'
acceptance = {
    'version': '2.1.0', 'date': '2026-09-30', 'platform': 'Windows / Python 3.12',
    'tests': {'frame_review': 3, 'portable_install_and_config': 2, 'legacy_platform': 6, 'all_passed': True},
    'gpu_smoke': {'source_sha256': smoke['source_sha256'], 'start': 169, 'end': 189,
        'model': 'large-v3', 'device': 'cuda', 'compute_type': 'float16', 'segments': 13,
        'source_and_evidence_verified': True},
    'full_course_previous_acceptance': {'videos': 5, 'frames': 169, 'passed': True},
    'limitations': ['Windows only tested', 'no semantic-accuracy guarantee', 'no generic speaker diarization', 'large-v3 revision not pinned']
}
(root / 'ACCEPTANCE_20260930.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding='utf-8')
(root / 'compat').mkdir(exist_ok=True)
shutil.copy2(legacy / '__init__.py', root / 'compat/legacy_package_init.py')
guide_name = '跨项目音视频识别调用指南.md'
shutil.copy2(root / 'README.md', delivery / guide_name)
old_guide = delivery / 'legacy_guide_before_20260930.md'
if not old_guide.exists():
    shutil.copy2(legacy / guide_name, old_guide)
shutil.copy2(delivery / guide_name, legacy / guide_name)
intro = '''> 2026-09-30 共通化更新：通用核心已发布并在本机安装为 `local-media-platform 2.1.0`。
> 新项目请看 [跨项目音视频识别调用指南](跨项目音视频识别调用指南.md)，直接使用 `python -m media_platform`，无需注入本项目路径。
> 中立源码位于用户 Documents 的 `SharedTools/media-platform`；原有业务工作流仍按本文使用。
> Google Drive 共通交付：https://drive.google.com/drive/folders/1vk4nIGxQoSZtqCP-dNqpti7fqwnbgRNg

'''
readme = (legacy / 'README.md').read_text(encoding='utf-8')
if not readme.startswith('> 2026-09-30 共通化更新'):
    (legacy / 'README.md').write_text(intro + readme, encoding='utf-8')
wheel = root / 'dist/local_media_platform-2.1.0-py3-none-any.whl'
shutil.copy2(wheel, delivery / wheel.name)
update_name = '本地音视频基建_共通化更新说明_20260930.md'
shutil.copy2(delivery / update_name, root / 'CHANGELOG_20260930.md')
files = [root/'pyproject.toml', root/'README.md', root/'CHANGELOG_20260930.md', root/'ACCEPTANCE_20260930.json']
files += sorted((root/'src/media_platform').glob('*.py'))
files += sorted((root/'tests').glob('*.py'))
files += sorted((root/'compat').glob('*.py'))
archive = delivery / 'local-media-platform-2.1.0-source.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for file in files:
        z.write(file, 'local-media-platform-2.1.0/' + file.relative_to(root).as_posix())
for name in ['云聪五段视频_完整理解总结_20260930.md', update_name, guide_name, wheel.name]:
    shutil.copy2(delivery / name, target / name)
source_target = target / 'shared_media_platform'
for file in files:
    destination = source_target / file.relative_to(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file, destination)
(target / 'README.md').write_text('''# MrWu｜云聪视频理解与共通音视频基建

日期：2026-09-30。此目录是 MrWu 部分的新交付，保存指定课程的理解与共通工具发布；没有改写现有交易策略。

## 阅读入口

1. [云聪五段视频完整理解总结](云聪五段视频_完整理解总结_20260930.md)：体系、13 类买点、逐段笔记、原片时间及未定义条件。
2. [基建改进与验收](本地音视频基建_共通化更新说明_20260930.md)：实际改动、已有能力、11 项测试和真实 GPU 验证。
3. [跨项目调用指南](跨项目音视频识别调用指南.md)：安装、配置、提取、画面复核与证据读取。
4. [共用源码](shared_media_platform/)：可独立安装，含构建配置、核心、测试和兼容桥示例。
5. [安装包](local_media_platform-2.1.0-py3-none-any.whl)：Python wheel，依赖按需安装。

## Google Drive

- [课程理解交付目录](https://drive.google.com/drive/folders/1zVOP1LtxWf4HQjA4Eynawlktge9F2eUc)
- [跨项目共通音视频基建](https://drive.google.com/drive/folders/1vk4nIGxQoSZtqCP-dNqpti7fqwnbgRNg)

## 安装

本目录下执行：

```powershell
python -m pip install ".\\local_media_platform-2.1.0-py3-none-any.whl[asr,ocr,gpu]"
python -m media_platform doctor
```

也可以进入 `shared_media_platform` 后 `python -m pip install ".[asr,ocr,gpu]"`。FFmpeg/ffprobe 需在 PATH。共用包并未发布到 PyPI。

## 范围

课程图形和经验判断未被统计回测。本目录不附原视频、完整逐字稿、截图、个人互动材料、模型权重或认证信息。原素材以总结中的文件名、SHA-256 和时间定位。`SHA256SUMS.json` 校验交付文件。
''', encoding='utf-8')
def hashes(folder, paths):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
public_files = list(target.rglob('*'))
attributes = target / '.gitattributes'
attributes.write_bytes(b'* -text\n')
public_files.append(attributes) if attributes not in public_files else None
public_files = [p for p in public_files if p.is_file() and p.name != 'SHA256SUMS.json']
for p in public_files:
    if p.suffix in ('.py', '.md', '.toml', '.json'):
        s = p.read_text(encoding='utf-8')
        assert 'C:\\Users\\HarryWu_IOKI' not in s, p
        assert 'C:\\Project Rocket' not in s, p
(target / 'SHA256SUMS.json').write_text(json.dumps(hashes(target, public_files), ensure_ascii=False, indent=2), encoding='utf-8')
selected = [delivery/n for n in ['云聪五段视频_完整理解总结_20260930.md', update_name, guide_name, wheel.name, archive.name]]
(delivery / 'SHA256SUMS.json').write_text(json.dumps(hashes(delivery, selected), ensure_ascii=False, indent=2), encoding='utf-8')
shutil.copy2(root / 'ACCEPTANCE_20260930.json', delivery / 'ACCEPTANCE_20260930.json')
infra = [delivery/n for n in [update_name, guide_name, wheel.name, archive.name, 'ACCEPTANCE_20260930.json']]
(delivery / '共通基建_SHA256SUMS.json').write_text(json.dumps(hashes(delivery, infra), ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'public_files':len(public_files)+1, 'zip_files':len(files), 'zip_bytes':archive.stat().st_size, 'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest()}, indent=2))
