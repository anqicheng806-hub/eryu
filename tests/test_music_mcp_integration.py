from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client

from mcp_server.eryu_music_mcp import EryuReadClient, build_server
from server.eryu import EryuHandler, ServerState, ThreadingHTTPServer


FULL_TOKEN = "integration-full-token-for-tests-000001"
READ_TOKEN = "integration-read-token-for-tests-000002"


def current_presence() -> dict:
    return {
        "schemaVersion": 1,
        "clientSessionId": "mcp-integration-session",
        "sequence": 1,
        "reportedAt": "2026-08-14T06:00:00Z",
        "song": {
            "songId": 12345,
            "name": "Integration Song",
            "artist": "Integration Artist",
            "album": "Integration Album",
            "cover": "",
        },
        "playback": {
            "status": "playing",
            "playing": True,
            "positionSeconds": 75,
            "durationSeconds": 180,
            "progressRatio": 75 / 180,
        },
        "lyrics": {
            "songId": 12345,
            "status": "none",
            "currentIndex": -1,
            "current": None,
            "previous": [],
            "next": [],
        },
    }


class MusicMcpHttpIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ERYU_AUTH_TOKEN": FULL_TOKEN,
                "ERYU_MCP_READ_TOKEN": READ_TOKEN,
                "MUSIC_PRESENCE_TTL_SECONDS": "10",
            },
            clear=False,
        )
        self.environment.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = ServerState(0, data_dir=Path(self.temp_dir.name))
        self.state.presence.update(current_presence())

        cache_dir = self.state.data_dir / "music_cache"
        (cache_dir / "12345_preanalysis.json").write_text(
            json.dumps(
                {
                    "songId": "12345",
                    "name": "Integration Song",
                    "artist": "Integration Artist",
                    "duration": 180,
                    "bpm": 128,
                    "key": "C#",
                    "segments": [
                        {"start": 60, "end": 120, "avgEnergy": 0.3, "maxEnergy": 0.5}
                    ],
                    "spectrogram": "C:/private/server/cache/12345_analysis.png",
                }
            ),
            encoding="utf-8",
        )
        self.image = b"\x89PNG\r\n\x1a\nend-to-end-synthetic-image"
        (cache_dir / "12345_analysis.png").write_bytes(self.image)

        EryuHandler.state = self.state
        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), EryuHandler)
        self.http_server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.http_server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()
        self.environment.stop()

    async def test_real_http_analysis_returns_energy_and_image_without_path(self) -> None:
        backend = EryuReadClient(
            f"http://127.0.0.1:{self.http_server.server_port}",
            READ_TOKEN,
        )
        async with Client(build_server(backend), raise_exceptions=True) as client:
            result = await client.call_tool("music_analysis", {})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["analysis"]["bpm"], 128.0)
        self.assertEqual(
            result.structured_content["analysis"]["currentEnergySegment"]["avgEnergy"],
            0.3,
        )
        self.assertTrue(result.structured_content["analysis"]["spectrogramIncluded"])
        self.assertEqual([item.type for item in result.content], ["text", "image"])
        self.assertEqual(result.content[1].mime_type, "image/png")
        self.assertNotIn("C:/private", json.dumps(result.structured_content))


if __name__ == "__main__":
    unittest.main()
