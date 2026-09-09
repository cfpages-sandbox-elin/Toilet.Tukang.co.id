#!/usr/bin/env python3
"""Safely update checked-in HTML floating-contact markup from .clients."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ClientsError(Exception):
    pass


@dataclass(frozen=True)
class Filter:
    clauses: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]
    simple: tuple[str, ...] = ()


@dataclass(frozen=True)
class Client:
    row: int
    name: str
    phone: str
    whatsapp: str
    telephone: str
    filter: Filter | None
    address: str | None


FILTER_ATOM = r"-?[a-z0-9][a-z0-9._-]*"
SIMPLE_FILTER_RE = re.compile(rf"{FILTER_ATOM}(?:,{FILTER_ATOM})*\Z")
CATEGORY_FILTER_RE = re.compile(
    rf"{FILTER_ATOM}(?:,{FILTER_ATOM})*:{FILTER_ATOM}(?:,{FILTER_ATOM})*"
    rf"(?:;{FILTER_ATOM}(?:,{FILTER_ATOM})*:{FILTER_ATOM}(?:,{FILTER_ATOM})*)*\Z"
)
PHONE_RE = re.compile(r"\+?[0-9][0-9 ()+.\-]{6,}[0-9]\Z")
ADDRESS_RE = re.compile(r"(?:Jl\.|Jalan|Ruko|Perumahan)\s+[^<\r\n]+")
DIV_RE = re.compile(
    r"<div\b(?P<attrs>[^>]*\bclass\s*=\s*(?P<q>[\"'])(?P<class>.*?)(?P=q)[^>]*)>"
    r"(?P<body>.*?)</div\s*>",
    re.IGNORECASE | re.DOTALL,
)
HREF_RE = re.compile(r"\bhref\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
SPAN_RE = re.compile(r"<span\b[^>]*>([^<>]+)</span\s*>", re.IGNORECASE | re.DOTALL)
DISPLAY_RE = re.compile(r"\+?[0-9][0-9 ()+.\-]{6,}[0-9]\s*\([^<>\r\n()]+\)")
MARKERS = {"whatsapp-floating", "sms-floating", "tlp-floating"}


def _error(row: int, reason: str) -> ClientsError:
    return ClientsError(f"row {row}: {reason}")


def _valid_url(value: str) -> tuple[str, str]:
    split = urlsplit(value)
    if split.scheme == "tel":
        if not split.path:
            raise ValueError
    elif split.scheme not in {"http", "https"} or not split.netloc:
        raise ValueError
    return split.scheme.lower(), (split.hostname or "").lower()


def _route_role(value: str) -> str | None:
    try:
        scheme, host = _valid_url(value)
    except ValueError:
        return None
    lower = value.casefold()
    whatsapp = (
        "💬" in value
        or "whatsapp" in lower
        or host == "wa.me"
        or host.endswith(".whatsapp.com")
    )
    telephone = (
        scheme == "tel"
        or "📞" in value
        or "☎" in value
        or "telephone" in lower
        or "contact" in lower
    )
    if whatsapp and telephone:
        return "ambiguous"
    if whatsapp:
        return "whatsapp"
    if telephone:
        return "telephone"
    return None


def _phone(value: str) -> bool:
    if not PHONE_RE.fullmatch(value):
        return False
    digits = re.sub(r"\D", "", value)
    return 8 <= len(digits) <= 16


def _parse_filter(value: str) -> Filter | None:
    lower = value.casefold()
    if CATEGORY_FILTER_RE.fullmatch(lower):
        clauses = []
        for clause in lower.split(";"):
            categories, cities = clause.split(":")
            clauses.append((tuple(categories.split(",")), tuple(cities.split(","))))
        return Filter(tuple(clauses))
    if SIMPLE_FILTER_RE.fullmatch(lower) and value == lower:
        return Filter((), tuple(lower.split(",")))
    return None


def parse_clients(path: Path) -> list[Client]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ClientsError(".clients is missing") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ClientsError(".clients is not strict UTF-8") from None

    clients = []
    for row, source in enumerate(text.splitlines(), 1):
        if not source.strip():
            continue
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in source):
            raise _error(row, "unsafe control character")
        values = [part.strip() for part in source.split("|")]
        if any(not value for value in values):
            raise _error(row, "empty field")
        roles: dict[str, str] = {}
        remaining = []
        for value in values:
            route = _route_role(value)
            if route == "ambiguous":
                raise _error(row, "ambiguous route")
            if route:
                if route in roles:
                    raise _error(row, f"duplicate {route} route")
                roles[route] = value
            elif _phone(value):
                if "phone" in roles:
                    raise _error(row, "duplicate phone")
                roles["phone"] = value
            elif ADDRESS_RE.match(value):
                if "address" in roles:
                    raise _error(row, "duplicate address")
                roles["address"] = value
            else:
                remaining.append(value)

        filter_value = None
        names = []
        for value in remaining:
            parsed = _parse_filter(value)
            if parsed is not None:
                if filter_value is not None:
                    raise _error(row, "duplicate filter")
                filter_value = parsed
            elif any(mark in value for mark in (":", ",", ";")):
                raise _error(row, "malformed filter")
            else:
                names.append(value)
        if len(names) != 1:
            raise _error(row, "missing name" if not names else "extra unclassified field")
        for required in ("phone", "whatsapp", "telephone"):
            if required not in roles:
                raise _error(row, f"missing {required}")
        clients.append(
            Client(
                row,
                names[0],
                roles["phone"],
                roles["whatsapp"],
                roles["telephone"],
                filter_value,
                roles.get("address"),
            )
        )
    return clients


def _terms_match(terms: tuple[str, ...], path: str) -> bool:
    positives = tuple(term for term in terms if not term.startswith("-"))
    negatives = tuple(term[1:] for term in terms if term.startswith("-"))
    return (not positives or any(term in path for term in positives)) and not any(
        term in path for term in negatives
    )


def _filter_matches(rule: Filter, path: str) -> bool:
    path = path.casefold()
    if rule.simple:
        return _terms_match(rule.simple, path)
    for categories, cities in rule.clauses:
        negative = tuple(
            term[1:]
            for term in categories + cities
            if term.startswith("-")
        )
        if any(term in path for term in negative):
            continue
        positive_categories = tuple(term for term in categories if not term.startswith("-"))
        positive_cities = tuple(term for term in cities if not term.startswith("-"))
        if not positive_categories or not positive_cities:
            continue
        if any(
            category in path and city in path[path.index(category) + len(category) :]
            for category in positive_categories
            for city in positive_cities
        ):
            return True
    return False


def _html_paths(root: Path) -> list[Path]:
    paths = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name != ".git")
        base = Path(directory)
        for name in sorted(files):
            path = base / name
            if path.suffix.casefold() != ".html":
                continue
            if path.is_symlink():
                raise ClientsError("HTML symlink is not supported")
            paths.append(path)
    return paths


def _one(values: list[str], role: str, required: bool = True) -> str | None:
    unique = tuple(dict.fromkeys(values))
    if len(unique) > 1:
        raise ClientsError(f"matched HTML has ambiguous {role}")
    if not unique:
        if required:
            raise ClientsError(f"matched HTML is missing {role}")
        return None
    return unique[0]


def _transform(text: str, client: Client) -> tuple[bool, str]:
    blocks = []
    for match in DIV_RE.finditer(text):
        classes = set(match.group("class").casefold().split())
        markers = classes & MARKERS
        if markers:
            blocks.append((markers, match.group("body")))
    if not blocks:
        return False, text

    displays: list[str] = []
    whatsapp: list[str] = []
    telephone: list[str] = []
    for markers, body in blocks:
        for span in SPAN_RE.findall(body):
            displays.extend(DISPLAY_RE.findall(span))
        hrefs = [href for _, href in HREF_RE.findall(body)]
        if markers & {"whatsapp-floating", "sms-floating"}:
            whatsapp.extend(hrefs)
        if "tlp-floating" in markers:
            telephone.extend(hrefs)

    old_display = _one(displays, "displayed phone/name")
    old_whatsapp = _one(whatsapp, "WhatsApp route")
    old_telephone = _one(telephone, "telephone route")
    replacements = [
        (old_display, f"{client.phone} ({client.name})"),
        (old_whatsapp, client.whatsapp),
        (old_telephone, client.telephone),
    ]
    if client.address is not None:
        old_address = _one(ADDRESS_RE.findall(text), "address")
        replacements.append((old_address, client.address))

    result = text
    for old, new in replacements:
        if old != new:
            result = result.replace(old, new)
    return True, result


def _encode_like(raw: bytes, text: str) -> bytes:
    bom = raw.startswith(b"\xef\xbb\xbf")
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise ClientsError("path escaped repository root") from None


def update(root: Path, clients_path: Path, dry_run: bool) -> dict[str, object]:
    root = root.resolve()
    clients = parse_clients(clients_path)
    unfiltered = [client for client in clients if client.filter is None]
    if len(unfiltered) > 1:
        raise ClientsError("multiple unfiltered clients are ambiguous")

    plans: list[tuple[Path, bytes, int]] = []
    scanned = matched = 0
    for path in _html_paths(root):
        scanned += 1
        relative = _relative(path.resolve(), root)
        selected = [
            client
            for client in clients
            if client.filter is not None and _filter_matches(client.filter, relative)
        ]
        if len(selected) > 1:
            raise ClientsError("HTML path matches multiple client filters")
        client = selected[0] if selected else (unfiltered[0] if unfiltered else None)
        if client is None:
            continue
        raw = path.read_bytes()
        try:
            text = raw[3:].decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ClientsError("matched HTML is not strict UTF-8") from None
        recognized, transformed = _transform(text, client)
        if not recognized:
            continue
        matched += 1
        output = _encode_like(raw, transformed)
        if output != raw:
            plans.append((path, output, stat.S_IMODE(path.stat().st_mode)))

    if not dry_run:
        for path, output, mode in plans:
            _atomic_write(path, output, mode)
    changed_paths = sorted(_relative(path, root) for path, _, _ in plans)
    return {
        "mode": "dry-run" if dry_run else "update",
        "client_count": len(clients),
        "scanned_count": scanned,
        "matched_count": matched,
        "changed_count": len(changed_paths),
        "changed_paths": changed_paths,
    }


def _read_paths(path: Path) -> list[str]:
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ClientsError("paths file is unreadable strict UTF-8") from None
    values = [value for value in text.split("\0") if value]
    if values != sorted(set(values)):
        raise ClientsError("paths file is not sorted and unique")
    for value in values:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in value:
            raise ClientsError("paths file contains unsafe path")
    return values


def verify_git(root: Path, paths_file: Path) -> None:
    expected = set(_read_paths(paths_file))
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise ClientsError("git status failed")
    entries = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    observed: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise ClientsError("git status returned malformed data")
        status_code, path = entry[:2], entry[3:]
        observed.add(path.replace("\\", "/"))
        if "R" in status_code or "C" in status_code:
            index += 1
    if observed != expected:
        raise ClientsError("git changes are outside planned paths")


def _write_outputs(summary: dict[str, object], summary_path: Path, paths_path: Path) -> None:
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    paths = summary["changed_paths"]
    paths_path.write_bytes(("".join(f"{path}\0" for path in paths)).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--clients", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--paths-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-git", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.repo_root.resolve()
        if args.verify_git:
            verify_git(root, args.paths_file)
            return 0
        if args.clients is None or args.summary is None:
            parser.error("--clients and --summary are required for update mode")
        clients = args.clients if args.clients.is_absolute() else root / args.clients
        summary = update(root, clients, args.dry_run)
        _write_outputs(summary, args.summary, args.paths_file)
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"changed={'true' if summary['changed_count'] else 'false'}\n")
        return 0
    except ClientsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
