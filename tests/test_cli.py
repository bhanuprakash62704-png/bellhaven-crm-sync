"""Tests for CLI argument routing without network calls or persistent project data."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bellhaven_crm_sync import cli
from bellhaven_crm_sync.models import Community


class CLITests(unittest.TestCase):
    def test_parser_requires_a_command(self):
        with self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args([])

        self.assertEqual(raised.exception.code, 2)

    def test_scrape_command_writes_json_snapshot(self):
        communities = [
            Community(
                "https://example.test/location",
                "Facility",
                "1 Main St",
                "Town",
                "OH",
                "00123",
                ("Assisted Living",),
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "communities.json"
            with patch("sys.argv", ["cli", "scrape", "--output", str(output)]):
                with patch.object(cli, "scrape_communities", return_value=communities):
                    with contextlib.redirect_stdout(io.StringIO()):
                        cli.main()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload[0]["name"], "Facility")
        self.assertEqual(payload[0]["care_offerings"], ["Assisted Living"])

    def test_refresh_command_prints_refresh_summary_and_returns(self):
        with patch("sys.argv", ["cli", "refresh", "--db", "review.sqlite3"]):
            with patch.object(cli, "refresh_review_queue", return_value={"new_proposals_added": 2}) as refresh:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    cli.main()

        refresh.assert_called_once_with("review.sqlite3")
        self.assertEqual(json.loads(output.getvalue()), {"new_proposals_added": 2})

    def test_run_refreshes_then_starts_server(self):
        with patch(
            "sys.argv",
            ["cli", "run", "--db", "review.sqlite3", "--host", "localhost", "--port", "8765"],
        ):
            with patch.object(cli, "refresh_review_queue", return_value={"proposal_count": 1}) as refresh:
                with patch.object(cli, "load_local_env") as load_env:
                    with patch.object(cli, "serve") as serve:
                        with contextlib.redirect_stdout(io.StringIO()):
                            cli.main()

        refresh.assert_called_once_with("review.sqlite3")
        load_env.assert_called_once_with()
        serve.assert_called_once_with(
            host="localhost",
            port=8765,
            database_path="review.sqlite3",
        )

    def test_serve_command_does_not_refresh(self):
        with patch("sys.argv", ["cli", "serve", "--port", "8001"]):
            with patch.object(cli, "refresh_review_queue") as refresh:
                with patch.object(cli, "load_local_env"):
                    with patch.object(cli, "serve") as serve:
                        cli.main()

        refresh.assert_not_called()
        serve.assert_called_once_with(
            host="127.0.0.1",
            port=8001,
            database_path="data/review.sqlite3",
        )


if __name__ == "__main__":
    unittest.main()
