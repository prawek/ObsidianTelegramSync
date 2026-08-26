import io
import unittest
import zipfile

from markdown_parser import (
    ConversionContext,
    convert_to_telegram_html,
    parse_note,
    resolve_wikilink,
)
from vault_parser import parse_vault_zip_bytes


class WikilinkParsingTests(unittest.TestCase):
    def test_plain_wikilink_keeps_the_full_target_and_becomes_a_link(self):
        note = parse_note("source", "See [[Meeting]].")

        self.assertEqual(note.wikilinks, ["Meeting"])
        rendered = convert_to_telegram_html(
            note,
            ConversionContext(note_link_map={"meeting": "https://t.me/c/1/2/3"}),
        )

        self.assertEqual(
            rendered,
            ['See <a href="https://t.me/c/1/2/3">Meeting</a>.'],
        )

    def test_caption_and_heading_are_parsed_separately(self):
        note = parse_note("source", "[[Projects/Plan#Next steps|the plan]]")

        self.assertEqual(note.wikilinks, ["Projects/Plan"])
        rendered = convert_to_telegram_html(
            note,
            ConversionContext(
                note_link_map={"projects/plan": "https://t.me/c/1/2/3"}
            ),
        )

        self.assertEqual(
            rendered,
            ['<a href="https://t.me/c/1/2/3">the plan</a>'],
        )

    def test_relative_target_resolves_from_the_source_note(self):
        context = ConversionContext(
            note_link_map={"projects/overview": "https://t.me/c/1/2/3"},
            source_note_path="projects/weekly/status.md",
        )

        self.assertEqual(
            resolve_wikilink("../overview", context),
            "https://t.me/c/1/2/3",
        )

    def test_zip_upload_is_parsed_in_memory(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("My vault/Ideas/Plan.md", "# Plan")

        vault = parse_vault_zip_bytes(archive.getvalue(), "upload")

        self.assertEqual(vault.vault_name, "My vault")
        self.assertEqual(vault.notes[0].relative_path, "Ideas/Plan.md")

    def test_task_items_are_not_rendered_as_plain_bullets(self):
        note = parse_note("tasks", "- [x] Done\n- [ ] Later")

        rendered = convert_to_telegram_html(note, ConversionContext())

        self.assertEqual(rendered, ["✅ Done\n☐ Later"])


if __name__ == "__main__":
    unittest.main()
