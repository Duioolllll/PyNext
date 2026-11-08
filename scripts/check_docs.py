#!/usr/bin/env python3
"""Check PyNext documentation consistency (stdlib only, no build needed).

Run from the repository root: ``python scripts/check_docs.py``.
CI runs this in the dedicated ``docs`` job.

Checks:
1. Version unification: the current-version documents (README, manuals,
   wiki, ROADMAP, ARCHITECTURE_SPEC, release notes) never present the
   superseded ``0.6.0a2`` / ``0.7.0a1`` lines or the old ``aa256bf``
   commit as the current version. CHANGELOG.md and RELEASE_PLAN.md are
   excluded: they are historical records that legitimately mention
   those versions.
2. Stale claims: sentences that were wrong on main (no standalone
   export, no streaming SSR / HMR, no inter-worker cache sync,
   no WebSockets, "only CPython 3.13 verified", "not on PyPI")
   must not appear in the scanned documents.
3. Canonical source: the English manual declares itself canonical and
   the ko/ja/zh-CN manuals declare themselves translations of it;
   the wiki index points at the English manual as canonical.
4. Non-goals: the wording in docs/FEATURES.md matches docs/ROADMAP.md
   (React/RSC is a non-goal, Django direction undecided).
5. Generated table: docs/FEATURES.md matches
   ``scripts/generate_feature_table.py`` output (freshness check).

Exit code 0 when everything passes, 1 otherwise (failures are printed).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _scanned_files(root: Path) -> list[Path]:
    return [
        root / "README.md",
        root / "docs" / "en" / "manual.md",
        root / "docs" / "ko" / "manual.md",
        root / "docs" / "ja" / "manual.md",
        root / "docs" / "zh-CN" / "manual.md",
        root / "docs" / "ROADMAP.md",
        root / "docs" / "ARCHITECTURE_SPEC.md",
        root / "docs" / "RELEASE_NOTES_0.1.0.md",
        *sorted((root / "docs" / "wiki").rglob("*.md")),
    ]


def _canonical_markers(root: Path) -> list[tuple[Path, str]]:
    return [
        (root / "docs" / "en" / "manual.md", "Canonical source: this English manual is the single source of truth"),
        (root / "docs" / "ko" / "manual.md", "영문 매뉴얼의 번역본입니다"),
        (root / "docs" / "ja" / "manual.md", "英語マニュアルの翻訳です"),
        (root / "docs" / "zh-CN" / "manual.md", "英文手册的翻译"),
        (root / "docs" / "wiki" / "README.md", "Canonical source: the [English manual]"),
    ]

# Strings that must not appear in a current-version document.
STALE_STRINGS = [
    "v0.6.0a2",
    "0.6.0a2",
    "aa256bf",
    "0.1.0a0",
    "v0.1.0-alpha",
    "no standalone export",
    "Standalone 배포 산출물은 없습니다",
    "There is no inter-worker synchronization",
    "워커 간 캐시 동기화는 없습니다",
    "No built-in WebSockets",
    "Client-side router/navigation and HMR are not implemented",
    "클라이언트 라우팅과 HMR은 없습니다",
    "Only CPython 3.13 is verified",
    "현재 검증된 Python은 CPython 3.13입니다",
    "(the only currently verified Python version)",
    "(현재 검증된 유일한 Python 버전)",
    "do not assume it is distributed through PyPI",
    "PyPI에 게시된 패키지가 있다고 가정하면 안 됩니다",
]


def canonical_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_feature_table", ROOT / "scripts" / "generate_feature_table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check() -> list[str]:
    failures: list[str] = []
    version = canonical_version()
    scanned = _scanned_files(ROOT)
    # "0.7.0a1" is only allowed in README.md and RELEASE_NOTES_0.1.0.md,
    # where one sentence each records it as the superseded development
    # candidate (0.1.0 sorts lower than it). Everywhere else it would
    # read as a current version.
    stale_070_files = [p for p in scanned if p.name not in ("README.md", "RELEASE_NOTES_0.1.0.md")]

    for path in scanned:
        if not path.exists():
            failures.append(f"{path.relative_to(ROOT)}: missing file")
            continue
        text = path.read_text(encoding="utf-8")
        for stale in STALE_STRINGS:
            if stale in text:
                failures.append(f"{path.relative_to(ROOT)}: stale string {stale!r}")
        if path in stale_070_files and "0.7.0a1" in text:
            failures.append(f"{path.relative_to(ROOT)}: stale string '0.7.0a1' (superseded version)")

    for path, marker in _canonical_markers(ROOT):
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            failures.append(f"{path.relative_to(ROOT)}: missing canonical-source marker {marker!r}")

    # Non-goals wording must match docs/ROADMAP.md.
    roadmap = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    for phrase in ("React 및 React Server Components 호환성은 현재 비목표다",
                   "Django 통합은 ORM·폼 활용과 ASGI 서브앱 마운트 중 방향 결정을 보류한다"):
        if phrase not in roadmap:
            failures.append(f"docs/ROADMAP.md: non-goal phrase changed: {phrase!r}")

    # Generated feature table must be fresh and carry the current version.
    features_path = ROOT / "docs" / "FEATURES.md"
    if not features_path.exists():
        failures.append("docs/FEATURES.md: missing (run `python scripts/generate_feature_table.py`)")
    else:
        generated = _load_generator().render()
        if features_path.read_text(encoding="utf-8") != generated:
            failures.append("docs/FEATURES.md: stale (run `python scripts/generate_feature_table.py` to regenerate)")
        if version not in features_path.read_text(encoding="utf-8"):
            failures.append(f"docs/FEATURES.md: does not mention current version {version}")

    return failures


def main(argv: list[str] | None = None) -> int:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: parent of scripts/)")
    args = parser.parse_args(argv)
    ROOT = args.root.resolve()
    failures = check()
    if failures:
        print(f"docs check FAILED ({len(failures)} problem(s)):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("docs check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
