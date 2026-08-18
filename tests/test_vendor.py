from pathlib import Path

from marin_style.vendor import _render


def test_python_asset_keeps_shebang_and_pins_source_revision() -> None:
    source = Path("src/marin_style/assets/skills/consult-echo/scripts/echo.py")

    rendered = _render(source, "0.4.0", "abc123")

    assert rendered.startswith("#!/usr/bin/env -S uv run --script\n# Vendored from")
    assert "marin-style@abc123" in rendered
    assert "@MARIN_STYLE_REV@" not in rendered
    compile(rendered, str(source), "exec")
