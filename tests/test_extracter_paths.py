"""Tests for the shared extracter-root resolver.

Regression guard for the Windows startup bug: the old
``"/mnt/c" if os.path.isdir("/mnt/c") else "C:"`` heuristic misfired on native
Windows (which resolves ``/mnt/c`` to ``C:\\mnt\\c``), yielding a mangled
``/mnt/c/…\\GCDictionary.dict`` path. The resolver now probes real directories,
so it only ever returns a path that actually exists (or a well-formed default).
"""
import os

import drserver.data.extracter_paths as ep

_ENV_VARS = ("DR_EXTRACTER_DIR", "DR_EXTRACTER_ROOT")


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_candidate_roots_first_entry_is_repo_sibling():
    # The most-reliable candidate is drive/view-agnostic: a sibling of the repo.
    roots = ep.candidate_roots()
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(ep.__file__), "..", ".."))
    expected = os.path.join(os.path.dirname(repo_root), "extracter")
    assert roots[0] == expected


def test_resolve_honors_env_override_when_dir_exists(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DR_EXTRACTER_DIR", str(tmp_path))
    assert ep.resolve_extracter_dir() == str(tmp_path)


def test_resolve_honors_legacy_env_var_name(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DR_EXTRACTER_ROOT", str(tmp_path))
    assert ep.resolve_extracter_dir() == str(tmp_path)


def test_resolve_ignores_env_override_that_is_not_a_dir(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    missing = str(tmp_path / "nope")
    monkeypatch.setenv("DR_EXTRACTER_DIR", missing)
    existing = tmp_path / "real_extracter"
    existing.mkdir()
    monkeypatch.setattr(ep, "candidate_roots", lambda: [missing, str(existing)])
    assert ep.resolve_extracter_dir() == str(existing)


def test_resolve_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(
        ep, "candidate_roots", lambda: [str(tmp_path / "a"), str(tmp_path / "b")])
    assert ep.resolve_extracter_dir() is None


def test_resolve_picks_first_existing_candidate(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setattr(
        ep, "candidate_roots", lambda: [str(tmp_path / "first"), str(second)])
    assert ep.resolve_extracter_dir() == str(second)


def test_extracter_dir_falls_back_to_env_when_nothing_exists(tmp_path, monkeypatch):
    # Best-guess default must be well-formed (the env value), never a probed miss.
    _clear_env(monkeypatch)
    missing = str(tmp_path / "ghost")
    monkeypatch.setenv("DR_EXTRACTER_DIR", missing)
    monkeypatch.setattr(ep, "candidate_roots", lambda: [str(tmp_path / "x")])
    assert ep.extracter_dir() == missing


def test_extracter_dir_falls_back_to_sibling_without_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(ep, "resolve_extracter_dir", lambda: None)
    assert ep.extracter_dir() == ep.candidate_roots()[0]


def test_extracter_dir_never_mixes_separators(tmp_path, monkeypatch):
    # A returned, existing path joined with a filename yields a single-separator
    # path — the exact property the old WSL/Windows heuristic violated.
    _clear_env(monkeypatch)
    real = tmp_path / "extracter"
    real.mkdir()
    monkeypatch.setattr(ep, "candidate_roots", lambda: [str(real)])
    joined = os.path.join(ep.extracter_dir(), "GCDictionary.dict")
    # No backslash-after-forwardslash (or vice-versa) mixed-separator artifact.
    assert "/\\" not in joined and "\\/" not in joined
    assert os.path.dirname(joined) == str(real)
