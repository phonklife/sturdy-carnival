import json
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

import generator


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        content_length: str | None = None,
        final_url: str = "https://example.com/tracks",
    ) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}
        self._final_url = final_url
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]

    def geturl(self) -> str:
        return self._final_url


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def open(self, source_url: str, timeout: int = 30) -> FakeResponse:
        return self._response


class GeneratorTests(unittest.TestCase):
    def test_load_source_payload_rejects_non_http_scheme(self) -> None:
        with self.assertRaisesRegex(ValueError, "http or https"):
            generator.load_source_payload(None, "file:///tmp/source.json")

    def test_load_source_payload_requires_json_response(self) -> None:
        with patch(
            "generator.build_opener",
            return_value=FakeOpener(FakeResponse(b"<html></html>", content_type="text/html")),
        ):
            with self.assertRaisesRegex(ValueError, "JSON response"):
                generator.load_source_payload(None, "https://example.com/tracks")

    def test_load_source_payload_rejects_large_response(self) -> None:
        huge_length = str(generator.MAX_SOURCE_BYTES + 1)
        with patch(
            "generator.build_opener",
            return_value=FakeOpener(
                FakeResponse(b"{}", content_type="application/json", content_length=huge_length)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                generator.load_source_payload(None, "https://example.com/tracks")

    def test_load_source_payload_rejects_non_http_redirect_target(self) -> None:
        with patch(
            "generator.build_opener",
            return_value=FakeOpener(
                FakeResponse(
                    b"{}",
                    content_type="application/json",
                    final_url="file:///tmp/source.json",
                )
            ),
        ):
            with self.assertRaisesRegex(ValueError, "redirect must remain"):
                generator.load_source_payload(None, "https://example.com/tracks")

    def test_redirect_handler_rejects_non_http_redirect_target(self) -> None:
        handler = generator.SafeRedirectHandler()
        request = Request("https://example.com/tracks")

        with self.assertRaisesRegex(ValueError, "redirect must remain"):
            handler.redirect_request(request, None, 302, "Found", {}, "file:///tmp/source.json")

    def test_load_and_import_from_file_payload(self) -> None:
        payload = {
            "tracks": [
                {
                    "id": "song-001",
                    "title": "Solar Echo",
                    "artist": "virtualluser",
                    "tags": ["ambient", "synthwave"],
                    "url": "https://example.com/tracks/song-001",
                    "audio_url": "https://example.com/audio/song-001.mp3",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.json"
            database_path = Path(temp_dir) / "archive.db"
            source_path.write_text(json.dumps(payload), encoding="utf-8")

            loaded_payload = generator.load_source_payload(str(source_path), None)
            imported = generator.import_tracks(
                database_path=str(database_path),
                payload=loaded_payload,
                root_key="tracks",
                default_artist="fallback-artist",
            )

            self.assertEqual(imported, 1)

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT source_id, title, artist, status, tags_json FROM tracks"
                ).fetchone()

            self.assertEqual(
                row,
                (
                    "song-001",
                    "Solar Echo",
                    "virtualluser",
                    "pending",
                    '["ambient", "synthwave"]',
                ),
            )

    def test_upsert_updates_existing_track(self) -> None:
        first_payload = [
            {
                "id": "song-002",
                "title": "Night Signal",
                "tags": "electronic, cinematic",
                "url": "https://example.com/tracks/song-002",
                "audio_url": "https://example.com/audio/song-002.mp3",
            }
        ]
        second_payload = [
            {
                "id": "song-002",
                "title": "Night Signal (Archive Cut)",
                "artist": "guest",
                "status": "archived",
                "archived_at": "2026-09-05T10:00:00+00:00",
                "tags": ["electronic"],
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT title, artist, source_url, audio_url, status, archived_at, tags_json
                    FROM tracks
                    WHERE source_id = 'song-002'
                    """
                ).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

            self.assertEqual(count, 1)
            self.assertEqual(
                row,
                (
                    "Night Signal (Archive Cut)",
                    "guest",
                    "https://example.com/tracks/song-002",
                    "https://example.com/audio/song-002.mp3",
                    "archived",
                    "2026-09-05T10:00:00+00:00",
                    '["electronic"]',
                ),
            )

    def test_archived_at_implies_archived_status(self) -> None:
        record = generator.normalize_track(
            {
                "id": "song-003",
                "title": "Dawn Pulse",
                "archived_at": "2026-09-05T10:00:00+00:00",
            },
            "virtualluser",
        )

        self.assertEqual(record.status, "archived")

    def test_archived_at_overrides_conflicting_status(self) -> None:
        record = generator.normalize_track(
            {
                "id": "song-004",
                "title": "Orbit Fade",
                "status": "pending",
                "archived_at": "2026-09-05T10:00:00+00:00",
            },
            "virtualluser",
        )

        self.assertEqual(record.status, "archived")

    def test_missing_tags_preserve_existing_tags(self) -> None:
        first_payload = [
            {
                "id": "song-005",
                "title": "Crystal Run",
                "tags": ["retro", "night"],
            }
        ]
        second_payload = [
            {
                "id": "song-005",
                "title": "Crystal Run v2",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT title, tags_json FROM tracks WHERE source_id = 'song-005'"
                ).fetchone()

            self.assertEqual(row, ("Crystal Run v2", '["retro", "night"]'))

    def test_invalid_late_record_does_not_rollback_prior_imports(self) -> None:
        payload = [
            {"id": "song-006", "title": "First Light"},
            {"id": "song-007"},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            with self.assertRaisesRegex(ValueError, "missing a title"):
                generator.import_tracks(
                    database_path=str(database_path),
                    payload=payload,
                    root_key="tracks",
                    default_artist="virtualluser",
                )

            with sqlite3.connect(database_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

            self.assertEqual(count, 1)

    def test_missing_artist_and_status_preserve_existing_values(self) -> None:
        first_payload = [
            {
                "id": "song-008",
                "title": "Signal Bloom",
                "artist": "original-artist",
                "status": "archived",
                "archived_at": "2026-09-05T10:00:00+00:00",
            }
        ]
        second_payload = [
            {
                "id": "song-008",
                "title": "Signal Bloom Remaster",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT title, artist, status, archived_at
                    FROM tracks
                    WHERE source_id = 'song-008'
                    """
                ).fetchone()

            self.assertEqual(
                row,
                (
                    "Signal Bloom Remaster",
                    "original-artist",
                    "archived",
                    "2026-09-05T10:00:00+00:00",
                ),
            )

    def test_explicit_null_optional_fields_clear_existing_values(self) -> None:
        first_payload = [
            {
                "id": "song-009",
                "title": "Clear Skies",
                "url": "https://example.com/tracks/song-009",
                "audio_url": "https://example.com/audio/song-009.mp3",
                "archived_at": "2026-09-05T10:00:00+00:00",
            }
        ]
        second_payload = [
            {
                "id": "song-009",
                "title": "Clear Skies",
                "source_url": "",
                "audio_url": None,
                "archived_at": "",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT source_url, audio_url, archived_at
                    FROM tracks
                    WHERE source_id = 'song-009'
                    """
                ).fetchone()

            self.assertEqual(row, (None, None, None))

    def test_explicit_empty_tags_clear_existing_tags(self) -> None:
        first_payload = [
            {
                "id": "song-010",
                "title": "Tag Reset",
                "tags": ["one", "two"],
            }
        ]
        second_payload = [
            {
                "id": "song-010",
                "title": "Tag Reset",
                "tags": [],
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT tags_json FROM tracks WHERE source_id = 'song-010'"
                ).fetchone()

            self.assertEqual(row, ('[]',))


class ParseArgsTests(unittest.TestCase):
    """CLI argument parsing tests."""

    def test_parse_args_requires_source(self) -> None:
        """Both --source-file and --source-url missing should fail."""
        with patch("sys.argv", ["generator.py"]):
            with self.assertRaises(SystemExit):
                generator.parse_args()

    def test_parse_args_mutually_exclusive_sources(self) -> None:
        """--source-file and --source-url together should fail."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json", "--source-url", "https://example.com"]):
            with self.assertRaises(SystemExit):
                generator.parse_args()

    def test_parse_args_source_file_only(self) -> None:
        """--source-file alone is valid."""
        with patch("sys.argv", ["generator.py", "--source-file", "/path/to/source.json"]):
            args = generator.parse_args()
            self.assertEqual(args.source_file, "/path/to/source.json")
            self.assertIsNone(args.source_url)

    def test_parse_args_source_url_only(self) -> None:
        """--source-url alone is valid."""
        with patch("sys.argv", ["generator.py", "--source-url", "https://example.com/tracks"]):
            args = generator.parse_args()
            self.assertIsNone(args.source_file)
            self.assertEqual(args.source_url, "https://example.com/tracks")

    def test_parse_args_default_database(self) -> None:
        """--database defaults to 'archive.db'."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json"]):
            args = generator.parse_args()
            self.assertEqual(args.database, "archive.db")

    def test_parse_args_custom_database(self) -> None:
        """--database can be customized."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json", "--database", "custom.db"]):
            args = generator.parse_args()
            self.assertEqual(args.database, "custom.db")

    def test_parse_args_default_root_key(self) -> None:
        """--root-key defaults to 'tracks'."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json"]):
            args = generator.parse_args()
            self.assertEqual(args.root_key, "tracks")

    def test_parse_args_custom_root_key(self) -> None:
        """--root-key can be customized."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json", "--root-key", "songs"]):
            args = generator.parse_args()
            self.assertEqual(args.root_key, "songs")

    def test_parse_args_default_artist(self) -> None:
        """--default-artist defaults to 'virtualluser'."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json"]):
            args = generator.parse_args()
            self.assertEqual(args.default_artist, "virtualluser")

    def test_parse_args_custom_default_artist(self) -> None:
        """--default-artist can be customized."""
        with patch("sys.argv", ["generator.py", "--source-file", "f.json", "--default-artist", "guest-artist"]):
            args = generator.parse_args()
            self.assertEqual(args.default_artist, "guest-artist")

    def test_parse_args_all_options_together(self) -> None:
        """All options can be combined."""
        with patch(
            "sys.argv",
            [
                "generator.py",
                "--source-url",
                "https://example.com/api/tracks",
                "--database",
                "music.db",
                "--root-key",
                "items",
                "--default-artist",
                "unknown",
            ],
        ):
            args = generator.parse_args()
            self.assertIsNone(args.source_file)
            self.assertEqual(args.source_url, "https://example.com/api/tracks")
            self.assertEqual(args.database, "music.db")
            self.assertEqual(args.root_key, "items")
            self.assertEqual(args.default_artist, "unknown")


class MainFunctionTests(unittest.TestCase):
    """End-to-end main() function tests."""

    def test_main_successful_import_from_file(self) -> None:
        """main() with --source-file should succeed and return 0."""
        payload = {"tracks": [{"id": "1", "title": "Test Track"}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "source.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "sys.argv",
                ["generator.py", "--source-file", str(source_file), "--database", str(database)],
            ):
                with patch("builtins.print") as mock_print:
                    exit_code = generator.main()

            self.assertEqual(exit_code, 0)
            mock_print.assert_called_once()
            call_args = mock_print.call_args[0][0]
            self.assertIn("Imported 1 track", call_args)
            self.assertIn(str(database), call_args)

            # Verify database was created and populated
            with sqlite3.connect(database) as conn:
                row = conn.execute("SELECT source_id, title FROM tracks WHERE source_id = '1'").fetchone()
                self.assertEqual(row, ("1", "Test Track"))

    def test_main_successful_import_from_url(self) -> None:
        """main() with --source-url should succeed via mocked HTTP."""
        payload = {"tracks": [{"id": "song-1", "title": "Remote Track", "artist": "remote-artist"}]}
        payload_json = json.dumps(payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.db"

            with patch(
                "generator.build_opener",
                return_value=FakeOpener(
                    FakeResponse(payload_json, content_type="application/json")
                ),
            ):
                with patch(
                    "sys.argv",
                    ["generator.py", "--source-url", "https://example.com/tracks", "--database", str(database)],
                ):
                    with patch("builtins.print") as mock_print:
                        exit_code = generator.main()

            self.assertEqual(exit_code, 0)
            mock_print.assert_called_once()
            call_args = mock_print.call_args[0][0]
            self.assertIn("Imported 1 track", call_args)

            # Verify database was created and populated
            with sqlite3.connect(database) as conn:
                row = conn.execute(
                    "SELECT source_id, title, artist FROM tracks WHERE source_id = 'song-1'"
                ).fetchone()
                self.assertEqual(row, ("song-1", "Remote Track", "remote-artist"))

    def test_main_custom_root_key(self) -> None:
        """main() with --root-key should use custom key."""
        payload = {"songs": [{"id": "2", "title": "Alternative Root"}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "source.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "sys.argv",
                [
                    "generator.py",
                    "--source-file",
                    str(source_file),
                    "--database",
                    str(database),
                    "--root-key",
                    "songs",
                ],
            ):
                exit_code = generator.main()

            self.assertEqual(exit_code, 0)

            with sqlite3.connect(database) as conn:
                row = conn.execute("SELECT title FROM tracks WHERE source_id = '2'").fetchone()
                self.assertEqual(row[0], "Alternative Root")

    def test_main_custom_default_artist(self) -> None:
        """main() with --default-artist should use fallback artist."""
        payload = {"tracks": [{"id": "3", "title": "Artist Fallback"}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "source.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "sys.argv",
                [
                    "generator.py",
                    "--source-file",
                    str(source_file),
                    "--database",
                    str(database),
                    "--default-artist",
                    "fallback-artist",
                ],
            ):
                exit_code = generator.main()

            self.assertEqual(exit_code, 0)

            with sqlite3.connect(database) as conn:
                row = conn.execute("SELECT artist FROM tracks WHERE source_id = '3'").fetchone()
                self.assertEqual(row[0], "fallback-artist")

    def test_main_missing_source_file(self) -> None:
        """main() with non-existent file should raise error."""
        with patch(
            "sys.argv",
            ["generator.py", "--source-file", "/nonexistent/path/source.json"],
        ):
            with self.assertRaises(FileNotFoundError):
                generator.main()

    def test_main_invalid_json(self) -> None:
        """main() with invalid JSON should raise error."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "bad.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text("not valid json", encoding="utf-8")

            with patch(
                "sys.argv",
                ["generator.py", "--source-file", str(source_file), "--database", str(database)],
            ):
                with self.assertRaises(json.JSONDecodeError):
                    generator.main()

    def test_main_invalid_payload_missing_root_key(self) -> None:
        """main() with missing root key should fail."""
        payload = {"wrong_key": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "source.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "sys.argv",
                ["generator.py", "--source-file", str(source_file), "--database", str(database)],
            ):
                with self.assertRaisesRegex(ValueError, "does not contain the configured root key"):
                    generator.main()

    def test_main_multiple_tracks(self) -> None:
        """main() should import multiple tracks correctly."""
        payload = {
            "tracks": [
                {"id": "1", "title": "Track 1"},
                {"id": "2", "title": "Track 2", "artist": "custom-artist"},
                {"id": "3", "title": "Track 3", "tags": ["tag1", "tag2"]},
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "source.json"
            database = Path(temp_dir) / "test.db"

            source_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "sys.argv",
                ["generator.py", "--source-file", str(source_file), "--database", str(database)],
            ):
                with patch("builtins.print") as mock_print:
                    exit_code = generator.main()

            self.assertEqual(exit_code, 0)
            call_args = mock_print.call_args[0][0]
            self.assertIn("Imported 3 track", call_args)

            with sqlite3.connect(database) as conn:
                count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
                self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
