from pathlib import Path
import json

root = Path(r'C:\Project Rocket\output\feiyun_double_breakout')
out = root / 'delivery_20260930'
out.mkdir(exist_ok=True)
data = json.loads((root / 'understanding.json').read_text(encoding='utf-8'))
accept = json.loads((root / 'acceptance.json').read_text(encoding='utf-8'))
keys = ['03', '04', '08', '09', 'buypoints']
notes = {key: json.loads((root / f'notes_{key}.json').read_text(encoding='utf-8')) for key in keys}

def ts(value):
    n = int(value)
    return f'{n//3600:02}:{n//60%60:02}:{n%60:02}' if n >= 3600 else f'{n//60:02}:{n%60:02}'

def refs(items):
    return '；'.join(f'《{notes[k]["video"].removesuffix(".mkv")}》{ts(a)}–{ts(b)}（{label}）' for k,a,b,label in items)

lines = ['# 云聪五段视频：交易体系与 13 类买点理解', '',
    '整理：MrWu / Codex｜2026-09-30｜版本 1.0', '',
    '## 1. 阅读结论', '', data['thesis'], '', data['scope'], '',
    '**证据范围：**五段原片共 2 小时 24 分 47 秒；完整语音提取并阅读转写；对照 147 张一分钟样本和 22 张针对疑点补取的画面，共 169 张。这里的“完整”指语音处理覆盖全片，画面复核是抽样，不代表逐帧看完所有视频。转写仍可能有错词。', '',
    '**内容定位：**这是对指定课程的理解和可复核归纳。讲师的经验比例、个人盈利自述和图形解释未经过本项目统计验证；本轮没有生成收益、胜率或实盘结论。', '',
    '### 五节课怎样相互连接', '',
    '| 原片 | 时长 | 在体系中的作用 |', '| --- | --- | --- |']
roles = ['理解趋势线、运行惯性与结构改变', '用历史交易和量能解释关键价格区域', '区分逆转与持续，比较沽压空间及止损代价', '把开仓、止损、加仓、退出组织成系统', '把同一框架展开为 13 类具体入场情境']
for video, role in zip(accept['videos'], roles):
    lines.append(f'| {video["video"]} | {ts(video["duration_seconds"])} | {role} |')
lines += ['', '## 2. 体系的决策顺序', '']
for i, row in enumerate(data['flow'], 1):
    lines += [f'{i}. **{row["title"]}**：{row["text"]}']
lines += ['', '### 六个贯穿全课的要点', '']
for row in data['core']:
    lines += [f'#### {row["title"]}', '', row['text'], '', f'原片定位：{refs(row["refs"])}。', '']
lines += ['## 3. 13 类买点对照', '', data['pattern_note'], '',
    '| 编号 | 名称 | 背景 | 触发及确认 | 主要边界 | 原片定位 |', '| --- | --- | --- | --- | --- | --- |']
for row in data['patterns']:
    values = [str(row['number']), row['name'], row['context'], row['trigger'], row['boundary'], refs(row['refs'])]
    lines.append('| ' + ' | '.join(s.replace('|', '\\|').replace('\n', '<br>') for s in values) + ' |')
lines += ['', '### 如何把这些买点联系起来', '',
    '- **回调末端的早期信号**：抛售高潮、2B、假诱空、顺势头肩底、箱体弹簧。有些较早，有些要求拉回或颈线确认，不能混成同一个触发条件。',
    '- **吸筹后转强与边界突破**：强势出现、双突破、跳跃小溪。强势出现可能在箱体中轴附近，尚未突破箱顶；跳跃小溪强调跨过上方边界。',
    '- **突破后的确认和延续**：回抽确认、N 字突破、支撑压力互换、形态突破与多周期共振。旧边界回踩与重新越过前高是不同事件。',
    '- **特殊情境**：趋势第一次快速回落至选定上升波段约一半的位置。视频没有把波段端点、止损和确认都定义清楚，应单独保留，不能补成标准双突破。',
    '- 以上分组是本报告的理解归纳；课程并未要求每次走势都按这个顺序走完。', '',
    '## 4. 容易误读的地方', '']
for i, row in enumerate(data['distinctions'], 1):
    lines += [f'### 4.{i} {row["title"]}', '', row['text'], '', f'原片定位：{refs(row["refs"])}。', '']
lines += ['## 5. 五节课逐段笔记', '']
for key in keys:
    item = notes[key]
    lines += [f'### {item["video"]}', '', item['review'], '']
    for section in item['sections']:
        lines += [f'#### {ts(section["start"])}–{ts(section["end"])}｜{section["title"]}', '']
        lines += ['- ' + text for text in section['points']]
        lines += ['']
    lines += ['**本课保留的问题：**', ''] + ['- ' + text for text in item['open_points']] + ['']
lines += ['## 6. 尚不能从课程确定的规则', '']
for row in data['unresolved']:
    lines += [f'### {row["title"]}', '', row['text'], '', f'原片定位：{refs(row["refs"])}。', '']
lines += ['## 7. 在 MrWu 研究中怎样使用这份理解', '',
    '这份材料作为课程理解和后续规则定义的依据，不直接改写现有策略，也不把示例图当作回测样本。若进入程序化研究，应先独立冻结下列定义：', '',
    '1. 固定大、小周期及波段端点的因果识别方式，避免使用未来极值画线。',
    '2. 定义有效突破、收盘确认、放量、快速收回、回踩容差及最大等待时间。',
    '3. 逐个定义买点，不把 13 类形态混为一个信号，也不在看到结果后更换周期。',
    '4. 明确止损、加仓后总风险、分批比例的分母、手续费和滑点。',
    '5. 分开记录“讲师原意”“研究者操作化定义”“样本表现”，保留不一致及失效案例。',
    '6. 先用未暴露数据验证，才讨论是否有统计优势；本报告不提供未经验证的收益或概率。', '',
    '## 8. 识别与理解方法', '']
lines += ['- ' + s for s in data['method']]
lines += ['', '补充说明：分块覆盖验收证明音频区间被处理，不证明每个字都识别正确。云聪的口语、画线动作、图形、术语和修正须合起来解释；仅有转录不足以接受结论。', '',
    '## 9. 原片校验索引', '',
    '时间均为各自原片播放时间。文件名与 SHA-256 可用于辨认同一版本。原视频、完整逐字稿和抽帧图片不随本 Markdown 在公开 GitHub 发布。', '',
    '| 原片 | SHA-256 | 语音块 | 语音段 |', '| --- | --- | --- | --- |']
for row in accept['videos']:
    lines += [f'| {row["video"]} | `{row["source_sha256"]}` | {row["chunks"]} | {row["segments"]} |']
lines += ['', '逐项验收：五个源文件未改变；五个提取目录的分块覆盖和记录哈希通过；169 张画面有实际解码时间与原分辨率记录。详细提取和图像证据保存在本机原交付目录。', '',
    '## 10. 版本与相关交付', '',
    '- 本文：五课综合理解、13 类买点、逐段笔记和未决条件。',
    '- `本地音视频基建_共通化更新说明_20260930.md`：本次实际工程改进与验收。',
    '- `跨项目音视频识别调用指南.md`：其他项目接入共用包的方法。', '']
target = out / '云聪五段视频_完整理解总结_20260930.md'
target.write_text('\n'.join(lines), encoding='utf-8')
assert len(data['patterns']) == 13
assert len(accept['videos']) == 5 and accept['passed']
assert all(row['source_unchanged'] and row['audio_coverage_complete'] for row in accept['videos'])
assert not any(x in target.read_text(encoding='utf-8') for x in ['C:\\Users\\', 'C:\\Project Rocket'])
print(f'{target.name}: {target.stat().st_size} bytes, {len(lines)} lines')
