from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLIENT_HTML = REPOSITORY_ROOT / "client" / "index.html"
NODE = shutil.which("node")


def _reporter_core() -> str:
    source = CLIENT_HTML.read_text(encoding="utf-8")
    start_marker = "// PRESENCE_REPORTER_CORE_START"
    end_marker = "// PRESENCE_REPORTER_CORE_END"
    if source.count(start_marker) != 1 or source.count(end_marker) != 1:
        raise AssertionError("presence reporter test markers must occur exactly once")
    return source.split(start_marker, 1)[1].split(end_marker, 1)[0]


class ClientPresenceReporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if NODE is None:
            raise RuntimeError(
                "Node.js is required for the browser reporter regression tests"
            )

    def run_scenario(self, scenario: str) -> None:
        script = f"""
'use strict';
{_reporter_core()}

const assert = require('node:assert/strict');

class FakeAbortController {{
  constructor() {{
    const listeners = [];
    this.signal = {{
      aborted: false,
      addEventListener: (event, callback) => {{
        if (event === 'abort') listeners.push(callback);
      }},
    }};
    this.abort = () => {{
      if (this.signal.aborted) return;
      this.signal.aborted = true;
      for (const listener of listeners) listener();
    }};
  }}
}}

function makeHarness() {{
  let latestSongId = 'A';
  const sends = [];
  const deferred = [];
  const reporter = createPresenceReporter({{
    hasAuth: () => true,
    buildSnapshot: () => ({{ songId: latestSongId }}),
    sendSnapshot: (payload, {{ signal }}) => {{
      sends.push({{ payload, signal }});
      return new Promise((resolve, reject) => {{
        deferred.push({{ resolve, reject }});
        signal.addEventListener('abort', () => reject(new Error('aborted')));
      }});
    }},
    createAbortController: () => new FakeAbortController(),
    scheduleTimeout: () => ({{ kind: 'fake-timeout' }}),
    cancelTimeout: () => {{}},
  }});
  return {{
    reporter,
    sends,
    deferred,
    setSong: songId => {{ latestSongId = songId; }},
  }};
}}

async function settle() {{
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}}

(async () => {{
{scenario}
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
"""
        result = subprocess.run(
            [NODE, "-"],
            input=script,
            text=True,
            capture_output=True,
            cwd=REPOSITORY_ROOT,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_in_flight_events_coalesce_into_one_latest_trailing_snapshot(self) -> None:
        self.run_scenario(
            """
  const harness = makeHarness();
  harness.reporter.request();
  assert.equal(harness.sends.length, 1);
  assert.equal(harness.sends[0].payload.songId, 'A');

  harness.setSong('B');
  harness.reporter.request();
  harness.setSong('C');
  harness.reporter.request();
  assert.equal(harness.sends.length, 1, 'only one request may be in flight');

  harness.deferred[0].resolve();
  await settle();
  assert.equal(harness.sends.length, 2, 'completion must immediately drain one trailing send');
  assert.equal(harness.sends[1].payload.songId, 'C', 'the trailing send must build the latest snapshot');

  harness.deferred[1].resolve();
  await settle();
  assert.equal(harness.sends.length, 2, 'coalesced events must not create extra sends');
"""
        )

    def test_song_change_aborts_old_request_then_sends_latest_snapshot(self) -> None:
        self.run_scenario(
            """
  const harness = makeHarness();
  harness.reporter.request();
  harness.setSong('B');
  harness.reporter.request({ preempt: true });

  assert.equal(harness.sends.length, 1, 'replacement waits for the aborted request to settle');
  assert.equal(harness.sends[0].signal.aborted, true, 'song change must abort the old request');

  await settle();
  assert.equal(harness.sends.length, 2, 'abort must immediately drain the pending snapshot');
  assert.equal(harness.sends[1].payload.songId, 'B');

  harness.deferred[1].resolve();
  await settle();
  assert.equal(harness.sends.length, 2);
"""
        )

    def test_failure_without_newer_event_does_not_retry(self) -> None:
        self.run_scenario(
            """
  const harness = makeHarness();
  harness.reporter.request();
  harness.deferred[0].reject(new Error('network failure'));
  await settle();
  await settle();
  assert.equal(harness.sends.length, 1, 'a failure alone must not trigger an immediate retry');
"""
        )


class ClientPresenceReporterWiringTests(unittest.TestCase):
    def test_periodic_heartbeat_remains_two_seconds(self) -> None:
        source = CLIENT_HTML.read_text(encoding="utf-8")
        self.assertIn("setInterval(reportPresence, 2000)", source)

    def test_song_load_uses_preemptive_presence_request(self) -> None:
        source = CLIENT_HTML.read_text(encoding="utf-8")
        self.assertIn("reportPresence({ preempt: true });", source)

    def test_auth_token_is_masked_and_not_persisted_in_browser_storage(self) -> None:
        source = CLIENT_HTML.read_text(encoding="utf-8")
        self.assertIn('id="auth-input" type="password"', source)
        self.assertIn("let sessionAuthToken = '';", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)


if __name__ == "__main__":
    unittest.main()
