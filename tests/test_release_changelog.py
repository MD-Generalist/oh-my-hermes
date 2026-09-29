"""Changelog extraction through the existing release command boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from functools import partial
import subprocess
import sys

run_cli = partial(subprocess.run, capture_output=True, text=True, timeout=120)


class ReleaseChangelogTests(unittest.TestCase):
    def test_extracts_exact_markdown_when_version_is_stamped(self):
        # Given
        body = '- fixture $HOME "quotes"\n\n```md\n## Unreleased\n```\n'
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            original = '# Changelog\n\n## Unreleased\n\n## 2.0.4 - 2026-09-13\n\n' + body
            changelog.write_text(original, encoding='utf-8')
            notes = root / 'notes.md'
            # When
            result = run_cli([sys.executable, '-P', '-m', 'omh.cli', 'release', 'notes', '--version', '2.0.4', '--repo-root', str(root), '--notes-file', str(notes), '--json'])
            # Then
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(notes.read_bytes(), body.encode())
            self.assertEqual(changelog.read_text(), original)

    def test_pure_parser_preserves_interior_when_markdown_is_special(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        body = '### Changed\n\n- $HOME "quotes" \\ escapes\n\n~~~md\n## 2.0.4 - 1999-01-01\n~~~\n\n```md\n## Unreleased\n```\n\n- caf\u00e9 \u2603  \n'
        raw = ('## Unreleased\n\n## 2.0.4 - 2026-09-13\n\n' + body + '\n## 2.0.3 - 2026-09-12\n\n- old\n').encode()
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, body.encode())

    def test_pure_parser_normalizes_crlf_when_artifact_is_extracted(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        raw = b'## 2.0.4 - 2026-09-13\r\n\r\n- first\r\n\r\n- second  \r\n\r\n'
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, b'- first\n\n- second  \n')

    def test_preserves_unicode_separators_when_they_are_not_markdown_newlines(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        body = 'prefix\u2028## 2.0.3 - 2026-09-12\n\n- still target content\n'
        raw = ('## 2.0.4 - 2026-09-13\n\n' + body).encode()
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, body.encode())

    def test_refuses_stamp_when_output_would_exceed_parser_bound(self):
        from datetime import date
        from omh.maintenance.changelog import ChangelogError, MAX_CHANGELOG_BYTES, stamp_changelog
        # Given
        tail = b'\n## Unreleased\n\n- fixture\n'
        raw = b'x' * (MAX_CHANGELOG_BYTES - len(tail)) + tail
        # When / Then
        with self.assertRaises(ChangelogError):
            stamp_changelog(raw, '2.0.4', date(2026, 9, 13))

    def test_refuses_invalid_section_when_source_is_malformed(self):
        from omh.maintenance.changelog import ChangelogError, extract_notes
        # Given
        cases = [b'## Unreleased\n\n- pending\n', b'## 2.0.4 - 2026-09-13\n\n',
                 b'## 2.0.4 - 2026-09-13\n\n- a\n## 2.0.4 - 2026-09-14\n\n- b\n',
                 b'## 2.0.4 - 2026-02-30\n\n- a\n', b'\xff', b'x' * (2 * 1024 * 1024 + 1),
                 b'## 2.0.4 - 2026-09-13\n\n' + b'a' * (256 * 1024 + 1)]
        for raw in cases:
            with self.subTest(size=len(raw)):
                # When / Then
                with self.assertRaises(ChangelogError):
                    extract_notes(raw, '2.0.4')

    def test_refuses_bad_version_when_target_is_untrusted(self):
        from omh.maintenance.changelog import ChangelogError, extract_notes
        # Given
        for version in ('', '../2.0.4', '2.0.4\n', '2.04.0', '2.0.4-beta'):
            with self.subTest(version=version):
                # When / Then
                with self.assertRaises(ChangelogError):
                    extract_notes(b'## 2.0.4 - 2026-09-13\n\n- a\n', version)


class ReleaseBodyBoundTests(unittest.TestCase):
    """The published body cannot exceed what GitHub accepts.

    The 2.0.4 cut tagged and pushed, then failed at publication on
    `HTTP 422 ... body is too long (maximum is 125000 characters)` with a
    228,212-byte section; nothing was released and npm and Homebrew were
    skipped. `MAX_NOTES_BYTES` is 262,144, so the bound that existed was
    twice the surface's and could not have caught it.
    """

    @staticmethod
    def _entry(index, filler=''):
        return f'- **Entry {index}.** body {index}{filler}\n\n'

    def _section(self, count, filler=''):
        return ''.join(self._entry(i, filler) for i in range(1, count + 1)).encode()

    def test_a_section_inside_the_bound_is_returned_byte_for_byte(self):
        from omh.maintenance.changelog import bound_release_body
        # Given
        section = self._section(20)
        # When
        result = bound_release_body(section, '2.0.4')
        # Then the ordinary release keeps publishing exactly its authored notes.
        self.assertIs(result, section)

    def test_an_oversized_section_keeps_a_byte_exact_prefix_of_its_entries(self):
        from omh.maintenance.changelog import (
            MAX_RELEASE_BODY_UNITS, bound_release_body, release_body_units,
        )
        # Given a section past the bound by a wide margin.
        section = self._section(400, filler=' ' + 'x' * 400)
        self.assertGreater(release_body_units(section.decode()), MAX_RELEASE_BODY_UNITS)
        # When
        result = bound_release_body(section, '2.0.4').decode()
        # Then: inside the bound, and what it kept is the original's own bytes
        # rather than a reflow of them -- the trailer is the only new text.
        self.assertLessEqual(release_body_units(result), MAX_RELEASE_BODY_UNITS)
        head, trailer = result.split('\n_Bounded for publication:', 1)
        self.assertTrue(section.decode().startswith(head), 'kept region is not a prefix')
        self.assertEqual(trailer.count('_Bounded for publication:'), 0)

    def test_the_trailer_counts_are_derived_from_the_split_not_fixed(self):
        from omh.maintenance.changelog import bound_release_body
        # Given two oversized sections whose entry counts differ.
        results = {}
        for count in (400, 600):
            body = bound_release_body(self._section(count, filler=' ' + 'x' * 400), '2.0.4').decode()
            kept = body.count('- **Entry ')
            results[count] = (kept, body)
        # Then each trailer states its own totals, and kept + remaining is the
        # total it came from -- the arithmetic is the assertion, not a literal.
        for total, (kept, body) in results.items():
            self.assertIn(f'the first {kept} of {total} entries', body)
            self.assertIn(f'The remaining {total - kept} are in', body)
        self.assertNotEqual(results[400][0], 0)

    def test_one_entry_larger_than_the_bound_refuses_instead_of_splitting_it(self):
        from omh.maintenance.changelog import ChangelogError, bound_release_body
        # Given a single entry that cannot fit however it is trimmed.
        section = ('- **Huge.** ' + 'x' * 200_000 + '\n').encode()
        # When / Then: truncating inside an entry would publish half a claim.
        with self.assertRaises(ChangelogError) as caught:
            bound_release_body(section, '2.0.4')
        self.assertEqual(caught.exception.code, 'notes_entry_too_large')

    def test_the_length_is_counted_in_utf16_units_not_code_points(self):
        from omh.maintenance.changelog import release_body_units
        # Given one astral character, which GitHub may count as two.
        # Then the wider reading is the one the bound uses.
        self.assertEqual(release_body_units('\U0001f600'), 2)
        self.assertEqual(len('\U0001f600'), 1)

    def test_the_cli_writes_a_publishable_body_and_leaves_the_changelog_whole(self):
        # Given a stamped changelog whose section is past the bound.
        section = ''.join(f'- **Entry {i}.** body {i} ' + 'x' * 400 + '\n\n' for i in range(1, 401))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            original = '# Changelog\n\n## Unreleased\n\n## 2.0.4 - 2026-09-21\n\n' + section
            changelog.write_text(original, encoding='utf-8')
            notes = root / 'notes.md'
            # When
            result = run_cli([sys.executable, '-P', '-m', 'omh.cli', 'release', 'notes',
                              '--version', '2.0.4', '--repo-root', str(root),
                              '--notes-file', str(notes), '--json'])
            # Then the artifact is publishable and the record is untouched.
            self.assertEqual(result.returncode, 0, result.stderr)
            body = notes.read_text(encoding='utf-8')
            self.assertLessEqual(len(body.encode('utf-16-le')) // 2, 125_000)
            self.assertIn('_Bounded for publication:', body)
            self.assertEqual(changelog.read_text(encoding='utf-8'), original)

    def test_evidence_accepts_the_bounded_body_the_notes_step_wrote(self):
        from omh.maintenance.changelog import ChangelogError
        from omh.maintenance.release import require_notes_from_source
        from omh.maintenance.release_notes import prepare_notes
        # Given an oversized section, written by the same step the release
        # workflow runs before the evidence bundle (v3.0.0's first run).
        section = ''.join(f'- **Entry {i}.** body {i} ' + 'x' * 400 + '\n\n' for i in range(1, 401))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'CHANGELOG.md').write_text(
                '# Changelog\n\n## Unreleased\n\n## 2.0.4 - 2026-09-21\n\n' + section, encoding='utf-8')
            notes = root / 'notes.md'
            prepare_notes(root, '2.0.4', notes)
            self.assertIn('_Bounded for publication:', notes.read_text(encoding='utf-8'))
            # When / Then the trimmed artifact is the derived body, not a mismatch.
            require_notes_from_source(root, notes, '2.0.4')
            # And an edit to that artifact is still refused.
            notes.write_bytes(notes.read_bytes() + b'forged\n')
            with self.assertRaises(ChangelogError) as caught:
                require_notes_from_source(root, notes, '2.0.4')
            self.assertEqual(caught.exception.code, 'notes_source_mismatch')
