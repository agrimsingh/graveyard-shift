"""Parse ignore entries (and their justifying comments) out of dependabot.yml.

PyYAML drops comments, but the comments ARE the data here: each one documents
why a pin exists. So we walk the raw lines instead, tracking update blocks.
"""

import hashlib
import re
from dataclasses import dataclass


@dataclass
class IgnoreEntry:
    dependency: str
    directory: str
    reason: str
    raw: str

    @property
    def entry_hash(self) -> str:
        return hashlib.sha256(self.raw.encode()).hexdigest()[:16]


def parse_ignore_entries(yaml_text: str) -> list[IgnoreEntry]:
    entries: list[IgnoreEntry] = []
    block: list[IgnoreEntry] = []
    block_directory = ""
    in_ignore = False
    ignore_indent = 0
    comment_buffer: list[str] = []
    current: IgnoreEntry | None = None

    def flush_block() -> None:
        nonlocal block, block_directory
        for entry in block:
            entry.directory = block_directory
        entries.extend(block)
        block = []
        block_directory = ""

    for line in yaml_text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- package-ecosystem:"):
            flush_block()
            in_ignore = False
            current = None
            comment_buffer = []
            continue

        directory_match = re.match(r'directory:\s*"?([^"\s]+)"?', stripped)
        if directory_match:
            block_directory = directory_match.group(1)

        if stripped.startswith("ignore:"):
            in_ignore = True
            ignore_indent = indent
            comment_buffer = []
            current = None
            continue

        if not in_ignore:
            continue
        # The ignore block ends at the next key at or above its indent level.
        if stripped and indent <= ignore_indent and not stripped.startswith(("#", "-")):
            in_ignore = False
            current = None
            continue
        if stripped.startswith("#"):
            comment_buffer.append(stripped.lstrip("# "))
        elif match := re.match(r'-\s*dependency-name:\s*"?([^"\s]+)"?', stripped):
            if comment_buffer:
                reason = " ".join(comment_buffer)
            elif block:
                # Consecutive entries under one comment share the justification.
                reason = block[-1].reason
            else:
                reason = "(no comment)"
            current = IgnoreEntry(
                dependency=match.group(1),
                directory="",
                reason=reason,
                raw=" ".join(comment_buffer) + "|" + stripped,
            )
            block.append(current)
            comment_buffer = []
        elif current and stripped.startswith("update-types:"):
            current.raw += "|" + stripped

    flush_block()
    return entries
