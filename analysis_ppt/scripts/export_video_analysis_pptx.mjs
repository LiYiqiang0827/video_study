#!/usr/bin/env node
/** Export an audited, concise video analysis as an offline editable PPTX.
 * No inference, network, media generation, or source-file modifications.
 * Artifact Tool and validation runtimes remain outside this checkout.
 */
import fs from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const { values: args } = parseArgs({ options: {
  analysis: { type: 'string' }, plan: { type: 'string' }, output: { type: 'string' },
  'build-dir': { type: 'string' }, font: { type: 'string', default: 'PingFang SC' },
  'media-manifest': { type: 'string' },
  help: { type: 'boolean', default: false },
}});
if (args.help) {
  console.log('Usage: export_video_analysis_pptx.mjs --analysis analysis.json --plan ppt_plan.json --output result.pptx --build-dir EXTERNAL_DIR [--font "PingFang SC"] [--media-manifest media_manifest.json]\nEnvironment: RUNTIME_NODE_MODULES, PRESENTATIONS_SKILL_DIR, RUNTIME_PYTHON');
  process.exit(0);
}

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const inside = (file, dir) => {
  const relative = path.relative(dir, file);
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`));
};
const toolRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const projectRoot = path.resolve(toolRoot, '..');
const tempRoot = path.join(projectRoot, 'tmp');
const clock = seconds => {
  const n = Math.floor(seconds);
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(v => String(v).padStart(2, '0')).join(':');
};
const cleanTime = text => String(text ?? '').replace(/[（(][^）)]*(?:时钟映射|误差区间|不确定性)[^）)]*[）)]/g, '').trim();
const readingTime = value => {
  const cleaned = cleanTime(value);
  return /未确认|未知|不确定/.test(cleaned) ? '' : cleaned;
};
const colours = { ink: '#17334B', muted: '#52687A', paper: '#FBFAF7', watch: '#9A6B18', buy_plan: '#B24838', no_buy: '#5D6570', bearish: '#5D6570', hold: '#9A6B18', sell: '#5D6570' };
const originTitles = { self: '云聪主动分析的股票', viewer: '网友提问的股票', both: '主动分析与网友提问', unknown: '其他提及的股票' };
const labels = { buy_plan: '买入计划', watch: '关注', no_buy: '不买', bearish: '不看好', hold: '持有', sell: '卖出计划' };
const displayName = entity => entity.visual_identity?.visual_verified === true
  ? entity.visual_identity.display_name || entity.name : entity.name;
const requireText = (value, field, max) => {
  if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error(`Invalid or overly long ${field}`);
  if (/注意与不确定性|时钟映射误差|误差区间|\[字幕|字幕\s*\d+|未核验/.test(value)) throw new Error(`Audit annotation in reading text: ${field}`);
};

async function main() {
  for (const key of ['analysis', 'plan', 'output', 'build-dir']) if (!args[key]) throw new Error(`Missing --${key}`);
  for (const key of ['RUNTIME_NODE_MODULES', 'PRESENTATIONS_SKILL_DIR', 'RUNTIME_PYTHON']) if (!process.env[key]) throw new Error(`Missing environment ${key}`);
  const analysisPath = await fs.realpath(args.analysis), planPath = await fs.realpath(args.plan);
  const output = path.resolve(args.output), build = path.resolve(args['build-dir']);
  if (!output.endsWith('.pptx')) throw new Error('Output must be a .pptx');
  if (inside(output, projectRoot) && !inside(output, tempRoot)) throw new Error('Final output inside the checkout is allowed only under project tmp/');
  if (inside(build, projectRoot) && !inside(build, tempRoot)) throw new Error('Build output inside the checkout is allowed only under project tmp/');
  try { await fs.lstat(output); throw new Error('Existing output preserved; use a new filename'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  await fs.mkdir(build, { recursive: true }); await fs.mkdir(path.dirname(output), { recursive: true });
  const buildReal = await fs.realpath(build), parentReal = await fs.realpath(path.dirname(output));
  if ((inside(buildReal, projectRoot) && !inside(buildReal, tempRoot)) ||
      (inside(parentReal, projectRoot) && !inside(parentReal, tempRoot))) throw new Error('Output symlink points into tools checkout');
  if (inside(buildReal, parentReal)) throw new Error('The private build directory must be outside the final output directory');
  const candidate = path.join(buildReal, 'candidate.pptx');
  try { await fs.lstat(candidate); throw new Error('Existing build preserved; use a new build directory'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const analysisBytes = await fs.readFile(analysisPath), planBytes = await fs.readFile(planPath);
  const analysis = JSON.parse(analysisBytes), plan = JSON.parse(planBytes);
  if (plan.schema_version !== 'yuncong_ppt_plan_v1' || plan.analysis_sha256 !== sha(analysisBytes)) throw new Error('PPT plan does not match the frozen analysis');
  requireText(plan.title, 'title', 45);
  for (const key of ['market', 'self_analysis', 'viewer_questions', 'focus']) requireText(plan.overview?.[key], `overview.${key}`, 65);
  const entities = new Map(analysis.entities.map(e => [e.id, e]));
  if (!Array.isArray(plan.stocks) || (plan.market_items !== undefined && !Array.isArray(plan.market_items))) throw new Error('Expected stocks and optional market_items arrays');
  const marketItems = plan.market_items ?? [];
  const items = [...marketItems, ...plan.stocks];
  const seen = new Set(), frameInputs = [], videoInputs = [], embeddingItems = [];
  let mediaBytes, mediaPath, media;
  if (args['media-manifest']) {
    mediaPath = await fs.realpath(args['media-manifest']);
    mediaBytes = await fs.readFile(mediaPath); media = JSON.parse(mediaBytes);
    if (media.schema_version !== 'yuncong_ppt_media_v1' || media.analysis_sha256 !== sha(analysisBytes) || media.plan_sha256 !== sha(planBytes) || media.source_video_sha256 !== analysis.metadata.source_video_sha256 || !Array.isArray(media.clips)) throw new Error('Media manifest must match the frozen source, analysis and plan');
  }
  for (const stock of items) {
    const entity = entities.get(stock.entity_id);
    const isMarket = marketItems.includes(stock);
    if (!entity || seen.has(stock.entity_id) || (isMarket ? !['index', 'market'].includes(entity.kind) : entity.kind !== 'stock')) throw new Error('Invalid, repeated or misclassified presentation entity');
    seen.add(stock.entity_id);
    if ((!isMarket && !originTitles[stock.origin]) || !labels[stock.attitude]) throw new Error('Invalid origin or attitude enum');
    requireText(stock.label, 'stock.label', 20); requireText(stock.outline_sentence, 'stock.outline_sentence', 58);
    if (!Array.isArray(stock.details) || !stock.details.length) throw new Error('Missing stock detail');
    const frames = entity.segments.flatMap(s => (s.frames ?? []).map(f => ({ ...f, segment_id: s.id })));
    stock.entity = entity;
    for (const detail of stock.details) {
      requireText(detail.title, 'detail.title', 28); requireText(detail.caption, 'detail.caption', 45);
      if (!Array.isArray(detail.points) || detail.points.length < 2 || detail.points.length > 4) throw new Error('Use 2–4 detail points');
      for (const point of detail.points) { requireText(point.heading, 'point.heading', 9); requireText(point.text, 'point.text', 58); }
      const selected = frames.find(f => f.path === detail.frame_path);
      if (!selected) throw new Error(`Image is not registered for ${entity.name}`);
      const visibleName = selected.visible_entity_name || selected.visible_stock_name;
      if (visibleName && ![entity.name, displayName(entity)].includes(visibleName)) throw new Error('Selected screenshot displays a different entity');
      if (isMarket && (!selected.visual_verified || !selected.chart_verified)) throw new Error('Market detail needs a visually verified chart frame');
      const framePath = await fs.realpath(path.resolve(path.dirname(analysisPath), selected.path));
      if (!inside(framePath, path.dirname(analysisPath))) throw new Error('Screenshot must be inside the analysis directory');
      const bytes = await fs.readFile(framePath);
      if (!['.jpg', '.jpeg', '.png'].includes(path.extname(framePath).toLowerCase())) throw new Error('Unsupported screenshot type');
      if (detail.crop) {
        for (const key of ['left', 'top', 'right', 'bottom']) if (!Number.isFinite(detail.crop[key]) || detail.crop[key] < 0 || detail.crop[key] >= 1) throw new Error('Invalid crop');
        if (detail.crop.left + detail.crop.right >= 1 || detail.crop.top + detail.crop.bottom >= 1) throw new Error('Empty image crop');
      }
      detail.sourceFrame = selected; detail.imageBytes = bytes;
      frameInputs.push({ path: framePath, sha256: sha(bytes) });
      if (detail.embed_video !== undefined && typeof detail.embed_video !== 'boolean') throw new Error('embed_video must be a boolean');
      if (detail.embed_video === false && (typeof detail.video_omission_reason !== 'string' || !detail.video_omission_reason.trim())) throw new Error('Screenshot-only detail needs a recorded video omission reason');
      if (media && detail.embed_video === false) {
        const omissions = (media.omissions ?? []).filter(c => c.entity_id === entity.id && c.frame_path === detail.frame_path);
        if (omissions.length !== 1 || omissions[0].frame_sha256 !== sha(bytes) || omissions[0].reason !== detail.video_omission_reason) throw new Error('Screenshot-only detail lacks matching omission provenance');
      } else if (media) {
        const clips = media.clips.filter(c => c.entity_id === entity.id && c.frame_path === detail.frame_path);
        if (clips.length !== 1) throw new Error('Each detail needs exactly one registered video');
        const clip = clips[0], segment = entity.segments.find(s => s.id === selected.segment_id);
        if (clip.validation?.state !== 'passed' || clip.validation?.complete_decode !== true || clip.frame_sha256 !== sha(bytes)) throw new Error('Video lacks verified decoding and poster provenance');
        if (clip.segment_id !== segment.id || !Number.isFinite(clip.start_seconds) || !Number.isFinite(clip.end_seconds) || clip.end_seconds <= clip.start_seconds || selected.video_seconds < clip.start_seconds || selected.video_seconds > clip.end_seconds) throw new Error('Video range does not match the registered screenshot');
        if (clip.start_seconds < segment.start_seconds - 0.05 || clip.end_seconds > segment.end_seconds + 0.05) throw new Error('Video range crosses its source segment');
        if (JSON.stringify(clip.reviewed_bounds ?? null) !== JSON.stringify(segment.ppt_clip_bounds ?? null)) throw new Error('Video reviewed bounds differ from analysis');
        if (segment.ppt_clip_bounds && (clip.start_seconds < segment.ppt_clip_bounds.start_seconds || clip.end_seconds > segment.ppt_clip_bounds.end_seconds)) throw new Error('Video range crosses its visually reviewed bounds');
        const videoPath = await fs.realpath(path.resolve(path.dirname(analysisPath), clip.path));
        const temporaryCache = mediaPath && inside(mediaPath, tempRoot) && inside(videoPath, path.dirname(mediaPath));
        if ((!inside(videoPath, path.dirname(analysisPath)) && !temporaryCache) || path.extname(videoPath).toLowerCase() !== '.mp4') throw new Error('Video must be a registered MP4 in the analysis folder or its project tmp media cache');
        const clipBytes = await fs.readFile(videoPath);
        if (sha(clipBytes) !== clip.sha256) throw new Error('Video hash differs from the verified manifest');
        detail.video = { ...clip, path: videoPath, poster_sha256: sha(bytes) };
        videoInputs.push({ path: videoPath, sha256: clip.sha256 });
      }
    }
  }
  for (const entity of entities.values()) {
    const required = entity.kind === 'stock' || (['index', 'market'].includes(entity.kind) && entity.segments.some(s => (s.frames ?? []).some(f => f.visual_verified && f.chart_verified)));
    if (required && !seen.has(entity.id)) throw new Error(`PPT lacks a discussed stock or verified market chart: ${entity.name}`);
  }
  const moduleRoot = process.env.RUNTIME_NODE_MODULES, skillDir = process.env.PRESENTATIONS_SKILL_DIR;
  const { Presentation, PresentationFile } = await import(pathToFileURL(path.join(moduleRoot, '@oai/artifact-tool/dist/artifact_tool.mjs')).href);
  const { FontLibrary } = await import(pathToFileURL(path.join(moduleRoot, '@oai/artifact-tool/node_modules/skia-canvas/lib/index.mjs')).href);
  if (!FontLibrary.families.includes(args.font)) throw new Error(`Font is unavailable: ${args.font}`);
  const { finalizePresentation } = await import(pathToFileURL(path.join(skillDir, 'container_tools/artifact_tool_utils.mjs')).href);
  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  const outlineOwners = [];
  const text = (slide, value, x, y, w, h, size = 28, bold = false, color = colours.ink) => {
    const box = slide.shapes.add({ geometry: 'textbox', position: { left: x, top: y, width: w, height: h }, fill: 'none', line: { fill: 'none', width: 0 } });
    box.text = value; box.text.style = { typeface: args.font, fontSize: size, bold, color, insets: 0, autoFit: 'none', wrap: 'square', verticalAlignment: 'top' };
    return box;
  };
  const page = title => {
    const slide = presentation.slides.add(); slide.background.fill = colours.paper;
    text(slide, title, 56, 38, 1168, 68, 44, true);
    text(slide, String(presentation.slides.items.length).padStart(2, '0'), 1180, 680, 44, 24, 16, false, colours.muted);
    return slide;
  };
  const note = (slide, detail) => {
    slide.speakerNotes.text = `来源：${analysisPath}\n原视频：${analysis.metadata.source_filename}\n${detail ? `截图：${detail.frame_path}\n视频 ${clock(detail.sourceFrame.video_seconds)}\n北京时间 ${cleanTime(detail.sourceFrame.beijing_time_text)}` : '内容：云聪观点的精简整理'}`;
  };
  const cover = page('云聪视频观点');
  text(cover, plan.title, 56, 170, 1150, 180, 62, true);
  text(cover, plan.overview.focus, 56, 396, 1080, 86, 32);
  const recordingTime = analysis.alignment?.timezone_confirmed === true ? readingTime(analysis.metadata.recording_time) : '';
  const visibleDates = [...new Set(analysis.entities.flatMap(e => e.segments.flatMap(s => (s.frames ?? [])
    .filter(f => f.desktop_clock_verified === true && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(f.desktop_time_text ?? ''))
    .map(f => f.desktop_time_text.slice(0, 10)))))].sort();
  const coverTime = recordingTime ? `录制北京时间  ${recordingTime}` : visibleDates.length
    ? `桌面录制日期  ${visibleDates.length === 1 ? visibleDates[0] : `${visibleDates[0]} 至 ${visibleDates.at(-1)}`}` : '';
  if (coverTime) text(cover, coverTime, 56, 564, 1150, 52, 23, false, colours.muted);
  note(cover);
  const overview = page('本期大纲');
  const sections = [['大盘整体看法', plan.overview.market]];
  if (plan.stocks.some(s => ['self', 'both'].includes(s.origin))) sections.push(['云聪主动分析', plan.overview.self_analysis]);
  if (plan.stocks.some(s => ['viewer', 'both'].includes(s.origin))) sections.push(['网友提问', plan.overview.viewer_questions]);
  sections.forEach(([title, body], i) => {
    text(overview, title, 56, 160 + i * 155, 1080, 42, 32, true);
    text(overview, body, 56, 214 + i * 155, 1140, 74, 28);
  });
  note(overview);
  for (const [origin, title] of Object.entries(originTitles)) {
    const stocks = plan.stocks.filter(s => s.origin === origin);
    for (let offset = 0; offset < stocks.length; offset += 6) {
      const rows = stocks.slice(offset, offset + 6), slide = page(title);
      const table = slide.tables.add({ rows: rows.length + 1, columns: 3, left: 56, top: 148, width: 1168, height: 488,
        columnWidths: [192, 225, 751], values: [['股票', '云聪态度', '一句话概括'], ...rows.map(s => [displayName(s.entity), s.label, s.outline_sentence])] });
      table.borders.assign({ style: 'solid', fill: '#DDD9D0', width: 0.6 });
      for (let row = 0; row < rows.length + 1; row++) {
        table.rows[row].height = row === 0 ? 48 : 72;
        for (let col = 0; col < 3; col++) {
          const cell = table.getCell(row, col); cell.fill = row === 0 ? '#EAE6DC' : colours.paper;
          cell.text.style = { typeface: args.font, fontSize: 24, color: row > 0 && col === 1 ? colours[rows[row - 1].attitude] : colours.ink, bold: row === 0 || col === 0, verticalAlignment: 'middle', insets: 12, autoFit: 'none' };
        }
      }
      outlineOwners.push(presentation.slides.items.length); note(slide);
    }
  }
  for (const stock of items) for (const detail of stock.details) {
    const slide = page(detail.title);
    text(slide, stock.label, 56, 110, 1168, 36, 27, true, colours[stock.attitude]);
    slide.images.add({ blob: detail.imageBytes, contentType: detail.frame_path.endsWith('.png') ? 'image/png' : 'image/jpeg', alt: detail.caption,
      fit: 'contain', ...(detail.crop ? { crop: detail.crop } : {}), position: { left: 56, top: 157, width: 815, height: 476 } });
    let y = 176;
    for (const point of detail.points) {
      text(slide, point.heading, 910, y, 310, 34, 25, true);
      const lines = Math.ceil(point.text.length / 12), height = Math.max(65, lines * 31);
      text(slide, point.text, 910, y + 42, 310, height, 24);
      y += 42 + height + 20;
    }
    if (y > 670) throw new Error(`Too much slide text: ${detail.title}; shorten before reducing fonts`);
    text(slide, detail.video ? `点击画面播放原讲解（${Math.round(detail.video.end_seconds - detail.video.start_seconds)}秒）` : detail.caption, 56, 643, 815, 33, 22);
    const frame = detail.sourceFrame;
    const confirmedZone = frame.desktop_timezone_confirmed === true || analysis.alignment?.timezone_confirmed === true;
    const clockLabel = confirmedZone ? '北京时间' : '桌面时间';
    const frameTime = frame.desktop_clock_verified === true && !confirmedZone
      ? readingTime(frame.desktop_time_text) : confirmedZone ? readingTime(frame.beijing_time_text) : '';
    text(slide, `视频 ${clock(frame.video_seconds)}${frameTime ? `    ${clockLabel} ${frameTime}` : ''}`, 56, 682, 1100, 23, 16, false, colours.muted);
    note(slide, detail);
    if (detail.video) {
      embeddingItems.push({ slide: presentation.slides.items.length, entity_id: stock.entity_id, ...detail.video });
      slide.speakerNotes.text += `\n内嵌原讲解：视频 ${clock(detail.video.start_seconds)} 至 ${clock(detail.video.end_seconds)}`;
    }
  }
  for (let i = 0; i < presentation.slides.items.length; i++) {
    const slide = presentation.slides.items[i], blob = await presentation.export({ slide, format: 'png', scale: 1 });
    await fs.writeFile(path.join(buildReal, `slide-${String(i + 1).padStart(2, '0')}.png`), new Uint8Array(await blob.arrayBuffer()));
    const layout = await slide.export({ format: 'layout' }); await fs.writeFile(path.join(buildReal, `slide-${String(i + 1).padStart(2, '0')}.layout.json`), await layout.text());
  }
  const montage = await presentation.export({ format: 'webp', montage: true });
  await fs.writeFile(path.join(buildReal, 'montage.webp'), new Uint8Array(await montage.arrayBuffer()));
  await (await PresentationFile.exportPptx(presentation)).save(candidate);
  if (sha(await fs.readFile(analysisPath)) !== sha(analysisBytes) || sha(await fs.readFile(planPath)) !== sha(planBytes)) throw new Error('Input changed during export');
  for (const frame of frameInputs) if (sha(await fs.readFile(frame.path)) !== frame.sha256) throw new Error('Screenshot changed during export');
  for (const video of videoInputs) if (sha(await fs.readFile(video.path)) !== video.sha256) throw new Error('Video changed during export');
  if (mediaPath && sha(await fs.readFile(mediaPath)) !== sha(mediaBytes)) throw new Error('Media manifest changed during export');
  let candidatePath = candidate;
  if (embeddingItems.length) {
    const specPath = path.join(buildReal, 'embedding_spec.json');
    await fs.writeFile(specPath, JSON.stringify({ schema_version: 'yuncong_ppt_embedding_v1', input_pptx_sha256: sha(await fs.readFile(candidate)), videos: embeddingItems }, null, 2) + '\n');
    candidatePath = path.join(buildReal, 'candidate_embedded.pptx');
    await promisify(execFile)(process.env.RUNTIME_PYTHON, [path.join(toolRoot, 'scripts/embed_ppt_videos.py'), '--input', candidate, '--spec', specPath, '--output', candidatePath, '--receipt', path.join(buildReal, 'MEDIA_EMBEDDING.json')], { maxBuffer: 1048576 });
  }
  const stagedFinalDir = path.join(buildReal, 'finalized');
  await fs.mkdir(stagedFinalDir, { recursive: true });
  const stagedFinal = path.join(stagedFinalDir, 'finalized.pptx');
  await finalizePresentation({ workspaceDir: buildReal, candidatePath, finalPath: stagedFinal, pythonExecutable: process.env.RUNTIME_PYTHON,
    integrityValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_package_integrity.py'),
    layoutValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_layout_geometry.py'),
    layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-heading-fit', ...outlineOwners.flatMap(n => ['--require-native-table-slide', String(n)])],
    requiredNativeTableOwnerSlides: outlineOwners, fontPolicy: { basis: 'design', families: [args.font] },
    verifyArtifactToolImport: true, receiptPath: path.join(buildReal, 'VALIDATION.json') });
  try {
    await fs.link(stagedFinal, output);
    await fs.unlink(stagedFinal);
  } catch (error) {
    if (error.code !== 'EXDEV') throw error;
    await fs.copyFile(stagedFinal, output, fsConstants.COPYFILE_EXCL);
  }
  const result = { state: 'exported', output, slides: presentation.slides.items.length, stocks: plan.stocks.length, market_items: marketItems.length,
    screenshot_only_details: items.flatMap(item => item.details).filter(detail => detail.embed_video === false).length,
    sha256: sha(await fs.readFile(output)), embedded_videos: embeddingItems.length, inputs: { analysis_sha256: sha(analysisBytes), plan_sha256: sha(planBytes), frames: frameInputs, videos: videoInputs, ...(mediaPath ? { media_manifest_sha256: sha(mediaBytes) } : {}) }, model_calls: 0 };
  await fs.writeFile(path.join(buildReal, 'EXPORT_RESULT.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
}
main().catch(error => { console.error(JSON.stringify({ state: 'failed', error: error.message })); process.exitCode = 1; });
