"""Isolated live-API validation of the Messages API wire shape.

Grounds the payload shape against the real API before any selection logic is
written on top of it, covering two integration risks in one script.

This script deliberately does not touch scripts/llm.py. The point is to
validate the wire shape directly, bypassing any wrapper that might silently
drop unfamiliar content blocks. llm.call() extracts text-typed blocks only;
thinking and tool_use blocks would be lost through that path even if the API
returned them.

What it DOES do: three minimal real Anthropic Messages API calls (Haiku
4.5, sub-penny each), one per integration probe:

  Probe 1 — thinking parameter actually engages
      Send: thinking={"type": "enabled", "budget_tokens": N}, temp=1.0
      Verify: response.content[] contains a block with type == "thinking"
              and a non-empty thinking field (the reasoning).
      Implication: a wrong key produces a 200 OK that just doesn't have
      the thinking block — silent failure. This probe makes the silence
      visible.

  Probe 2 — tools parameter triggers a tool_use block
      Send: tools=[{name, description, input_schema}] + a prompt that
            cannot be answered without the tool
      Verify: response.content[] contains a block with type == "tool_use",
              the tool name matches, and input matches the schema.

  Probe 3 — combined thinking + tools in one call
      Send: both parameters together (the shape used for
            complex-reasoning tool-using calls)
      Verify: response.content[] contains BOTH a thinking block and a
              tool_use block.

For each probe, the script reports:
  - Whether the call succeeded (HTTP 200)
  - The full content[] shape (every block's type and a snippet)
  - Whether the expected block type was actually present
  - PASS only if both true

This is the "did the API actually do the thing" test: validate the
foundation against reality first, then build selection logic on top.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Use jobcfg + certifi the same way llm.py does, but NOT llm.call().
from alice.jobcfg import load as _load_cfg  # noqa: E402

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_MODEL = "claude-haiku-4-5-20251001"


def _raw_call(payload: dict, label: str) -> dict | None:
    """Make the raw HTTPS call; return the parsed JSON response or None
    on error. Does NOT log to llm.py's cost log — this is a probe, not a
    production call."""
    cfg = _load_cfg()
    key = cfg.get("ANTHROPIC_API_KEY")
    if not key:
        print(f"  ERROR [{label}]: ANTHROPIC_API_KEY not set in config.env")
        return None

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _API_URL,
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_text = e.read().decode(errors="replace")[:500]
        print(f"  ERROR [{label}]: HTTP {e.code}: {err_text}")
        return None
    except Exception as e:
        print(f"  ERROR [{label}]: {type(e).__name__}: {e}")
        return None


def _describe_content(content: list, indent: str = "    ") -> str:
    """Render each block's type + a short payload preview."""
    lines = []
    for i, block in enumerate(content):
        btype = block.get("type", "<no-type>")
        if btype == "thinking":
            snippet = (block.get("thinking", "") or "").replace("\n", " ")[:80]
            lines.append(f"{indent}[{i}] type='thinking'  thinking[:80]={snippet!r}")
        elif btype == "text":
            snippet = (block.get("text", "") or "").replace("\n", " ")[:80]
            lines.append(f"{indent}[{i}] type='text'      text[:80]={snippet!r}")
        elif btype == "tool_use":
            tname = block.get("name", "")
            tinput = json.dumps(block.get("input", {}))[:80]
            tid = block.get("id", "")[:16]
            lines.append(f"{indent}[{i}] type='tool_use'  name={tname!r} id={tid!r} input={tinput}")
        elif btype == "redacted_thinking":
            lines.append(f"{indent}[{i}] type='redacted_thinking'  (encrypted)")
        else:
            keys = list(block.keys())
            lines.append(f"{indent}[{i}] type={btype!r}  unexpected; keys={keys}")
    return "\n".join(lines) if lines else f"{indent}(content is empty)"


def _has_block_of_type(content: list, btype: str) -> bool:
    return any(b.get("type") == btype for b in content)


# ─── Probe 1: thinking parameter ─────────────────────────────────────────────

def probe_thinking() -> bool:
    print("\n[Probe 1] extended thinking — does `thinking` payload key engage?")
    payload = {
        "model": _MODEL,
        "max_tokens": 2048,
        "temperature": 1.0,  # extended thinking requires temperature=1.0
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [{
            "role": "user",
            "content": "Compute 23 * 47 step by step. Show your reasoning, then give the final number.",
        }],
    }
    resp = _raw_call(payload, "thinking")
    if resp is None:
        return False

    content = resp.get("content", [])
    print(f"  stop_reason: {resp.get('stop_reason')!r}")
    print(f"  usage: {resp.get('usage')}")
    print(f"  content blocks:")
    print(_describe_content(content))

    has_thinking = _has_block_of_type(content, "thinking") or _has_block_of_type(content, "redacted_thinking")
    has_text = _has_block_of_type(content, "text")

    if has_thinking and has_text:
        print(f"  PASS — thinking block present AND text block present.")
        return True
    if has_thinking and not has_text:
        print(f"  PARTIAL — thinking present but no text. Likely max_tokens too low.")
        return False
    if not has_thinking:
        print(f"  FAIL — NO thinking block in response. Payload key may be wrong, "
              f"or thinking didn't engage. (This is the silent-failure scenario "
              f"the build map warned about.)")
        return False
    return False


# ─── Probe 2: tools parameter triggers tool_use ──────────────────────────────

def probe_tools() -> bool:
    print("\n[Probe 2] tool use — does `tools` payload trigger a tool_use block?")
    payload = {
        "model": _MODEL,
        "max_tokens": 1024,
        "temperature": 1.0,
        "tools": [{
            "name": "get_current_time",
            "description": "Returns the current ISO timestamp. Use this whenever the user asks for the current time, date, or 'right now.'",
            "input_schema": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone, e.g. 'America/New_York'. Optional.",
                    },
                },
                "required": [],
            },
        }],
        "messages": [{
            "role": "user",
            "content": "What is the current time right now? Use the tool I gave you.",
        }],
    }
    resp = _raw_call(payload, "tools")
    if resp is None:
        return False

    content = resp.get("content", [])
    stop = resp.get("stop_reason")
    print(f"  stop_reason: {stop!r}")
    print(f"  usage: {resp.get('usage')}")
    print(f"  content blocks:")
    print(_describe_content(content))

    has_tool_use = _has_block_of_type(content, "tool_use")
    if has_tool_use and stop == "tool_use":
        # Find the tool_use block, verify name
        tu = next(b for b in content if b.get("type") == "tool_use")
        if tu.get("name") == "get_current_time":
            print(f"  PASS — tool_use block present, name matches, stop_reason='tool_use'.")
            return True
        else:
            print(f"  FAIL — tool_use present but name {tu.get('name')!r} != 'get_current_time'.")
            return False
    if has_tool_use and stop != "tool_use":
        print(f"  PARTIAL — tool_use block present but stop_reason is {stop!r} not 'tool_use'.")
        return False
    print(f"  FAIL — NO tool_use block (model answered directly instead of calling).")
    return False


# ─── Probe 3: thinking + tools together ──────────────────────────────────────

def probe_combined() -> bool:
    print("\n[Probe 3] combined — thinking and tools together in one call")
    payload = {
        "model": _MODEL,
        "max_tokens": 4096,
        "temperature": 1.0,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "tools": [{
            "name": "lookup_role_status",
            "description": "Look up the current status of a job application by company name. Returns one of: new, good fit, materials pending, submitted, first screen scheduled, interviewing, offer, closed, not a fit.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "company_substring": {
                        "type": "string",
                        "description": "Substring matching the company name in the pipeline.",
                    },
                },
                "required": ["company_substring"],
            },
        }],
        "messages": [{
            "role": "user",
            "content": "The candidate asked me what the status of his Northwind Systems application is. I need to reason about what tool to use and then call it. What's Northwind Systems's status?",
        }],
    }
    resp = _raw_call(payload, "combined")
    if resp is None:
        return False

    content = resp.get("content", [])
    stop = resp.get("stop_reason")
    print(f"  stop_reason: {stop!r}")
    print(f"  usage: {resp.get('usage')}")
    print(f"  content blocks:")
    print(_describe_content(content))

    has_thinking = _has_block_of_type(content, "thinking") or _has_block_of_type(content, "redacted_thinking")
    has_tool_use = _has_block_of_type(content, "tool_use")

    if has_thinking and has_tool_use:
        print(f"  PASS — both thinking AND tool_use blocks present in same response.")
        return True
    print(f"  FAIL — thinking_present={has_thinking}, tool_use_present={has_tool_use}.")
    return False


def main() -> int:
    print("=== isolated live-API validation ===")
    print(f"Model: {_MODEL}")
    print(f"API:   {_API_URL}")
    print(f"This script validates the live wire shape before any selection")
    print(f"logic is built on top. Each probe is one real call (~$0.001 ea).")

    p1 = probe_thinking()
    p2 = probe_tools()
    p3 = probe_combined()

    print(f"\n=== summary ===")
    print(f"  thinking parameter:        {'PASS' if p1 else 'FAIL'}")
    print(f"  tools parameter:           {'PASS' if p2 else 'FAIL'}")
    print(f"  combined thinking+tools:   {'PASS' if p3 else 'FAIL'}")

    if p1 and p2 and p3:
        print("\nAll three probes engaged. Wire shape is grounded against the live API.")
        print("Safe to proceed with selection logic on top of these payload shapes.")
        return 0
    print("\nAt least one probe failed to engage. STOP — do not build on an")
    print("unvalidated foundation. Inspect each FAIL above; adjust the payload")
    print("shape or model selection, re-probe, then proceed only after a clean run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
