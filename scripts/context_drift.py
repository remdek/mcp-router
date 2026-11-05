#!/usr/bin/env python3
"""Map changed source files to docs/context fragments via frontmatter sources:.

Usage:
  python scripts/context_drift.py [git-range]

Default range: origin/main..HEAD (or origin/master..HEAD). Exit 0 when every
changed source is covered by at least one fragment, or when there are no
documented-source changes. Exit 1 on uncovered sources. Exit 2 when the range
has nothing to compare (standing on the base branch).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "docs" / "context"
SOURCE_PREFIXES = ("src/", "config/", "pyproject.toml", "Makefile", ".github/")


def _default_range() -> str:
    for base in ("origin/main", "origin/master"):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", base],
            cwd=ROOT,
            capture_output=True,
        )
        if r.returncode == 0:
            return f"{base}..HEAD"
    return "HEAD~20..HEAD"


def _parse_sources(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end < 0:
        return []
    fm = text[3:end]
    sources: list[str] = []
    in_sources = False
    for line in fm.splitlines():
        if re.match(r"^sources:\s*$", line):
            in_sources = True
            continue
        if in_sources:
            m = re.match(r"^\s+-\s+(.+)$", line)
            if m:
                sources.append(m.group(1).strip().strip("\"'"))
                continue
            if re.match(r"^[a-zA-Z_]+:", line):
                in_sources = False
            else:
                break
    return sources


def _fragment_map() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for md in sorted(CONTEXT.glob("*.md")):
        if md.name == "README.md":
            continue
        rel_doc = f"docs/context/{md.name}"
        for src in _parse_sources(md):
            mapping.setdefault(src, []).append(rel_doc)
    return mapping


def _changed_files(git_range: str) -> list[str]:
    out = subprocess.check_output(
        ["git", "diff", "--name-only", git_range],
        cwd=ROOT,
        text=True,
    )
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return [
        f for f in files if f.startswith(SOURCE_PREFIXES) or f in ("pyproject.toml", "Makefile")
    ]


def main(argv: list[str]) -> int:
    git_range = argv[1] if len(argv) > 1 else _default_range()
    base = git_range.split("..", 1)[0]
    if (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", base],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    ):
        commits = int(
            subprocess.check_output(
                ["git", "rev-list", "--count", git_range],
                cwd=ROOT,
                text=True,
            ).strip()
            or "0"
        )
        if commits == 0:
            print(
                "NOTHING TO COMPARE — empty range "
                f"{git_range!r}. Use HEAD~N..HEAD or run from a feature branch.",
                file=sys.stderr,
            )
            return 2

    mapping = _fragment_map()
    changed = _changed_files(git_range)
    if not changed:
        print(f"No documented-source changes in {git_range}.")
        return 0

    print(f"Changed sources in {git_range} → fragments to review:")
    gaps: list[str] = []
    for path in changed:
        docs = mapping.get(path)
        if docs:
            print(f"  {path:48} → {', '.join(docs)}")
        else:
            # Only flag paths that look like code we normally document.
            if path.startswith("src/") or path.startswith("config/"):
                gaps.append(path)
            else:
                print(f"  {path:48} → (meta — see import-boundaries if contracts change)")

    if gaps:
        print("\nChanged sources with NO fragment:")
        for path in gaps:
            print(f"  {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
