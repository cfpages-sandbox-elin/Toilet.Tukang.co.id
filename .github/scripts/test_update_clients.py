#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "update_clients.py"
if not SCRIPT.exists():
    SCRIPT = HERE.parent / "canonical" / "scripts" / "update_clients.py"
INSTALLED_CLIENTS = HERE.parent / "workflows" / "clients.yaml"
CLIENTS_FIXTURE = INSTALLED_CLIENTS if INSTALLED_CLIENTS.exists() else HERE.parent / "canonical" / "clients.yaml"
SPEC = importlib.util.spec_from_file_location("update_clients", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

OLD_PHONE = "0800 0000 0000"
NEW_PHONE = "0899 9999 9999"
OLD_NAME = "Old Fixture"
NEW_NAME = "New Fixture"
OLD_WA = "https://example.test/old-wa💬"
NEW_WA = "https://wa.me/620000000099"
OLD_TEL = "https://contact.example.test/old📞"
NEW_TEL = "tel:+620000000099"
OLD_ADDRESS = "Jl. Fixture Lama 1"
NEW_ADDRESS = "Jalan Fixture Baru 2"


def html(newline="\n", final=True):
    text = newline.join(
        [
            '<div class="whatsapp-floating">',
            f'  <a href="{OLD_WA}"><span>{OLD_PHONE} ({OLD_NAME})</span></a>',
            "</div>",
            '<div class="tlp-floating">',
            f'  <a href="{OLD_TEL}"><span>{OLD_PHONE} ({OLD_NAME})</span></a>',
            "</div>",
            f"<address>{OLD_ADDRESS}</address>",
        ]
    )
    return text + (newline if final else "")


def client(extra=()):
    values = [NEW_NAME, NEW_PHONE, NEW_WA, NEW_TEL, *extra]
    return "|".join(values) + "\n"


class ClientsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_fixture(self, relative="index.html", content=None, bom=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (html() if content is None else content).encode("utf-8")
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
        return path

    def run_cli(self, clients=".clients", dry_run=False):
        command = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--repo-root",
            str(self.root),
            "--clients",
            clients,
            "--summary",
            str(self.root / "summary.json"),
            "--paths-file",
            str(self.root / "paths.bin"),
            "--github-output",
            str(self.root / "output.txt"),
        ]
        if dry_run:
            command.append("--dry-run")
        return subprocess.run(command, text=True, capture_output=True)

    def test_empty_clients_is_noop_and_missing_fails_without_writes(self):
        target = self.write_fixture()
        original = target.read_bytes()
        (self.root / ".clients").write_bytes(b"")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(json.loads((self.root / "summary.json").read_text())["changed_count"], 0)
        self.assertEqual((self.root / "paths.bin").read_bytes(), b"")
        self.assertEqual((self.root / "output.txt").read_text(), "changed=false\n")
        (self.root / ".clients").unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_bytes(), original)

    def test_four_field_order_variants(self):
        fields = [NEW_NAME, NEW_PHONE, NEW_WA, NEW_TEL]
        for offset in range(4):
            path = self.root / f"c{offset}"
            path.write_text("|".join(fields[offset:] + fields[:offset]), encoding="utf-8")
            parsed = MODULE.parse_clients(path)
            self.assertEqual((parsed[0].name, parsed[0].phone), (NEW_NAME, NEW_PHONE))

    def test_five_and_six_field_records_and_category_filter(self):
        cases = [
            (OLD_ADDRESS, False, True),
            ("roof,-draft", True, False),
            ("roof:jakarta,-bogor;wall:depok", True, True),
        ]
        for index, (extra, has_filter, has_address) in enumerate(cases):
            path = self.root / f"p{index}"
            suffix = [extra]
            if has_address and has_filter:
                suffix.append(NEW_ADDRESS)
            path.write_text(client(suffix), encoding="utf-8")
            parsed = MODULE.parse_clients(path)[0]
            self.assertEqual(parsed.filter is not None, has_filter)
            self.assertEqual(parsed.address is not None, has_address)

    def test_malformed_and_ambiguous_records_are_sanitized(self):
        secret = "Private Fixture"
        path = self.root / ".clients"
        path.write_text(
            "|".join([secret, NEW_PHONE, NEW_WA, NEW_TEL, "roof:"]) + "\n",
            encoding="utf-8",
        )
        self.write_fixture()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("row 1", result.stderr)

    def test_markup_update_address_and_absent_address(self):
        first = self.write_fixture("first.html")
        (self.root / ".clients").write_text(client([NEW_ADDRESS]), encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        changed = first.read_text(encoding="utf-8")
        for value in (NEW_PHONE, NEW_NAME, NEW_WA, NEW_TEL, NEW_ADDRESS):
            self.assertIn(value, changed)
        self.assertNotIn(OLD_ADDRESS, changed)

        second_root = self.root / "without-address"
        second_root.mkdir()
        second = second_root / "index.html"
        second.write_text(html(), encoding="utf-8")
        (second_root / ".clients").write_text(client(), encoding="utf-8")
        summary = MODULE.update(second_root, second_root / ".clients", False)
        self.assertEqual(summary["changed_count"], 1)
        self.assertIn(OLD_ADDRESS, second.read_text(encoding="utf-8"))

    def test_filter_selects_category_city_and_default_remainder(self):
        selected = self.write_fixture("roof/jakarta/index.html")
        default = self.write_fixture("wall/surabaya/index.html")
        selected_values = "|".join(
            ["Filtered Fixture", "0877 7777 7777", "https://wa.me/620000000077", "tel:+620000000077", "roof:jakarta"]
        )
        (self.root / ".clients").write_text(selected_values + "\n" + client(), encoding="utf-8")
        summary = MODULE.update(self.root, self.root / ".clients", False)
        self.assertEqual(summary["changed_count"], 2)
        self.assertIn("Filtered Fixture", selected.read_text(encoding="utf-8"))
        self.assertIn(NEW_NAME, default.read_text(encoding="utf-8"))

    def test_plans_all_files_before_any_write(self):
        first = self.write_fixture("a.html")
        second = self.write_fixture(
            "b.html",
            html().replace(f'<a href="{OLD_TEL}"', '<a href="https://contact.example.test/other📞"')
            + '<div class="tlp-floating"><a href="tel:+621111111111"></a></div>',
        )
        original = first.read_bytes()
        (self.root / ".clients").write_text(client(), encoding="utf-8")
        with self.assertRaises(MODULE.ClientsError):
            MODULE.update(self.root, self.root / ".clients", False)
        self.assertEqual(first.read_bytes(), original)
        self.assertIn(OLD_PHONE, second.read_text(encoding="utf-8"))

    def test_bom_newlines_final_newline_and_mode_are_preserved(self):
        modes = {}
        for index, (bom, newline, final) in enumerate(
            [(False, "\n", True), (True, "\n", False), (False, "\r\n", True)]
        ):
            path = self.write_fixture(f"shape{index}.html", html(newline, final), bom)
            os.chmod(path, 0o640)
            modes[index] = stat.S_IMODE(path.stat().st_mode)
        (self.root / ".clients").write_text(client(), encoding="utf-8")
        MODULE.update(self.root, self.root / ".clients", False)
        for index, (bom, newline, final) in enumerate(
            [(False, "\n", True), (True, "\n", False), (False, "\r\n", True)]
        ):
            path = self.root / f"shape{index}.html"
            raw = path.read_bytes()
            self.assertEqual(raw.startswith(b"\xef\xbb\xbf"), bom)
            body = raw[3:] if bom else raw
            self.assertEqual(b"\r\n" in body, newline == "\r\n")
            self.assertEqual(body.endswith(newline.encode()), final)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), modes[index])

    def test_second_run_is_noop_and_outputs_do_not_leak_values(self):
        self.write_fixture()
        (self.root / ".clients").write_text(client([NEW_ADDRESS]), encoding="utf-8")
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual(summary["changed_count"], 0)
        captured = first.stdout + first.stderr + second.stdout + second.stderr + json.dumps(summary)
        for value in (NEW_NAME, NEW_PHONE, NEW_ADDRESS, NEW_WA, NEW_TEL):
            self.assertNotIn(value, captured)

    def test_dry_run_reports_plan_without_write(self):
        target = self.write_fixture()
        before = target.read_bytes()
        (self.root / ".clients").write_text(client(), encoding="utf-8")
        result = self.run_cli(dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(json.loads((self.root / "summary.json").read_text())["mode"], "dry-run")
        self.assertEqual((self.root / "paths.bin").read_bytes(), b"index.html\0")

    def test_verify_git_exact_scope_and_outside_dirty_poison(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        target = self.write_fixture()
        (self.root / ".clients").write_text(client(), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        MODULE.update(self.root, self.root / ".clients", False)
        paths = Path(self.temporary.name + ".paths")
        self.addCleanup(paths.unlink, missing_ok=True)
        paths.write_bytes(b"index.html\0")
        MODULE.verify_git(self.root, paths)
        before = target.read_bytes()
        (self.root / "outside.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaises(MODULE.ClientsError):
            MODULE.verify_git(self.root, paths)
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipIf(
 INSTALLED_CLIENTS.exists(),
        "already executing installed-layout check",
    )
    def test_installed_layout_execution_without_source_search_path(self):
        installed = self.root / "installed" / ".github" / "scripts"
        installed.mkdir(parents=True)
        shutil.copy2(SCRIPT, installed / "update_clients.py")
        shutil.copy2(Path(__file__), installed / "test_update_clients.py")
        workflows = installed.parent / "workflows"
        workflows.mkdir()
        shutil.copy2(
            Path(__file__).parents[1] / "canonical" / "clients.yaml",
            workflows / "clients.yaml",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = ""
        environment["PYTHONNOUSERSITE"] = "1"
        result = subprocess.run(
            [sys.executable, "-B", str(installed / "test_update_clients.py")],
            cwd=self.root / "installed",
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workflow_four_case_event_matrix(self):
        base = Path(__file__).parents[1]
        workflow = CLIENTS_FIXTURE.read_text(encoding="utf-8")
        expression = "github.event_name == 'workflow_dispatch' && (github.ref_type != 'branch' || inputs.dry_run != false)"
        self.assertIn(expression, workflow)
        cases = (
            ("push", "branch", True, False),
            ("workflow_dispatch", "branch", True, True),
            ("workflow_dispatch", "branch", False, False),
            ("workflow_dispatch", "tag", False, True),
        )
        for event, ref_type, dry_run, expected in cases:
            actual = event == "workflow_dispatch" and (ref_type != "branch" or dry_run is not False)
            self.assertEqual(actual, expected, (event, ref_type, dry_run))


if __name__ == "__main__":
    unittest.main()
