"""Discord format_message: compact table and paragraph rendering."""

import types
import sys


def _make_discord_adapter():
    """Construct a DiscordAdapter with discord.py stubbed out."""
    fake_discord = types.ModuleType("discord")
    fake_discord.Intents = type("Intents", (), {"default": classmethod(lambda cls: cls())})
    fake_discord.Message = object
    fake_ext = types.ModuleType("discord.ext")
    fake_commands = types.ModuleType("discord.ext.commands")
    fake_ext.commands = fake_commands
    fake_discord.ext = fake_ext
    sys.modules.setdefault("discord", fake_discord)
    sys.modules.setdefault("discord.ext", fake_ext)
    sys.modules.setdefault("discord.ext.commands", fake_commands)

    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    return adapter


class TestDiscordFormatMessage:

    def test_two_column_table_becomes_a_compact_code_block(self):
        adapter = _make_discord_adapter()
        text = (
            "Results:\n\n"
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 95   |\n"
            "| Bob   | 80   |\n"
            "\nDone."
        )
        out = adapter.format_message(text)
        assert "```text\nName   Score\nAlice  95\nBob    80\n```" in out
        assert out.startswith("Results:")
        assert out.rstrip().endswith("Done.")
        assert "|---" not in out

    def test_three_column_table_keeps_columns_together(self):
        adapter = _make_discord_adapter()
        text = "| Name | Age | City |\n|---|---|---|\n| Ada | 30 | Rome |"
        assert adapter.format_message(text) == (
            "```text\nName  Age  City\nAda   30   Rome\n```"
        )

    def test_long_cells_use_compact_key_value_rows(self):
        adapter = _make_discord_adapter()
        text = (
            "| Service | Status | Detail |\n"
            "|---|---|---|\n"
            "| Gateway | degraded | This sentence makes a monospaced table too wide for Discord. |"
        )
        out = adapter.format_message(text)
        assert "```" not in out
        assert out == (
            "**Service:** Gateway · **Status:** degraded · **Detail:** "
            "This sentence makes a monospaced table too wide for Discord."
        )

    def test_escaped_pipe_and_rich_tokens_do_not_enter_code_block(self):
        adapter = _make_discord_adapter()
        text = "| Owner | State |\n|---|---|\n| <@123> | :wave: a\\|b at C:\\work |"
        out = adapter.format_message(text)
        assert "```" not in out
        assert "<@123>" in out
        assert ":wave: a|b at C:\\work" in out

    def test_plain_text_unchanged(self):
        adapter = _make_discord_adapter()
        text = "Hello world, no tables here."
        assert adapter.format_message(text) == text

    def test_code_block_table_unchanged(self):
        adapter = _make_discord_adapter()
        text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        assert adapter.format_message(text) == text

    def test_heading_lists_and_single_paragraph_breaks_survive(self):
        adapter = _make_discord_adapter()
        text = "# Update\n\n\n- first\n\n\n\n- second\n\nParagraph."
        assert adapter.format_message(text) == "# Update\n\n- first\n\n- second\n\nParagraph."

    def test_formatting_is_idempotent(self):
        adapter = _make_discord_adapter()
        text = "| A | B |\n|---|---|\n| x | 1 |\n| y | 2 |\n\n\nDone."
        once = adapter.format_message(text)
        assert adapter.format_message(once) == once

    def test_formatted_table_remains_chunkable(self):
        adapter = _make_discord_adapter()
        text = "| Name | Score |\n|---|---|\n" + "\n".join(
            f"| player-{index} | {index} |" for index in range(20)
        )
        chunks = adapter.truncate_message(adapter.format_message(text), 80)
        assert len(chunks) > 1
        assert all(len(chunk) <= 80 for chunk in chunks)

    def test_empty_string(self):
        adapter = _make_discord_adapter()
        assert adapter.format_message("") == ""
