"""Tests for per-intent workspace management."""

from __future__ import annotations

from pathlib import Path

from workestrator.workspace import WorkspaceManager, _sanitize


def test_creates_intent_dir(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path / "workspaces")
    path = wm.for_intent("intent_abc")
    assert path.is_dir()
    assert path.parent == (tmp_path / "workspaces").resolve()


def test_sanitizes_unsafe_chars() -> None:
    assert _sanitize("intent_abc") == "intent_abc"
    assert _sanitize("intent/abc") == "intent_abc"
    assert _sanitize("intent abc") == "intent_abc"
    assert _sanitize("intent:abc") == "intent_abc"
    assert _sanitize("intent.abc-123") == "intent.abc-123"  # dots and hyphens pass through


def test_for_intent_is_idempotent(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path / "workspaces")
    p1 = wm.for_intent("intent_x")
    p2 = wm.for_intent("intent_x")
    assert p1 == p2
