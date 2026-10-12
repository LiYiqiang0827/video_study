#!/usr/bin/env python3
"""Analyze an archived Posts JSON file using the configured remote GPU Qwen service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "analyze_posts.md"


def request_json(url, payload, transport, ssh_host, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    if transport == "ssh":
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", ssh_host) or ssh_host.startswith("-"):
            raise ValueError("ssh_host_invalid")
        command = ["curl", "--noproxy", "127.0.0.1,localhost,::1", "-sS", "--fail-with-body", "--max-time", str(timeout), "-H", "Content-Type:application/json"]
        if data is not None:
            command.extend(["--data-binary", "@-"])
        command.append(url)
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=30", ssh_host, shlex.join(command)],
                                input=data, capture_output=True, timeout=timeout + 45)
        if result.returncode:
            raise RuntimeError("remote_qwen_transport_failed")
        return json.loads(result.stdout.decode("utf-8"))
    from urllib.request import Request, urlopen
    with urlopen(Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=timeout) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--transport", choices=("ssh", "http"), default="ssh")
    parser.add_argument("--ssh-host", default=os.environ.get("THEME_VLLM_SSH_HOST", "llm-server"))
    parser.add_argument("--base-url", default=os.environ.get("THEME_VLLM_REMOTE_BASE_URL", "http://127.0.0.1:8002/v1"))
    parser.add_argument("--model", default=os.environ.get("THEME_VLLM_MODEL"))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=12000)
    parser.add_argument("--raw-response", type=Path, help="Offline response for review; no model call")
    args = parser.parse_args(argv)
    source = args.input.expanduser().resolve(strict=True)
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        parser.error("Output directory already exists; select a new path to preserve previous analysis")
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    posts = json.loads(source_bytes)
    if posts.get("schema_version") != 1 or not isinstance(posts.get("posts"), list):
        parser.error("Input is not a supported posts.json snapshot")
    if not posts["posts"]:
        parser.error("No posts in input snapshot")
    output.mkdir(parents=True, mode=0o700)
    prompt_template = PROMPT.read_text(encoding="utf-8")
    packet = json.dumps(posts, ensure_ascii=False, indent=2)
    prompt = prompt_template.replace("{{posts_json}}", packet)
    try:
        if args.raw_response:
            response_path = args.raw_response.expanduser().resolve(strict=True)
            raw = response_path.read_text(encoding="utf-8")
            try:
                response_data = json.loads(raw)
                if isinstance(response_data, dict) and response_data.get("choices"):
                    raw = response_data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                pass
            model_name, response_hash = "offline_response", hashlib.sha256(raw.encode()).hexdigest()
        else:
            base_url = args.base_url.rstrip("/")
            models = request_json(base_url + "/models", None, args.transport, args.ssh_host, 30)
            model_name = args.model or models["data"][0]["id"]
            payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                       "max_tokens": args.max_tokens, "chat_template_kwargs": {"enable_thinking": False}}
            (output / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            response = request_json(base_url + "/chat/completions", payload, args.transport, args.ssh_host, args.timeout)
            (output / "response.json").write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
            raw = response["choices"][0]["message"]["content"]
            model_name = response.get("model", model_name)
            response_hash = hashlib.sha256(raw.encode()).hexdigest()
        (output / "prompt.txt").write_text(prompt, encoding="utf-8")
        (output / "analysis.md").write_text(raw.rstrip() + "\n", encoding="utf-8")
        provenance = {"state": "analyzed", "created_at": datetime.now(timezone.utc).isoformat(), "input_path": str(source),
                      "input_sha256": source_hash, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                      "response_sha256": response_hash, "model": model_name,
                      "transport": "offline_response" if args.raw_response else args.transport,
                      "post_ids": [post.get("post_id") for post in posts["posts"]]}
        (output / "ANALYSIS_PROVENANCE.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"state": "analyzed", "post_count": len(posts["posts"]), "output": str(output / "analysis.md"), "model": model_name}, ensure_ascii=False))
        return 0
    except Exception as exc:
        (output / "ERROR.json").write_text(json.dumps({"state": "failed", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"state": "failed", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
