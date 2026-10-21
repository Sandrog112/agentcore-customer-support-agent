"""The console's record of this run: every agent turn and direct tool call, as the server saw it, saved to
evidence/ as a readable Markdown report plus the raw JSON. Saving again rewrites the same two files."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infra.config import ROOT

EVIDENCE_DIR = ROOT / "evidence"


class RunRecord:
    def __init__(self, directory: Path = EVIDENCE_DIR) -> None:
        self.started = datetime.now(UTC)
        self.entries: list[dict[str, Any]] = []
        self.saved_entries = 0
        self._directory = directory
        self._lock = threading.Lock()

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = {"id": len(self.entries) + 1, **entry}
            self.entries.append(entry)
            return entry

    def save(self, stack: dict[str, Any]) -> list[Path]:
        """Write the report and the JSON; `stack` describes the deployment and AWS identity the run used."""
        with self._lock:
            entries = list(self.entries)
            self.saved_entries = len(entries)
        self._directory.mkdir(exist_ok=True)
        name = f"console-run-{self.started:%Y%m%d-%H%M%S}"
        report, raw = self._directory / f"{name}.md", self._directory / f"{name}.json"
        report.write_text(render_report(self.started, stack, entries))
        data = {"started_at": self.started.isoformat(), "stack": stack, "entries": entries}
        raw.write_text(json.dumps(data, indent=2, default=str) + "\n")
        return [report, raw]


def render_report(started: datetime, stack: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    turns = sum(entry["kind"] == "chat" for entry in entries)
    lines = [
        f"# Support agent console run, {started:%Y-%m-%d %H:%M} UTC",
        "",
        f"- Region: {stack.get('region')}",
        f"- Runtime: `{stack.get('runtime_arn')}`",
        f"- Operator identity: `{stack.get('identity')}`",
        f"- Recorded: {turns} agent turns and {len(entries) - turns} direct tool calls",
        "",
    ]
    for entry in entries:
        lines += _chat_section(entry) if entry["kind"] == "chat" else _tool_section(entry)
    return "\n".join(lines) + "\n"


def _chat_section(entry: dict[str, Any]) -> list[str]:
    title = entry.get("scenario") or "Free-form message"
    lines = [
        f"## {entry['id']}. Agent turn: {title}",
        "",
        f"- Time: {entry['at']}",
        f"- Customer: `{entry['actor_id']}`",
        f"- Session: `{entry['session_id']}`",
        f"- Duration: {entry['duration_ms'] / 1000:.1f} s",
    ]
    if entry.get("trace_id"):
        lines.append(f"- Trace: [`{entry['trace_id']}`]({entry['trace_url']})")
    lines += ["", "Customer:", "", *_quote(entry["prompt"]), ""]
    if entry.get("error"):
        lines += ["Error:", "", *_quote(entry["error"]), ""]
    else:
        lines += ["Agent:", "", *_quote(entry.get("response") or ""), ""]
    calls = entry.get("tool_calls_detail")
    if calls:
        lines += ["| # | Tool | Input | Outcome | Detail |", "|---|---|---|---|---|"]
        for number, call in enumerate(calls, 1):
            cells = [str(number), call["tool"], f"`{json.dumps(call['input'])}`", call["outcome"]["label"]]
            lines.append("| " + " | ".join(_cell(cell) for cell in [*cells, call["outcome"]["detail"]]) + " |")
        lines.append("")
    elif calls == []:
        lines += ["No tool calls.", ""]
    elif entry.get("tool_calls_note"):
        lines += [f"Tool call details: {entry['tool_calls_note']}", ""]
    counts = entry.get("tool_calls") or {}
    if counts:
        lines += ["Runtime tool call counts: " + ", ".join(f"{tool} {count}" for tool, count in counts.items()), ""]
    return lines


def _tool_section(entry: dict[str, Any]) -> list[str]:
    lines = [
        f"## {entry['id']}. Direct tool call: {entry['tool']}",
        "",
        f"- Time: {entry['at']}",
        f"- Customer: `{entry['actor_id']}`",
        f"- Duration: {entry['duration_ms'] / 1000:.1f} s",
        f"- Arguments: `{json.dumps(entry['arguments'])}`",
    ]
    if entry.get("error"):
        lines += [f"- Error: {entry['error']}", ""]
        return lines
    detail = entry["outcome"]["detail"]
    lines += [f"- Outcome: {entry['outcome']['label']}" + (f" ({detail})" if detail else ""), ""]
    lines += ["```json", json.dumps(entry["result"], indent=2, default=str), "```", ""]
    return lines


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in text.splitlines()] or [">"]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
