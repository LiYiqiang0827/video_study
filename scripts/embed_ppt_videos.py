#!/usr/bin/env python3
"""Embed verified MP4s into existing PPTX picture objects without changing layout.

The presentation is authored with Artifact Tool. This adapter supplies Office's
video relationships, which are not exposed by the installed authoring API.
No media transcoding, inference, download, or source replacement happens here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from lxml import etree as ET

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
VIDEO_REL = NS["r"] + "/video"
MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"
ROOT = Path(__file__).resolve().parents[1]
PARSER = ET.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_xml(data: bytes):
    return ET.fromstring(data, PARSER)


def serialize(root) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def target_part(owner: str, target: str) -> str:
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))


def outside_code(path: Path) -> Path:
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Generated files must remain outside the tools checkout")
    return path


def embed(input_path: Path, spec_path: Path, output_path: Path, receipt_path: Path) -> dict:
    output_path = outside_code(output_path)
    receipt_path = outside_code(receipt_path)
    if output_path == receipt_path or output_path.suffix.lower() != ".pptx":
        raise ValueError("Use a PPTX output and a separate receipt file")
    if output_path.exists() or receipt_path.exists():
        raise ValueError("Existing output preserved; use a new output and receipt")
    source_bytes = input_path.read_bytes()
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes)
    if spec.get("schema_version") != "yuncong_ppt_embedding_v1" or spec.get("input_pptx_sha256") != digest(source_bytes):
        raise ValueError("Embedding plan does not match the frozen PPTX")
    videos = spec.get("videos")
    if not isinstance(videos, list) or not videos:
        raise ValueError("No videos requested")
    with ZipFile(input_path) as original:
        if original.testzip() is not None:
            raise ValueError("Corrupt source package")
        if len(original.namelist()) != len(set(original.namelist())):
            raise ValueError("Duplicate ZIP parts")
        parts = {name: original.read(name) for name in original.namelist()}
    media_parts = set()
    changed_slides = set()
    checks = []
    for item in videos:
        slide_no = item.get("slide")
        if not isinstance(slide_no, int) or isinstance(slide_no, bool) or slide_no < 1 or slide_no in changed_slides:
            raise ValueError("Use one video per distinct detail slide")
        slide_part = f"ppt/slides/slide{slide_no}.xml"
        rel_part = f"ppt/slides/_rels/slide{slide_no}.xml.rels"
        slide, rels = read_xml(parts[slide_part]), read_xml(parts[rel_part])
        video_path = Path(item["path"]).resolve(strict=True)
        if video_path.suffix.lower() != ".mp4":
            raise ValueError("Only verified MP4 media is supported")
        video_bytes = video_path.read_bytes()
        video_hash = digest(video_bytes)
        if video_hash != item["sha256"]:
            raise ValueError("Video changed since preparation")
        relationships = {r.get("Id"): r for r in rels}
        matches = []
        for pic in slide.findall(".//p:pic", NS):
            blip = pic.find("p:blipFill/a:blip", NS)
            if blip is None:
                continue
            rel = relationships.get(blip.get(f"{{{NS['r']}}}embed"))
            if rel is not None and rel.get("TargetMode") != "External":
                image_part = target_part(slide_part, rel.get("Target"))
                if digest(parts[image_part]) == item["poster_sha256"]:
                    matches.append(pic)
        if len(matches) != 1:
            raise ValueError("The registered poster must match exactly one picture")
        pic = matches[0]
        nv = pic.find("p:nvPicPr", NS)
        cprops, appprops = nv.find("p:cNvPr", NS), nv.find("p:nvPr", NS)
        if appprops is None:
            appprops = ET.SubElement(nv, f"{{{NS['p']}}}nvPr")
        if appprops.find("a:videoFile", NS) is not None:
            raise ValueError("Picture already contains video")
        part = f"ppt/media/yuncong_{video_hash[:24]}.mp4"
        if part in parts and digest(parts[part]) != video_hash:
            raise ValueError("Media part collision")
        parts[part] = video_bytes
        media_parts.add(part)
        ids = set(relationships)
        new_ids = []
        for label, rel_type in (("video", VIDEO_REL), ("media", MEDIA_REL)):
            rid = f"Yuncong{slide_no}{label}"
            while rid in ids:
                rid += "_"
            ids.add(rid)
            ET.SubElement(rels, f"{{{NS['rel']}}}Relationship", Id=rid, Type=rel_type, Target="../media/" + posixpath.basename(part))
            new_ids.append(rid)
        # Click-to-play uses Office's native media action on the existing poster.
        if cprops.find("a:hlinkClick", NS) is not None:
            raise ValueError("Poster already has a click action")
        # CT_Hyperlink's optional r:id is omitted for an internal media action.
        click = ET.Element(f"{{{NS['a']}}}hlinkClick", action="ppaction://media")
        cprops.insert(0, click)
        cprops.set("name", f"云聪讲解视频_{slide_no}")
        extensions = appprops.find("p:extLst", NS)
        video_file = ET.Element(f"{{{NS['a']}}}videoFile", {f"{{{NS['r']}}}link": new_ids[0]})
        appprops.insert(list(appprops).index(extensions) if extensions is not None else len(appprops), video_file)
        if extensions is None:
            extensions = ET.SubElement(appprops, f"{{{NS['p']}}}extLst")
        ext = ET.SubElement(extensions, f"{{{NS['p']}}}ext", uri="{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}")
        ET.SubElement(ext, f"{{{NS['p14']}}}media", {f"{{{NS['r']}}}embed": new_ids[1]}, nsmap={"p14": NS["p14"]})
        # Retain prior animations; add a waiting media node for native controls.
        timing = slide.find("p:timing", NS)
        ids_used = [int(t.get("id")) for t in slide.findall(".//p:cTn", NS) if t.get("id", "").isdigit()]
        next_id = max(ids_used, default=0) + 1
        if timing is None:
            timing = ET.Element(f"{{{NS['p']}}}timing")
            slide_ext = slide.find("p:extLst", NS)
            slide.insert(list(slide).index(slide_ext) if slide_ext is not None else len(slide), timing)
            nodes = ET.SubElement(timing, f"{{{NS['p']}}}tnLst")
            par = ET.SubElement(nodes, f"{{{NS['p']}}}par")
            root = ET.SubElement(par, f"{{{NS['p']}}}cTn", id=str(next_id), dur="indefinite", restart="never", nodeType="tmRoot")
            next_id += 1
        else:
            roots = timing.xpath("p:tnLst/p:par/p:cTn[@nodeType='tmRoot']", namespaces=NS)
            if len(roots) != 1:
                raise ValueError("Existing timing tree needs an explicit merge")
            root = roots[0]
        children = root.find("p:childTnLst", NS)
        if children is None:
            children = ET.SubElement(root, f"{{{NS['p']}}}childTnLst")
        video_node = ET.SubElement(children, f"{{{NS['p']}}}video")
        common = ET.SubElement(video_node, f"{{{NS['p']}}}cMediaNode", vol="100000")
        time_node = ET.SubElement(common, f"{{{NS['p']}}}cTn", id=str(next_id), fill="hold", display="0")
        conditions = ET.SubElement(time_node, f"{{{NS['p']}}}stCondLst")
        ET.SubElement(conditions, f"{{{NS['p']}}}cond", delay="indefinite")
        target = ET.SubElement(common, f"{{{NS['p']}}}tgtEl")
        ET.SubElement(target, f"{{{NS['p']}}}spTgt", spid=cprops.get("id"))
        parts[slide_part], parts[rel_part] = serialize(slide), serialize(rels)
        changed_slides.add(slide_no)
        checks.append({"slide": slide_no, "entity_id": item["entity_id"], "shape_id": cprops.get("id"), "part": part, "sha256": video_hash, "bytes": len(video_bytes), "poster_sha256": item["poster_sha256"], "source": str(video_path)})
    types = read_xml(parts["[Content_Types].xml"])
    existing = types.find("ct:Default[@Extension='mp4']", NS)
    if existing is not None and existing.get("ContentType") != "video/mp4":
        raise ValueError("Conflicting MP4 content type")
    if existing is None:
        ET.SubElement(types, f"{{{NS['ct']}}}Default", Extension="mp4", ContentType="video/mp4")
    parts["[Content_Types].xml"] = serialize(types)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as stream, ZipFile(stream, "w", compression=ZIP_DEFLATED) as package:
        for name, data in parts.items():
            package.writestr(name, data, compress_type=ZIP_STORED if name in media_parts else ZIP_DEFLATED)
    with ZipFile(output_path) as package:
        if package.testzip() is not None:
            raise ValueError("Output package CRC failure")
        for check in checks:
            if digest(package.read(check["part"])) != check["sha256"]:
                raise ValueError("Embedded media failed byte verification")
            slide = read_xml(package.read(f"ppt/slides/slide{check['slide']}.xml"))
            if len(slide.findall(".//p14:media", NS)) != 1:
                raise ValueError("Expected one embedded video on each detail slide")
            if slide.find(".//p:video/p:cMediaNode/p:tgtEl/p:spTgt", NS).get("spid") != check["shape_id"]:
                raise ValueError("Playback timing does not address its poster")
    result = {"schema_version": "yuncong_ppt_embedding_receipt_v1", "status": "embedded", "input_sha256": digest(source_bytes), "spec_sha256": digest(spec_bytes), "output": str(output_path), "output_sha256": digest(output_path.read_bytes()), "embedded_count": len(checks), "embedded_bytes": sum(len(parts[p]) for p in media_parts), "checks": checks, "external_video_links": 0, "native_playback_verified": False}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    result = embed(args.input, args.spec, args.output, args.receipt)
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
