#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "marin-style @ git+https://github.com/marin-community/marin-style@@MARIN_STYLE_REV@",
# ]
# ///

from marin_style.echo import echo_group

echo_group()
