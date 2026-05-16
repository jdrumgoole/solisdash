"""Tests for the SVG sparkline path generator."""

from __future__ import annotations

from solisdash.tiles import sparkline_path


def test_sparkline_path_returns_none_for_empty() -> None:
    assert sparkline_path([]) is None


def test_sparkline_path_returns_none_for_single_value() -> None:
    assert sparkline_path([1.0]) is None


def test_sparkline_path_returns_none_when_all_values_missing() -> None:
    assert sparkline_path([None, None, None]) is None


def test_sparkline_path_starts_with_move_command() -> None:
    path = sparkline_path([1.0, 2.0, 3.0])
    assert path is not None
    assert path.startswith("M")
    # Three points → one M and two L segments.
    assert path.count("L") == 2


def test_sparkline_path_normalises_to_viewbox() -> None:
    path = sparkline_path([0.0, 10.0], width=200, height=40, pad=0)
    assert path is not None
    # Two points span the full width.
    assert path.startswith("M0.00,")
    assert " L200.00," in path
