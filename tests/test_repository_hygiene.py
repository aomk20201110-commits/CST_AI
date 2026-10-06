"""Repository hygiene: nothing in this tree is a secret, a binary, or someone
else's artefact.

The checks are deliberately blunt.  A public repository is a place where a single
committed file is enough to leak a credential or a licensed binary, so the scan
covers every file, and the needles are assembled at runtime so that this file does
not match itself.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__"}

FORBIDDEN_SUFFIXES = {
    ".cst", ".lok", ".ffm", ".fme", ".feh", ".sat", ".step", ".igs", ".mod", ".his",
    ".pdf", ".mcr", ".zip", ".tar", ".gz", ".7z",
    ".dll", ".exe", ".so", ".pyd", ".bin", ".o", ".obj",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
    ".bak", ".orig", ".rej", ".swp", ".tmp",
}

FORBIDDEN_DIRS = ("reports", "ledger", "runs", "artifacts", "model_ledger", ".tmp")

#: a credential in a public repository is one of these, whatever it is called
SECRET_PATTERNS = (
    r"ghp" + r"_[A-Za-z0-9]{20,}",
    r"github" + r"_pat_[A-Za-z0-9_]{20,}",
    r"AKIA" + r"[0-9A-Z]{16}",
    r"-----BEGIN" + r"[A-Z ]*PRIVATE KEY" + r"-----",
    r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']",
    r"(?i)\bpassword\b\s*[:=]\s*[\"'][^\"']{3,}[\"']",
)


def published_files() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("*")
        if p.is_file() and not (set(p.parts) & SKIP_DIRS)
    )


class TestNoBinaries(unittest.TestCase):
    def test_no_forbidden_file_types(self):
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for p in published_files()
            if p.suffix.lower() in FORBIDDEN_SUFFIXES
        ]

        self.assertEqual(offenders, [], f"CST projects, results or binaries found: {offenders}")

    def test_no_file_is_a_binary_blob(self):
        offenders = []

        for path in published_files():
            data = path.read_bytes()[:4096]

            if b"\x00" in data:
                offenders.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual(offenders, [], f"binary content found: {offenders}")


class TestNoInternalDirectories(unittest.TestCase):
    def test_internal_artefact_directories_are_absent(self):
        present = [name for name in FORBIDDEN_DIRS if (REPO_ROOT / name).exists()]

        self.assertEqual(present, [], f"internal directories present: {present}")

    def test_run_stores_are_not_committed(self):
        stray = [
            str(p.relative_to(REPO_ROOT))
            for p in published_files()
            if "state.json" in p.name or p.name == "events.jsonl"
        ]

        self.assertEqual(stray, [], f"run-store files present: {stray}")


class TestNoSecrets(unittest.TestCase):
    def test_no_credential_shaped_strings(self):
        offenders = []

        for path in published_files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue

            for pattern in SECRET_PATTERNS:
                match = re.search(pattern, text)

                if match:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}: {match.group(0)[:40]!r}"
                    )

        self.assertEqual(offenders, [], f"possible credentials found: {offenders}")

    def test_no_email_addresses_are_published_unnecessarily(self):
        """A personal address belongs in a commit identity, not in every file."""

        pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        allow = ("example.com", "users.noreply.github.com")
        offenders = []

        for path in published_files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue

            for match in re.findall(pattern, text):
                if not any(a in match for a in allow):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {match}")

        self.assertEqual(offenders, [], f"unexpected addresses found: {offenders}")


class TestNoEnvironmentLeaks(unittest.TestCase):
    def test_no_developer_home_or_user_profile_paths(self):
        needles = (
            "C:" + "\\" + "Users" + "\\",
            "C:/" + "Users" + "/",
            "/home" + "/",
            "/Users" + "/",
            "Alien" + "ware",
        )
        offenders = []

        for path in published_files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue

            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")

        self.assertEqual(offenders, [], f"developer paths found: {offenders}")


if __name__ == "__main__":
    unittest.main()
