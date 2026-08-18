import unittest
from pathlib import Path

from marin_style.vendor import _render


class VendorScriptTest(unittest.TestCase):
    def test_python_asset_keeps_shebang_and_pins_source_revision(self) -> None:
        source = Path("src/marin_style/assets/skills/consult-echo/scripts/echo.py")

        rendered = _render(source, "0.4.0", "abc123")

        self.assertTrue(rendered.startswith("#!/usr/bin/env -S uv run --script\n# Vendored from"))
        self.assertIn("marin-style@abc123", rendered)
        self.assertNotIn("@MARIN_STYLE_REV@", rendered)
        compile(rendered, str(source), "exec")


if __name__ == "__main__":
    unittest.main()
