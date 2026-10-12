import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const [archiveDir, workspaceDir, skillDir, runtimePython] = process.argv.slice(2);
if (!archiveDir || !workspaceDir || !skillDir || !runtimePython) {
  throw new Error("usage: generate_monthly_posts_ppt.mjs ARCHIVE_MONTH_DIR WORKSPACE_DIR SKILL_DIR RUNTIME_PYTHON");
}

const archive = path.resolve(archiveDir);
const workspace = path.resolve(workspaceDir);
const month = path.basename(archive);
const revision = new Date().toISOString().replace(/[-:.TZ]/g, "");
const input = JSON.parse(await fs.readFile(path.join(archive, "posts.json"), "utf8"));
const posts = input.posts ?? [];
const unresolvedMonth = month === "未识别日期";
function inferredOlderBoundary() {
  const reference = input.month_assignment_reference;
  const date = reference ? new Date(reference) : null;
  if (!date || Number.isNaN(date.valueOf())) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
  }).formatToParts(date).map(part => [part.type, part.value]));
  return { year: Number(parts.year) - 1, month: Number(parts.month) };
}
const olderBoundary = unresolvedMonth ? inferredOlderBoundary() : null;
const olderBoundaryLabel = olderBoundary
  ? `约 ${olderBoundary.year}年${olderBoundary.month}月以前（推断）`
  : "较早年份（推断；具体范围未确认）";
const outputDir = path.join(workspace, "tmp", "youtube_posts", "monthly_ppt");
const buildDir = path.join(outputDir, "build", `${month}_${revision}`);
const finalDir = path.join(outputDir, "final", revision);
const finalName = unresolvedMonth
  ? `云聪帖子_${olderBoundary ? `约${olderBoundary.year}年${olderBoundary.month}月以前（推断）` : "较早年份（推断）"}_${revision}.pptx`
  : `云聪帖子_${month}_${revision}.pptx`;
const finalPath = path.join(finalDir, finalName);
await fs.mkdir(buildDir, { recursive: true });
await fs.mkdir(finalDir, { recursive: true });

const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const fontFamily = "PingFang SC";
const colors = { ink: "#182536", muted: "#657386", accent: "#167D8D", paper: "#FBFAF7", rule: "#D9E0E5" };

function textbox(slide, text, position, style = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox", position, fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = { typeface: fontFamily, fontSize: 24, color: colors.ink, ...style };
  return shape;
}

function addRule(slide, top) {
  slide.shapes.add({
    geometry: "rect", position: { left: 70, top, width: 1140, height: 2 },
    fill: colors.rule, line: { style: "solid", fill: "none", width: 0 },
  });
}

function displayDate(post) {
  const raw = post.published_time_raw || "发布时间未标注";
  if (unresolvedMonth) {
    return `页面显示：${raw}\n归档范围：${olderBoundaryLabel}\n精确发帖日期：YouTube 未提供`;
  }
  if (post.published_timestamp) {
    const date = new Date(Number(post.published_timestamp) / 1000);
    if (!Number.isNaN(date.valueOf())) {
      return `精确发布时间：${date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}（北京时间）\n页面显示：${raw}`;
    }
  }
  if (post.estimated_published_at_start && post.estimated_published_at_end) {
    const format = (value) => new Date(value).toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    const archiveLabel = post.archive_month_precision === "estimated_from_relative_display"
      ? `归档月份：${month}（估算）`
      : "归档月份：未能确认";
    return `页面显示：${raw}\n推算时间范围：${format(post.estimated_published_at_start)} 至 ${format(post.estimated_published_at_end)}\n精确日期未提供；${archiveLabel}`;
  }
  const archiveLabel = post.archive_month_precision === "estimated_from_relative_display"
    ? `归档月份：${month}（估算）`
    : "归档月份：未能确认";
  return `页面显示：${raw}\n精确发帖日期：YouTube 未提供\n${archiveLabel}`;
}

function splitText(text, maxLines = 11) {
  const normalized = String(text || "（无文字正文）").replace(/\r\n/g, "\n");
  const visualLines = [];
  for (const sourceLine of normalized.split("\n")) {
    let remaining = Array.from(sourceLine);
    if (!remaining.length) {
      visualLines.push("");
      continue;
    }
    while (remaining.length > 24) {
      const window = remaining.slice(0, 24).join("");
      const punctuation = Math.max(
        window.lastIndexOf("。"), window.lastIndexOf("！"), window.lastIndexOf("？"),
        window.lastIndexOf("；"), window.lastIndexOf("，"), window.lastIndexOf(" "),
      );
      const cut = punctuation >= 12 ? punctuation + 1 : 24;
      visualLines.push(remaining.slice(0, cut).join("").trimEnd());
      remaining = remaining.slice(cut);
    }
    visualLines.push(remaining.join(""));
  }
  const parts = [];
  for (let index = 0; index < visualLines.length; index += maxLines) {
    parts.push(visualLines.slice(index, index + maxLines).join("\n"));
  }
  return parts;
}

async function imageBytes(imagePath) {
  return new Uint8Array(await fs.readFile(imagePath));
}

function addBase(slide, pageLabel) {
  slide.background.fill = colors.paper;
  textbox(slide, "云聪 YouTube 频道 Posts", { left: 72, top: 34, width: 720, height: 36 },
    { fontSize: 17, bold: true, color: colors.accent });
  textbox(slide, pageLabel, { left: 920, top: 34, width: 285, height: 30 },
    { fontSize: 16, color: colors.muted, alignment: "right" });
  addRule(slide, 82);
}

const cover = presentation.slides.add();
cover.background.fill = colors.paper;
cover.shapes.add({ geometry: "rect", position: { left: 72, top: 116, width: 9, height: 440 }, fill: colors.accent, line: { style: "solid", fill: "none", width: 0 } });
textbox(cover, unresolvedMonth ? "云聪帖子 · 历史时间范围" : "云聪频道帖子", { left: 112, top: 164, width: 1010, height: 90 }, { fontSize: 56, bold: true });
textbox(cover, unresolvedMonth ? olderBoundaryLabel : month, { left: 112, top: 280, width: 900, height: 62 }, { fontSize: unresolvedMonth ? 32 : 38, color: colors.accent });
textbox(cover, `${unresolvedMonth ? "年份级相对时间" : "本月归档"} ${posts.length} 条帖子`, { left: 112, top: 376, width: 760, height: 50 }, { fontSize: 25, color: colors.muted });
textbox(cover, unresolvedMonth
  ? `按采集月份 ${input.month_assignment_reference || "未记录"} 推断归入；YouTube 只提供“1 year ago”等相对时间，具体日期未提供。`
  : "页面只提供相对时间；归档月份为估算，不代表精确发帖日期。", { left: 112, top: 435, width: 1060, height: 54 }, { fontSize: 18, color: colors.muted });
cover.speakerNotes.text = `频道：${input.channel_url || "未提供"}\n归档月份：${month}\n帖子数：${posts.length}`;

let page = 0;
for (const [index, post] of posts.entries()) {
  const bodyParts = splitText(post.text);
  for (const [partIndex, body] of bodyParts.entries()) {
    const slide = presentation.slides.add();
    page += 1;
    addBase(slide, `${month} · ${String(index + 1).padStart(3, "0")}/${posts.length}`);
    const continuation = bodyParts.length > 1 ? `（${partIndex + 1}/${bodyParts.length}）` : "";
    const dateLabel = `${displayDate(post)}${continuation}`;
    textbox(slide, dateLabel, { left: 72, top: 108, width: 640, height: 66 }, { fontSize: 16, bold: true, color: colors.ink });
    textbox(slide, `帖子 ID：${post.post_id || "未知"}`, { left: 72, top: 178, width: 640, height: 28 }, { fontSize: 14, color: colors.muted });
    textbox(slide, body, { left: 72, top: 218, width: 610, height: 419 }, { fontSize: 24, color: colors.ink, verticalAlignment: "top" });

    const images = (post.downloaded_images || []).filter(image => image.path && !image.error);
    if (partIndex === 0 && images.length) {
      let imagePath = path.resolve(archive, images[0].path);
      let ext = path.extname(imagePath).toLowerCase();
      if (![".png", ".jpg", ".jpeg"].includes(ext)) {
        const convertedDir = path.join(buildDir, "converted_images");
        await fs.mkdir(convertedDir, { recursive: true });
        const convertedPath = path.join(convertedDir, `${String(index + 1).padStart(4, "0")}.jpg`);
        const conversion = spawnSync("/usr/bin/sips", ["-s", "format", "jpeg", imagePath, "--out", convertedPath], { encoding: "utf8" });
        if (conversion.status !== 0) throw new Error(`image_conversion_failed:${post.post_id}`);
        imagePath = convertedPath;
        ext = ".jpg";
      }
      const contentType = ext === ".png" ? "image/png" : "image/jpeg";
      slide.images.add({ blob: await imageBytes(imagePath), contentType, alt: `帖子 ${post.post_id} 配图`, fit: "contain", position: { left: 730, top: 122, width: 472, height: 470 } });
      if (images.length > 1) {
        textbox(slide, `另有 ${images.length - 1} 张配图保存在本月 media 目录。`, { left: 730, top: 602, width: 472, height: 34 }, { fontSize: 15, color: colors.muted });
      }
    } else if (partIndex === 0) {
      textbox(slide, post.video_ids?.length ? `关联视频：${post.video_ids.join("、")}` : "本条帖子没有可归档图片", { left: 730, top: 336, width: 450, height: 68 }, { fontSize: 21, color: colors.muted });
    }

    addRule(slide, 658);
    const url = post.url || "来源链接未提供";
    textbox(slide, url, { left: 72, top: 675, width: 1128, height: 25 }, { fontSize: 14, color: colors.accent });
    slide.speakerNotes.text = `帖子 ID：${post.post_id || "未知"}\n发布时间原文：${post.published_time_raw || "未提供"}\n精确发布时间：${post.published_timestamp || "YouTube 帖子页面未提供"}\n推算时间范围：${post.estimated_published_at_start || "未推算"} 至 ${post.estimated_published_at_end || "未推算"}\n推算说明：${post.estimated_published_at_note || "无可用的日期区间估算"}\n归档月份：${month}${post.archive_month_precision === "estimated_from_relative_display" ? "（相对时间估算，不代表发帖日）" : "（未确认）"}\n来源链接：${url}\n正文来自归档 JSON；配图来自本月 media 目录。`;
  }
}

const candidatePath = path.join(buildDir, "candidate.pptx");
const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(candidatePath);
const { finalizePresentation } = await import(pathToFileURL(path.join(skillDir, "container_tools/artifact_tool_utils.mjs")).href);
const result = await finalizePresentation({
  workspaceDir: workspace,
  candidatePath,
  finalPath,
  pythonExecutable: runtimePython,
  integrityValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs: ["--expected-slide-size-emu", "12192000,6858000", "--validate-heading-fit"],
  requiredNativeTableOwnerSlides: [],
  fontPolicy: { basis: "design", families: [fontFamily] },
  verifyArtifactToolImport: true,
  receiptPath: path.join(buildDir, "validation.json"),
});

const previews = path.join(buildDir, "previews");
await fs.mkdir(previews, { recursive: true });
for (const [index, slide] of presentation.slides.items.entries()) {
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(path.join(previews, `slide-${String(index + 1).padStart(4, "0")}.png`), new Uint8Array(await png.arrayBuffer()));
}
console.log(JSON.stringify({ month, postCount: posts.length, slideCount: presentation.slides.items.length, finalPath: result.finalPath || finalPath, receiptPath: path.join(buildDir, "validation.json") }, null, 2));
