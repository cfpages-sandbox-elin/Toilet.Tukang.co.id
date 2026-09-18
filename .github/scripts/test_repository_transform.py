#!/usr/bin/env python3

from __future__ import annotations

import codecs
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ADJACENT_SCRIPT = Path(__file__).resolve().with_name("repository_transform.py")
SCRIPT = (
    ADJACENT_SCRIPT
    if ADJACENT_SCRIPT.exists()
    else Path(__file__).resolve().parents[1] / "canonical" / "scripts" / "repository_transform.py"
)
ADJACENT_WORKFLOW = Path(__file__).resolve().parent.parent / "workflows" / "replace.yaml"
WORKFLOW = (
    ADJACENT_WORKFLOW
    if ADJACENT_WORKFLOW.exists()
    else Path(__file__).resolve().parents[1] / "canonical" / "repository-search-and-replace.yml"
)
SPEC = importlib.util.spec_from_file_location("repository_transform", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def contact_spec(**overrides):
    value = {
        "whatsapp_url": "https://wa.me/628111542354",
        "telephone_url": "tel:+628111542354",
        "visible_number": "081 1154 2354",
        "message_new_tab": True,
        "require_visible_number_per_anchor": True,
    }
    value.update(overrides)
    return value


SAMPLE = (
    '<div class="whatsapp-floating"><a href="https://klik.example/💬-lead">'
    '<img src="/whatsapp-icon.png" alt="whatsapp"><span>0813 7045 7401 (Riky)</span></a></div>\n'
    '<div class="tlp-floating"><a href="https://klik.example/📞-lead">'
    '<img src="/phone-icon.png" alt="whatsapp"><span>0813 7045 7401 (Riky)</span></a></div>\n'
    '<a href="https://wa.me/620000000000">unrelated WhatsApp link</a>\n'
)


class ContactTests(unittest.TestCase):
    def test_contact_routes_are_structural_and_idempotent(self):
        transformed, counts = MODULE.transform_contact_text(SAMPLE, contact_spec())
        self.assertEqual(
            counts,
            {"whatsapp_anchors": 1, "telephone_anchors": 1, "visible_numbers": 2},
        )
        self.assertIn('href="https://wa.me/628111542354"', transformed)
        self.assertIn('target="_blank"', transformed)
        self.assertIn('rel="noopener noreferrer"', transformed)
        self.assertIn('href="tel:+628111542354"', transformed)
        self.assertIn('href="https://wa.me/620000000000"', transformed)
        self.assertEqual(transformed.count("081 1154 2354 (Riky)"), 2)
        second, _ = MODULE.transform_contact_text(transformed, contact_spec())
        self.assertEqual(second, transformed)

    def test_crlf_bom_and_final_newline_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            html_path = root / "index.html"
            original = codecs.BOM_UTF8 + SAMPLE.replace("\n", "\r\n").encode("utf-8")
            html_path.write_bytes(original)
            spec = contact_spec(
                include=["**/*.html"],
                exclude=[".git/**"],
                expected={
                    "matched_files": 1,
                    "whatsapp_anchors": 1,
                    "telephone_anchors": 1,
                    "visible_numbers": 2,
                },
            )
            changes, summary = MODULE.transform_contact_repository(root, spec)
            self.assertEqual(summary["matched_files"], 1)
            self.assertEqual(len(changes), 1)
            payload = changes[0][1]
            self.assertTrue(payload.startswith(codecs.BOM_UTF8))
            self.assertNotIn(b"\n", payload.replace(b"\r\n", b""))
            self.assertTrue(payload.endswith(b"\r\n"))

    def test_ambiguous_container_fails_closed(self):
        poisoned = SAMPLE.replace(
            'class="whatsapp-floating"',
            'class="whatsapp-floating tlp-floating"',
            1,
        )
        with self.assertRaisesRegex(MODULE.TransformError, "both message and telephone"):
            MODULE.transform_contact_text(poisoned, contact_spec())

    def test_multiple_anchors_fail_closed(self):
        poisoned = '<div class="tlp-floating"><a href="tel:1">1</a><a href="tel:2">2</a></div>'
        with self.assertRaisesRegex(MODULE.TransformError, "exactly one anchor"):
            MODULE.transform_contact_text(poisoned, contact_spec(require_visible_number_per_anchor=False))

    def test_icon_conflict_fails_closed(self):
        poisoned = SAMPLE.replace("/phone-icon.png", "/whatsapp-icon.png", 1)
        with self.assertRaisesRegex(MODULE.TransformError, "conflicts with its icon"):
            MODULE.transform_contact_text(poisoned, contact_spec())


class ExactTests(unittest.TestCase):
    def _root(self):
        context = tempfile.TemporaryDirectory()
        root = Path(context.name)
        (root / ".git").mkdir()
        return context, root

    def test_exact_mode_uses_counts_and_preserves_unselected_file(self):
        context, root = self._root()
        with context:
            (root / "a.html").write_text("old old\n", encoding="utf-8", newline="\n")
            (root / "keep.txt").write_text("old\n", encoding="utf-8", newline="\n")
            changes, summary = MODULE.transform_exact_repository(root, {
                "include": ["**/*.html"],
                "exclude": [".git/**"],
                "replacements": [{"search": "old", "replace": "new", "expected": 2}],
            })
            self.assertEqual(summary["replacement_counts"], [2])
            self.assertEqual([path.name for path, _ in changes], ["a.html"])
            self.assertEqual(changes[0][1], b"new new\n")
            self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "old\n")

    def test_exact_count_mismatch_fails_before_write(self):
        context, root = self._root()
        with context:
            path = root / "a.html"
            path.write_text("old\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.TransformError, "count mismatch"):
                MODULE.transform_exact_repository(root, {
                    "include": ["**/*.html"],
                    "replacements": [{"search": "old", "replace": "new", "expected": 2}],
                })
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_overlapping_tokens_are_rejected(self):
        context, root = self._root()
        with context:
            (root / "a.html").write_text("abc\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.TransformError, "overlapping"):
                MODULE.transform_exact_repository(root, {
                    "include": ["**/*.html"],
                    "replacements": [
                        {"search": "ab", "replace": "x", "expected": 1},
                        {"search": "abc", "replace": "y", "expected": 1},
                    ],
                })

    def test_invalid_utf8_is_rejected(self):
        context, root = self._root()
        with context:
            (root / "a.html").write_bytes(b"\xff")
            with self.assertRaisesRegex(MODULE.TransformError, "not strict UTF-8"):
                MODULE.transform_exact_repository(root, {
                    "include": ["**/*.html"],
                    "replacements": [{"search": "x", "replace": "y", "expected": 0}],
                })


class MetadataCardTests(unittest.TestCase):
    CARD = (
        '<div class="ue_post_grid_item">'
        '<a class="image-link" href="/toilet-portable-kupang">'
        '<div class="uc_post_image"><img post-id="7" src="https://old.example/old.jpg" '
        'alt="Wrong" title="Wrong"><div class="overlay"></div></div></a>'
        '<div class="uc_post_title"><a href="/toilet-portable-kupang">'
        '<div class="ue_p_title">Wrong title</div></a></div>'
        '<div class="uc_post_text">Wrong text</div>'
        '<a class="detail" href="/toilet-portable-kupang">Detail</a>'
        '</div>'
    )

    @staticmethod
    def spec():
        return {
            "landing_pages": ["portable/index.html"],
            "destinations": {
                "include": ["toilet-portable-*.html"],
                "exclude": [".git/**"],
                "title_suffix": " - Example",
                "allowed_image_hosts": ["blogger.googleusercontent.com"],
            },
            "card": {
                "container_class": "ue_post_grid_item",
                "title_class": "uc_post_title",
                "title_text_class": "ue_p_title",
                "text_class": "uc_post_text",
                "image_class": "uc_post_image",
                "expected_hrefs_per_card": 3,
                "source_markers": ["Wrong"],
            },
            "expected": {"landing_pages": 1, "cards": 1, "destinations": 1},
        }

    @staticmethod
    def destination(description_tag=None):
        description = (
            description_tag
            if description_tag is not None
            else '<meta name="description" content="Portable &amp; clean">'
        )
        return (
            "<html><head><title>Sewa Toilet Portable di Kupang - Example</title>"
            f"{description}"
            '<meta property="og:image" '
            'content="https://blogger.googleusercontent.com/img/toilet.jpg">'
            "</head><body>keep</body></html>"
        )

    def root(self):
        context = tempfile.TemporaryDirectory()
        root = Path(context.name)
        (root / ".git").mkdir()
        (root / "portable").mkdir()
        (root / "portable" / "index.html").write_text(
            f"<html><body>before{self.CARD}after</body></html>",
            encoding="utf-8",
            newline="\n",
        )
        (root / "toilet-portable-kupang.html").write_text(
            self.destination(),
            encoding="utf-8",
            newline="\n",
        )
        return context, root

    def test_metadata_cards_updates_complete_card_and_is_idempotent(self):
        context, root = self.root()
        with context:
            changes, summary, inventory = MODULE.transform_metadata_cards_repository(
                root, self.spec()
            )
            self.assertEqual(summary["cards"], 1)
            self.assertEqual(len(inventory), 1)
            self.assertEqual([path.relative_to(root).as_posix() for path, _ in changes], ["portable/index.html"])
            transformed = changes[0][1].decode("utf-8")
            self.assertIn("before", transformed)
            self.assertIn("after", transformed)
            self.assertIn("Sewa Toilet Portable di Kupang", transformed)
            self.assertIn("Portable &amp; clean", transformed)
            self.assertIn("https://blogger.googleusercontent.com/img/toilet.jpg", transformed)
            self.assertIn('alt="Sewa Toilet Portable di Kupang"', transformed)
            self.assertIn('title="Sewa Toilet Portable di Kupang"', transformed)
            self.assertIn('post-id="7"', transformed)
            self.assertEqual(transformed.count('href="/toilet-portable-kupang"'), 3)
            (root / "portable" / "index.html").write_bytes(changes[0][1])
            second, _, second_inventory = MODULE.transform_metadata_cards_repository(
                root, self.spec()
            )
            self.assertEqual(second, [])
            self.assertEqual(second_inventory, inventory)

    def test_missing_destination_description_fails_before_write(self):
        context, root = self.root()
        with context:
            landing = root / "portable" / "index.html"
            original = landing.read_bytes()
            (root / "toilet-portable-kupang.html").write_text(
                self.destination(""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MODULE.TransformError, "meta description"):
                MODULE.transform_metadata_cards_repository(root, self.spec())
            self.assertEqual(landing.read_bytes(), original)

    def test_incomplete_card_link_set_fails_closed(self):
        context, root = self.root()
        with context:
            landing = root / "portable" / "index.html"
            original = landing.read_text(encoding="utf-8")
            landing.write_text(
                original.replace(
                    '<a class="detail" href="/toilet-portable-kupang">Detail</a>',
                    "",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MODULE.TransformError, "exactly 3 times"):
                MODULE.transform_metadata_cards_repository(root, self.spec())

    def test_noncompliant_card_without_source_marker_fails_closed(self):
        context, root = self.root()
        with context:
            landing = root / "portable" / "index.html"
            landing.write_text(
                landing.read_text(encoding="utf-8").replace("Wrong", "Other"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MODULE.TransformError, "approved source marker"):
                MODULE.transform_metadata_cards_repository(root, self.spec())

    def test_directory_index_destination_route_is_supported(self):
        context, root = self.root()
        with context:
            nested = root / "portable-city" / "index.html"
            nested.parent.mkdir()
            nested.write_text(self.destination(), encoding="utf-8")
            (root / "toilet-portable-kupang.html").unlink()
            landing = root / "portable" / "index.html"
            landing.write_text(
                landing.read_text(encoding="utf-8").replace(
                    "/toilet-portable-kupang",
                    "/portable-city/",
                ),
                encoding="utf-8",
            )
            spec = self.spec()
            spec["destinations"]["include"] = ["portable-city/index.html"]
            changes, summary, inventory = MODULE.transform_metadata_cards_repository(root, spec)
            self.assertEqual(summary["destinations"], 1)
            self.assertEqual(inventory[0]["source_path"], "portable-city/index.html")
            self.assertIn('href="/portable-city/"', changes[0][1].decode("utf-8"))


class WorkflowTests(unittest.TestCase):
    def test_byte_preserving_modes_skip_legacy_whitespace_gate(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("TRANSFORM_MODE: ${{ inputs.mode }}", workflow)
        self.assertIn('if [[ "$TRANSFORM_MODE" == "contact-routes" ]]; then', workflow)
        self.assertIn("git diff --cached --check", workflow)
        self.assertIn("- metadata-cards", workflow)
        self.assertIn("--inventory-markdown", workflow)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
