"""Local preparation has no publication authority."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from functools import partial
import subprocess
import sys

run_cli = partial(subprocess.run, capture_output=True, text=True, timeout=120)


class ReleasePreparationTests(unittest.TestCase):
    def test_stamps_once_when_unreleased_has_notes(self):
        # Given
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            changelog.write_text('# Changelog\n\n## Unreleased\n\n- fixture\n', encoding='utf-8')
            notes = root / 'notes.md'
            # When
            result = run_cli([sys.executable, '-P', '-m', 'omh.cli', 'release', 'prepare', '--version', '2.0.4', '--repo-root', str(root), '--notes-file', str(notes), '--json'])
            # Then
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(notes.read_bytes(), b'- fixture\n')
            self.assertEqual(changelog.read_text().count('## Unreleased\n'), 1)
            self.assertEqual(changelog.read_text().count('## 2.0.4 - '), 1)

    def test_stamps_utc_date_when_pure_preparation_runs(self):
        from datetime import date
        from omh.maintenance.changelog import stamp_changelog
        # Given
        history = b'## 2.0.3 - 2026-09-12\r\n\r\n- old\r\n'
        original = b'# Changelog\r\n\r\n## Unreleased\r\n\r\n- fixture\r\n\r\n' + history
        # When
        stamped, notes = stamp_changelog(original, '2.0.4', date(2026, 9, 13))
        # Then
        self.assertEqual(stamped, b'# Changelog\r\n\r\n## Unreleased\r\n\r\n## 2.0.4 - 2026-09-13\r\n\r\n- fixture\r\n\r\n' + history)
        self.assertEqual(notes, b'- fixture\n')

    def test_preserves_stamp_when_same_target_is_prepared_again(self):
        from datetime import date
        from omh.maintenance.changelog import stamp_changelog
        # Given
        original = b'## Unreleased\n\n## 2.0.4 - 2026-09-13\n\n- fixture\n'
        # When
        stamped, notes = stamp_changelog(original, '2.0.4', date(2030, 1, 1))
        # Then
        self.assertEqual(stamped, original)
        self.assertEqual(notes, b'- fixture\n')

    def test_recovers_notes_when_write_was_interrupted_after_stamp(self):
        from omh.maintenance.release_notes import prepare_notes
        from unittest.mock import patch
        from omh.system.local_store import atomic_write_text
        # Given
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            changelog.write_text('## Unreleased\n\n- fixture\n')
            notes = root / 'notes.md'
            def interrupt(path, text):
                if path.resolve() == notes.resolve():
                    raise OSError('injected notes write failure')
                atomic_write_text(path, text)
            with patch('omh.maintenance.release_notes.atomic_write_text', side_effect=interrupt):
                with self.assertRaises(OSError):
                    prepare_notes(root, '2.0.4', notes, stamp=True)
            stamped = changelog.read_bytes()
            # When
            prepare_notes(root, '2.0.4', notes, stamp=True)
            # Then
            self.assertEqual(changelog.read_bytes(), stamped)
            self.assertEqual(notes.read_bytes(), b'- fixture\n')

    def test_refuses_before_stamp_when_notes_basename_is_unsafe(self):
        from omh.maintenance.release_notes import prepare_notes
        from omh.maintenance.changelog import ChangelogError
        # Given
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            original = b'## Unreleased\n\n- fixture\n'
            changelog.write_bytes(original)
            # When
            with self.assertRaises(ChangelogError):
                prepare_notes(root, '2.0.4', root / 'bad:notes.md', stamp=True)
            # Then
            self.assertEqual(changelog.read_bytes(), original)

    def test_refuses_without_writes_when_preparation_is_invalid(self):
        # Given
        for source in ('# Changelog\n', '## Unreleased - invalid\n\n- fixture\n', '## Unreleased\n\n', '## Unreleased\n\n- a\n## Unreleased\n\n- b\n',
                       '## Unreleased\n\n- new\n## 2.0.4 - 2026-09-13\n\n- old\n',
                       '## Unreleased\n\n## 2.0.4 - 2026-09-13\n- a\n## 2.0.4 - 2026-09-14\n- b\n'):
            with self.subTest(source=source), TemporaryDirectory() as tmp:
                root = Path(tmp)
                changelog = root / 'CHANGELOG.md'
                changelog.write_text(source)
                notes = root / 'notes.md'
                notes.write_bytes(b'original notes\n')
                # When
                result = run_cli([sys.executable, '-P', '-m', 'omh.cli', 'release', 'prepare', '--version', '2.0.4', '--repo-root', str(root), '--notes-file', str(notes), '--json'])
                # Then
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(changelog.read_text(), source)
                self.assertEqual(notes.read_bytes(), b'original notes\n')
