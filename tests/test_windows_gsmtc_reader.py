import asyncio
import json
import enum
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from reader.windows_gsmtc_reader import (
    GSMTCReader,
    HttpResult,
    ListenEventReporter,
    ListenEventTracker,
    LyricLine,
    LyricsEnricher,
    LyricsHttpClient,
    LyricsLookupError,
    LyricsLookupResult,
    LyricsSnapshot,
    PresenceHttpClient,
    PresencePayloadBuilder,
    PresenceSession,
    SingleInstanceGuard,
    _normalize_endpoint,
    _select_session,
    _coerce_status,
    _effective_position,
    _parse_lyrics,
    _select_catalog_candidate,
    _select_lyrics_candidate,
)
from server.presence import PresenceStore, validate_presence_payload


def session(
    *,
    source="Spotify.exe",
    title="Song A",
    artist="Artist A",
    album="Album A",
    status="playing",
    playing=True,
    position=20.0,
    duration=100.0,
):
    return PresenceSession(
        source_app_user_model_id=source,
        title=title,
        artist=artist,
        album=album,
        status=status,
        playing=playing,
        position_seconds=position,
        duration_seconds=duration,
    )


class _MonotonicClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ListenEventTrackerTest(unittest.TestCase):
    def make_tracker(self):
        clock = _MonotonicClock()
        event_ids = iter(("listen:event-1", "listen:event-2", "listen:event-3"))
        tracker = ListenEventTracker(
            monotonic=clock,
            event_id_factory=lambda: next(event_ids),
        )
        return tracker, clock

    def test_counts_once_after_thirty_seconds_and_keeps_catalog_alias(self):
        tracker, clock = self.make_tracker()
        current = session(duration=180.0)

        self.assertIsNone(tracker.observe(current))
        for index in range(5):
            clock.advance(5)
            self.assertIsNone(
                tracker.observe(
                    current,
                    catalog_song_id="12345" if index == 0 else None,
                )
            )
        clock.advance(5)
        event = tracker.observe(current)

        self.assertIsNotNone(event)
        self.assertEqual(event["eventId"], "listen:event-1")
        self.assertEqual(event["name"], "Song A")
        self.assertEqual(event["durationSeconds"], 180.0)
        self.assertEqual(
            event["catalog"],
            {"provider": "netease", "songId": "12345"},
        )
        clock.advance(60)
        self.assertIsNone(tracker.observe(current))

    def test_paused_time_and_timeline_seek_do_not_count(self):
        tracker, clock = self.make_tracker()
        playing = session(position=1.0)
        paused = session(status="paused", playing=False, position=90.0)

        tracker.observe(playing)
        clock.advance(5)
        self.assertIsNone(tracker.observe(playing))
        clock.advance(5)
        self.assertIsNone(tracker.observe(paused))
        clock.advance(100)
        self.assertIsNone(tracker.observe(paused))
        self.assertIsNone(tracker.observe(playing))
        for position in (99.0, 2.0, 75.0):
            clock.advance(5)
            self.assertIsNone(tracker.observe(session(position=position)))
        clock.advance(5)
        self.assertIsNotNone(tracker.observe(session(position=2.0)))

    def test_long_observation_gap_is_not_mistaken_for_playback(self):
        tracker, clock = self.make_tracker()
        current = session()

        tracker.observe(current)
        clock.advance(3600)
        self.assertIsNone(tracker.observe(current))
        for _ in range(4):
            clock.advance(5)
            self.assertIsNone(tracker.observe(current))
        clock.advance(5)
        self.assertIsNotNone(tracker.observe(current))

    def test_song_switch_resets_and_terminal_replay_is_a_new_visit(self):
        tracker, clock = self.make_tracker()
        first = session(title="First")
        second = session(title="Second")

        tracker.observe(first)
        clock.advance(29)
        self.assertIsNone(tracker.observe(second))
        for _ in range(5):
            clock.advance(5)
            self.assertIsNone(tracker.observe(second))
        clock.advance(5)
        first_event = tracker.observe(second)
        self.assertEqual(first_event["eventId"], "listen:event-2")

        ended = session(title="Second", status="ended", playing=False)
        self.assertIsNone(tracker.observe(ended))
        self.assertIsNone(tracker.observe(second))
        for _ in range(5):
            clock.advance(5)
            self.assertIsNone(tracker.observe(second))
        clock.advance(5)
        replay_event = tracker.observe(second)
        self.assertEqual(replay_event["eventId"], "listen:event-3")


class PayloadBuilderTest(unittest.TestCase):
    def setUp(self):
        self.builder = PresencePayloadBuilder("gsmtc:test")

    def test_playing_session_matches_v2_server_schema(self):
        payload = self.builder.build(1, session())
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertIsNone(payload["song"]["catalog"])
        validated = validate_presence_payload(payload)

        self.assertEqual(validated["playback"]["status"], "playing")
        self.assertTrue(validated["playback"]["playing"])
        self.assertEqual(validated["playback"]["positionSeconds"], 20.0)
        self.assertEqual(validated["playback"]["durationSeconds"], 100.0)
        self.assertEqual(validated["playback"]["progressRatio"], 0.2)
        self.assertEqual(validated["lyrics"]["status"], "none")

    def test_winrt_int_enum_name_maps_to_playback_status(self):
        class PlaybackStatus(enum.IntEnum):
            PLAYING = 4
            PAUSED = 5

        self.assertEqual(_coerce_status(PlaybackStatus.PLAYING), "playing")
        self.assertEqual(_coerce_status(PlaybackStatus.PAUSED), "paused")

    def test_playing_position_advances_but_paused_position_does_not(self):
        now = datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc)
        updated = now - timedelta(seconds=2)

        self.assertEqual(
            _effective_position(5.0, 20.0, "playing", updated, now=now),
            7.0,
        )
        self.assertEqual(
            _effective_position(5.0, 20.0, "paused", updated, now=now),
            5.0,
        )
        self.assertEqual(
            _effective_position(19.5, 20.0, "playing", updated, now=now),
            20.0,
        )

    def test_paused_session_is_not_marked_playing(self):
        payload = self.builder.build(
            2, session(status="paused", playing=False, position=12.5)
        )
        validated = validate_presence_payload(payload)

        self.assertEqual(validated["playback"]["status"], "paused")
        self.assertFalse(validated["playback"]["playing"])
        self.assertEqual(validated["playback"]["positionSeconds"], 12.5)

    def test_no_session_builds_valid_idle_presence(self):
        payload = self.builder.build(3, None)
        self.assertEqual(payload["schemaVersion"], 2)
        validated = validate_presence_payload(payload)

        self.assertIsNone(validated["song"])
        self.assertEqual(validated["playback"]["status"], "idle")
        self.assertEqual(validated["lyrics"]["status"], "idle")

    def test_song_switch_changes_stable_numeric_song_id(self):
        first = self.builder.build(4, session(title="First"))
        repeated = self.builder.build(5, session(title="First"))
        second = self.builder.build(6, session(title="Second"))

        self.assertEqual(first["song"]["songId"], repeated["song"]["songId"])
        self.assertNotEqual(first["song"]["songId"], second["song"]["songId"])
        self.assertTrue(first["song"]["songId"].isdigit())

    def test_store_requires_strictly_increasing_sequence(self):
        store = PresenceStore(10.0)
        store.update(self.builder.build(10, session()))
        fresh = store.update(self.builder.build(11, session(position=22.0)))

        self.assertEqual(fresh["freshness"]["state"], "fresh")
        self.assertEqual(fresh["presence"]["sequence"], 11)

    def test_ready_lyrics_window_and_translation_match_strict_schema(self):
        current_session = session(position=22.0)
        song_id = self.builder.build(12, current_session)["song"]["songId"]
        lines = _parse_lyrics(
            "\n".join(
                (
                    "[00:00.00]first",
                    "[00:10.00]second",
                    "[00:20.00]third",
                    "[00:30.00]fourth",
                )
            ),
            "[00:20.00]第三句",
        )

        payload = self.builder.build(
            12,
            current_session,
            LyricsSnapshot(song_id, "ready", lines, "123"),
        )
        validated = validate_presence_payload(payload)
        lyrics = validated["lyrics"]

        self.assertEqual(
            validated["song"]["catalog"],
            {"provider": "netease", "songId": "123"},
        )
        self.assertEqual(lyrics["songId"], validated["song"]["songId"])
        self.assertEqual(lyrics["currentIndex"], 2)
        self.assertEqual(lyrics["current"]["text"], "third")
        self.assertEqual(lyrics["current"]["translation"], "第三句")
        self.assertEqual([line["text"] for line in lyrics["previous"]], ["first", "second"])
        self.assertEqual([line["text"] for line in lyrics["next"]], ["fourth"])

    def test_ready_before_first_line_and_nonready_statuses_are_valid(self):
        current_session = session(position=1.0)
        song_id = self.builder.build(13, current_session)["song"]["songId"]
        ready = self.builder.build(
            13,
            current_session,
            LyricsSnapshot(
                song_id,
                "ready",
                (
                    LyricLine(0, 5.0, "first"),
                    LyricLine(1, 10.0, "second"),
                    LyricLine(2, 15.0, "third"),
                ),
            ),
        )
        validated_ready = validate_presence_payload(ready)
        self.assertEqual(validated_ready["lyrics"]["currentIndex"], -1)
        self.assertIsNone(validated_ready["lyrics"]["current"])
        self.assertEqual(
            [line["text"] for line in validated_ready["lyrics"]["next"]],
            ["first", "second"],
        )

        for sequence, status in enumerate(("loading", "none", "error"), start=14):
            with self.subTest(status=status):
                payload = self.builder.build(
                    sequence,
                    current_session,
                    LyricsSnapshot(song_id, status),
                )
                validated = validate_presence_payload(payload)
                self.assertEqual(validated["lyrics"]["status"], status)
                self.assertEqual(
                    validated["lyrics"]["songId"],
                    validated["song"]["songId"],
                )

    def test_oversized_lyric_text_is_bounded_before_presence_validation(self):
        current_session = session(position=1.0)
        song_id = self.builder.build(17, current_session)["song"]["songId"]
        payload = self.builder.build(
            17,
            current_session,
            LyricsSnapshot(
                song_id,
                "ready",
                (LyricLine(0, 0.0, "x" * 2100, "译" * 2100),),
            ),
        )

        validated = validate_presence_payload(payload)
        self.assertEqual(len(validated["lyrics"]["current"]["text"]), 2000)
        self.assertEqual(len(validated["lyrics"]["current"]["translation"]), 2000)

    def test_mismatched_lyrics_snapshot_fails_closed_to_current_song_id(self):
        current_session = session(position=1.0)
        payload = self.builder.build(
            18,
            current_session,
            LyricsSnapshot(
                "999",
                "ready",
                (LyricLine(0, 0.0, "wrong song line"),),
                "123",
            ),
        )

        validated = validate_presence_payload(payload)
        self.assertEqual(validated["lyrics"]["status"], "none")
        self.assertEqual(
            validated["lyrics"]["songId"],
            validated["song"]["songId"],
        )
        self.assertIsNone(validated["lyrics"]["current"])
        self.assertIsNone(validated["song"]["catalog"])

    def test_catalog_is_available_for_none_lyrics_and_invalid_id_fails_closed(self):
        current_session = session()
        song_id = self.builder.build(19, current_session)["song"]["songId"]

        resolved = self.builder.build(
            19,
            current_session,
            LyricsSnapshot(song_id, "none", catalog_song_id="000123"),
        )
        invalid = self.builder.build(
            20,
            current_session,
            LyricsSnapshot(song_id, "none", catalog_song_id="../123"),
        )

        self.assertEqual(
            validate_presence_payload(resolved)["song"]["catalog"],
            {"provider": "netease", "songId": "123"},
        )
        self.assertIsNone(validate_presence_payload(invalid)["song"]["catalog"])


class LyricsParsingAndMatchingTest(unittest.TestCase):
    def test_parser_supports_multiple_timestamps_and_centisecond_translation(self):
        lines = _parse_lyrics(
            "[00:01.2][00:02.250]hello\n[00:03]world",
            "[00:02.25]你好\n[00:03.000]世界",
        )

        self.assertEqual([line.time_seconds for line in lines], [1.2, 2.25, 3.0])
        self.assertEqual([line.index for line in lines], [0, 1, 2])
        self.assertEqual([line.translation for line in lines], ["", "你好", "世界"])

    def test_candidate_matching_is_conservative_and_provider_id_is_internal(self):
        current_session = session(
            title="Song A",
            artist="Artist A & Artist B",
            album="Exact Album",
        )
        candidates = [
            {
                "id": 123,
                "name": "Song A",
                "artist": "Artist B, Artist A",
                "album": "Other Album",
            },
            {
                "id": 456,
                "name": "Song A",
                "artist": "Artist A & Artist B",
                "album": "Exact Album",
            },
        ]

        self.assertEqual(_select_lyrics_candidate(current_session, candidates), "456")
        self.assertIsNone(
            _select_lyrics_candidate(
                current_session,
                [
                    candidates[1],
                    {**candidates[1], "id": 789},
                ],
            )
        )
        self.assertIsNone(_select_lyrics_candidate(current_session, [candidates[0]]))
        self.assertIsNone(
            _select_lyrics_candidate(
                current_session,
                [{**candidates[1], "id": "../not-a-song-id"}],
            )
        )
        self.assertIsNone(
            _select_lyrics_candidate(
                current_session,
                [{**candidates[1], "id": 999, "name": "Song: A"}],
            )
        )

    def test_catalog_candidate_requires_exact_version_and_two_second_duration(self):
        current_session = session(
            title="Ｓｏｎｇ Ａ",
            artist="Artist A",
            album="Exact Album",
            duration=100.0,
        )
        exact = {
            "id": "000123",
            "name": "Song A",
            "artist": "Artist A",
            "album": "Exact Album",
            "durationSeconds": 102.0,
        }

        self.assertEqual(_select_catalog_candidate(current_session, [exact]), "123")
        self.assertIsNone(
            _select_catalog_candidate(
                current_session,
                [{**exact, "durationSeconds": 102.01}],
            )
        )
        self.assertIsNone(
            _select_catalog_candidate(
                current_session,
                [{**exact, "durationSeconds": 10**1000}],
            )
        )
        self.assertIsNone(
            _select_catalog_candidate(
                current_session,
                [{**exact, "album": "Other Album"}],
            )
        )
        self.assertIsNone(
            _select_catalog_candidate(
                current_session,
                [exact, {**exact, "id": 456}],
            )
        )
        self.assertEqual(
            _select_catalog_candidate(current_session, [exact, dict(exact)]),
            "123",
        )
        self.assertIsNone(
            _select_catalog_candidate(
                session(album="", duration=100.0),
                [exact],
            )
        )

    def test_catalog_strictness_does_not_change_lyrics_candidate_rule(self):
        current_session = session(album="Album A", duration=100.0)
        different_version = {
            "id": 123,
            "name": "Song A",
            "artist": "Artist A",
            "album": "Live Album",
            "durationSeconds": 140.0,
        }

        self.assertEqual(
            _select_lyrics_candidate(current_session, [different_version]),
            "123",
        )
        self.assertIsNone(_select_catalog_candidate(current_session, [different_version]))


class SessionSelectionTest(unittest.TestCase):
    def test_playing_session_wins_over_preferred_paused_session(self):
        paused_netease = session(
            source="cloudmusic.exe", status="paused", playing=False
        )
        playing_spotify = session(source="Spotify.exe")

        selected = _select_session(
            [paused_netease, playing_spotify],
            source_preference=("cloudmusic", "spotify"),
        )

        self.assertEqual(selected, playing_spotify)

    def test_preference_breaks_tie_and_ignored_source_uses_substring(self):
        browser = session(source="Chrome_WidgetWin_1.exe")
        spotify = session(source="Spotify.exe")
        cloudmusic = session(source="orpheus-cloudmusic.exe")

        selected = _select_session(
            [browser, spotify, cloudmusic],
            source_preference=("cloudmusic", "spotify"),
            ignored_sources={"chrome"},
        )

        self.assertEqual(selected, cloudmusic)


class _FakeAdapter:
    async def initialize(self):
        return None

    def attach_change_listener(self, _callback):
        return None

    async def current_session(self):
        return None


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        return None


class _ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sequences = []

    async def post(self, payload):
        self.sequences.append(payload["sequence"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ScriptedListenClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.event_ids = []

    async def post_listen(self, payload):
        self.event_ids.append(payload["eventId"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _CurrentSessionAdapter(_FakeAdapter):
    def __init__(self, current):
        self.current = current
        self.callback = None

    def attach_change_listener(self, callback):
        self.callback = callback

    async def current_session(self):
        return self.current


class _WakePresenceClient:
    def __init__(self, target_count=3):
        self.target_count = target_count
        self.payloads = []
        self.reader = None

    async def post(self, payload):
        self.payloads.append(payload)
        await asyncio.sleep(0)
        if len(self.payloads) < self.target_count:
            self.reader._on_changed()
        else:
            self.reader.stop()
        return HttpResult(200, "{}")


class _BlockingLyricsClient:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()

    async def lookup(self, _session):
        self.calls += 1
        self.started.set()
        await asyncio.Future()


class _BlockingListenClient:
    def __init__(self):
        self.started = asyncio.Event()

    async def post_listen(self, _payload):
        self.started.set()
        await asyncio.Future()


class _OneListenTracker:
    def __init__(self):
        self.sent = False

    def observe(self, current_session, *, catalog_song_id=None):
        if self.sent or current_session is None:
            return None
        self.sent = True
        return {
            "eventId": "listen:background-test",
            "songId": "123",
            "name": current_session.title,
            "artist": current_session.artist,
            "album": current_session.album,
            "durationSeconds": current_session.duration_seconds,
        }


class _OutcomeLyricsClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def lookup(self, _session):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _PerTitleLyricsClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def lookup(self, current_session):
        title = current_session.title
        self.calls.append(title)
        outcome = self.outcomes[title]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ControlledLyricsClient:
    def __init__(self, titles, outcomes=None):
        self.calls = []
        self.outcomes = outcomes or {}
        self.started = {title: asyncio.Event() for title in titles}
        self.release = {title: asyncio.Event() for title in titles}

    async def lookup(self, current_session):
        title = current_session.title
        self.calls.append(title)
        self.started[title].set()
        await self.release[title].wait()
        outcome = self.outcomes.get(title)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return LyricsLookupResult(
            "ready",
            (LyricLine(0, 0.0, f"{title} line"),),
        )


class ReaderSequenceTest(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, outcomes):
        client = _ScriptedClient(outcomes)
        reader = GSMTCReader(
            "http://127.0.0.1:9090",
            "x" * 32,
            adapter=_FakeAdapter(),
            http_client=client,
            lock=_FakeLock(),
        )
        return reader, client

    async def test_network_failure_retries_same_sequence_then_recovers(self):
        reader, client = self.make_reader(
            [RuntimeError("network unavailable"), HttpResult(200, "{}")]
        )

        self.assertFalse(await reader._send_once(session()))
        self.assertEqual(reader._sequence, 1)
        self.assertTrue(await reader._send_once(session()))
        self.assertEqual(reader._sequence, 2)
        self.assertEqual(client.sequences, [1, 1])

    async def test_409_marks_uncertain_sequence_as_consumed(self):
        reader, client = self.make_reader([HttpResult(409, "{}")])

        self.assertTrue(await reader._send_once(session()))
        self.assertEqual(reader._sequence, 2)
        self.assertEqual(client.sequences, [1])


class ListenEventReporterTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_post_retries_the_same_event_id(self):
        client = _ScriptedListenClient(
            [RuntimeError("network unavailable"), HttpResult(200, "{}")]
        )
        reporter = ListenEventReporter(
            client,
            retry_delay_seconds=0.01,
            max_retry_delay_seconds=0.01,
        )
        await reporter.start()
        self.assertTrue(
            reporter.submit(
                {
                    "eventId": "listen:same-event",
                    "songId": "123",
                    "name": "Song",
                }
            )
        )

        await asyncio.wait_for(reporter.queue.join(), timeout=1.0)
        await reporter.close()

        self.assertEqual(
            client.event_ids,
            ["listen:same-event", "listen:same-event"],
        )

    async def test_blocked_listen_post_does_not_block_presence_heartbeat(self):
        presence_client = _WakePresenceClient(target_count=3)
        listen_client = _BlockingListenClient()
        current = session(position=5.0)
        reader = GSMTCReader(
            "http://127.0.0.1:9090",
            "x" * 32,
            adapter=_CurrentSessionAdapter(current),
            http_client=presence_client,
            listen_client=listen_client,
            listen_tracker=_OneListenTracker(),
            lyrics_client=_OutcomeLyricsClient(LyricsLookupResult("none")),
            lock=_FakeLock(),
        )
        presence_client.reader = reader

        await asyncio.wait_for(reader.run(), timeout=2.0)

        self.assertTrue(listen_client.started.is_set())
        self.assertEqual(
            [payload["sequence"] for payload in presence_client.payloads],
            [1, 2, 3],
        )


class LyricsEnricherTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_lookup_does_not_block_presence_or_repeat_for_same_song(self):
        lyrics_client = _BlockingLyricsClient()
        presence_client = _WakePresenceClient(target_count=3)
        current = session(position=5.0)
        reader = GSMTCReader(
            "http://127.0.0.1:9090",
            "x" * 32,
            adapter=_CurrentSessionAdapter(current),
            http_client=presence_client,
            lyrics_client=lyrics_client,
            lock=_FakeLock(),
        )
        presence_client.reader = reader

        await asyncio.wait_for(reader.run(), timeout=2.0)

        self.assertTrue(lyrics_client.started.is_set())
        self.assertEqual(lyrics_client.calls, 1)
        self.assertEqual(
            [payload["sequence"] for payload in presence_client.payloads],
            [1, 2, 3],
        )
        for payload in presence_client.payloads:
            validated = validate_presence_payload(payload)
            self.assertEqual(validated["lyrics"]["status"], "loading")
            self.assertEqual(
                validated["lyrics"]["songId"],
                validated["song"]["songId"],
            )

    async def test_latest_song_wins_and_stale_result_only_enters_its_cache(self):
        client = _ControlledLyricsClient(
            ("Song A", "Song B", "Song C"),
            outcomes={
                "Song A": LyricsLookupResult(
                    "ready",
                    (LyricLine(0, 0.0, "Song A line"),),
                    "111",
                ),
                "Song C": LyricsLookupResult(
                    "ready",
                    (LyricLine(0, 0.0, "Song C line"),),
                    "333",
                ),
            },
        )
        changed = asyncio.Event()
        enricher = LyricsEnricher(client, changed.set)
        song_a = session(title="Song A")
        song_b = session(title="Song B")
        song_c = session(title="Song C")
        await enricher.start()
        try:
            enricher.observe(song_a)
            await asyncio.wait_for(client.started["Song A"].wait(), timeout=1.0)
            enricher.observe(song_b)
            enricher.observe(song_c)
            self.assertEqual(enricher.snapshot_for(song_c).status, "loading")
            self.assertIsNone(enricher.snapshot_for(song_c).catalog_song_id)

            client.release["Song A"].set()
            await asyncio.wait_for(client.started["Song C"].wait(), timeout=1.0)
            self.assertEqual(client.calls, ["Song A", "Song C"])
            self.assertFalse(changed.is_set())
            self.assertEqual(enricher.snapshot_for(song_c).status, "loading")

            client.release["Song C"].set()
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            await asyncio.wait_for(enricher._queue.join(), timeout=1.0)
            current = enricher.snapshot_for(song_c)
            self.assertEqual(current.status, "ready")
            self.assertEqual(current.lines[0].text, "Song C line")
            self.assertEqual(current.catalog_song_id, "333")

            enricher.observe(song_a)
            cached = enricher.snapshot_for(song_a)
            self.assertEqual(cached.status, "ready")
            self.assertEqual(cached.lines[0].text, "Song A line")
            self.assertEqual(cached.catalog_song_id, "111")
            self.assertEqual(client.calls, ["Song A", "Song C"])

            changed.clear()
            enricher.observe(song_b)
            await asyncio.wait_for(client.started["Song B"].wait(), timeout=1.0)
            self.assertEqual(client.calls, ["Song A", "Song C", "Song B"])
            client.release["Song B"].set()
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            await asyncio.wait_for(enricher._queue.join(), timeout=1.0)
            restored = enricher.snapshot_for(song_b)
            self.assertEqual(restored.status, "ready")
            self.assertEqual(restored.lines[0].text, "Song B line")
        finally:
            await enricher.close()

    async def test_none_and_error_are_cached_without_automatic_retry(self):
        cases = (
            (LyricsLookupResult("none"), "none"),
            (RuntimeError("lookup failed"), "error"),
        )
        for outcome, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                client = _OutcomeLyricsClient(outcome)
                changed = asyncio.Event()
                enricher = LyricsEnricher(client, changed.set)
                current = session(title=f"Song {expected_status}")
                await enricher.start()
                try:
                    enricher.observe(current)
                    await asyncio.wait_for(changed.wait(), timeout=1.0)
                    self.assertEqual(
                        enricher.snapshot_for(current).status,
                        expected_status,
                    )

                    enricher.observe(None)
                    enricher.observe(current)
                    await asyncio.sleep(0)
                    self.assertEqual(client.calls, 1)
                    self.assertEqual(
                        enricher.snapshot_for(current).status,
                        expected_status,
                    )
                finally:
                    await enricher.close()

    async def test_requeued_inflight_negative_song_does_not_repeat_lookup(self):
        negative_cases = (
            (LyricsLookupResult("none", catalog_song_id="111"), "none"),
            (LyricsLookupError("lookup failed", "111"), "error"),
        )
        for negative_outcome, expected_status in negative_cases:
            with self.subTest(expected_status=expected_status):
                client = _ControlledLyricsClient(
                    ("Song A", "Song B"),
                    outcomes={"Song A": negative_outcome},
                )
                changed = asyncio.Event()
                enricher = LyricsEnricher(client, changed.set)
                song_a = session(title="Song A")
                song_b = session(title="Song B")
                await enricher.start()
                try:
                    enricher.observe(song_a)
                    await asyncio.wait_for(client.started["Song A"].wait(), timeout=1.0)
                    enricher.observe(song_b)
                    enricher.observe(song_a)

                    client.release["Song A"].set()
                    await asyncio.wait_for(changed.wait(), timeout=1.0)
                    await asyncio.wait_for(enricher._queue.join(), timeout=1.0)

                    self.assertEqual(client.calls, ["Song A"])
                    self.assertFalse(client.started["Song B"].is_set())
                    self.assertEqual(
                        enricher.snapshot_for(song_a).status,
                        expected_status,
                    )
                    self.assertEqual(
                        enricher.snapshot_for(song_a).catalog_song_id,
                        "111",
                    )
                finally:
                    await enricher.close()

    async def test_negative_result_is_remembered_without_body_cache(self):
        client = _OutcomeLyricsClient(
            LyricsLookupResult("none", catalog_song_id="123")
        )
        changed = asyncio.Event()
        enricher = LyricsEnricher(client, changed.set)
        song_a = session(title="Song A")
        song_b = session(title="Song B")
        await enricher.start()
        try:
            enricher.observe(song_a)
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            changed.clear()
            enricher.observe(song_b)
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            self.assertEqual(client.calls, 2)

            enricher.observe(song_a)
            await asyncio.sleep(0)
            self.assertEqual(client.calls, 2)
            self.assertEqual(enricher.snapshot_for(song_a).status, "none")
            self.assertEqual(enricher.snapshot_for(song_a).catalog_song_id, "123")
            self.assertEqual(len(enricher._cache), 0)
        finally:
            await enricher.close()

    async def test_negative_results_do_not_evict_ready_body(self):
        negative_cases = (
            (LyricsLookupResult("none"), "none"),
            (RuntimeError("lookup failed"), "error"),
        )
        for negative_outcome, expected_status in negative_cases:
            with self.subTest(expected_status=expected_status):
                client = _PerTitleLyricsClient(
                    {
                        "Song A": LyricsLookupResult(
                            "ready",
                            (LyricLine(0, 0.0, "Song A line"),),
                        ),
                        "Song B": negative_outcome,
                    }
                )
                changed = asyncio.Event()
                enricher = LyricsEnricher(client, changed.set, cache_entries=1)
                song_a = session(title="Song A")
                song_b = session(title="Song B")
                await enricher.start()
                try:
                    enricher.observe(song_a)
                    await asyncio.wait_for(changed.wait(), timeout=1.0)
                    changed.clear()
                    self.assertEqual(enricher.snapshot_for(song_a).status, "ready")

                    enricher.observe(song_b)
                    await asyncio.wait_for(changed.wait(), timeout=1.0)
                    self.assertEqual(
                        enricher.snapshot_for(song_b).status,
                        expected_status,
                    )

                    enricher.observe(song_a)
                    restored = enricher.snapshot_for(song_a)
                    self.assertEqual(restored.status, "ready")
                    self.assertEqual(restored.lines[0].text, "Song A line")
                    self.assertEqual(client.calls, ["Song A", "Song B"])
                finally:
                    await enricher.close()

    async def test_evicted_ready_body_fails_closed_without_repeat_lookup(self):
        client = _PerTitleLyricsClient(
            {
                "Song A": LyricsLookupResult(
                    "ready",
                    (LyricLine(0, 0.0, "Song A line"),),
                    "111",
                ),
                "Song B": LyricsLookupResult(
                    "ready",
                    (LyricLine(0, 0.0, "Song B line"),),
                    "222",
                ),
            }
        )
        changed = asyncio.Event()
        enricher = LyricsEnricher(client, changed.set, cache_entries=1)
        song_a = session(title="Song A")
        song_b = session(title="Song B")
        await enricher.start()
        try:
            enricher.observe(song_a)
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            changed.clear()
            enricher.observe(song_b)
            await asyncio.wait_for(changed.wait(), timeout=1.0)

            enricher.observe(song_a)
            restored = enricher.snapshot_for(song_a)
            self.assertEqual(restored.status, "none")
            self.assertEqual(restored.lines, ())
            self.assertEqual(restored.catalog_song_id, "111")
            self.assertEqual(client.calls, ["Song A", "Song B"])
        finally:
            await enricher.close()

    async def test_new_enricher_run_allows_one_new_lookup(self):
        client = _OutcomeLyricsClient(LyricsLookupResult("none"))
        current = session(title="Song none")

        for _ in range(2):
            changed = asyncio.Event()
            enricher = LyricsEnricher(client, changed.set)
            await enricher.start()
            try:
                enricher.observe(current)
                await asyncio.wait_for(changed.wait(), timeout=1.0)
                self.assertEqual(enricher.snapshot_for(current).status, "none")
            finally:
                await enricher.close()

        self.assertEqual(client.calls, 2)

    async def test_lyrics_completion_does_not_wake_presence_retry_event(self):
        lyrics_client = _OutcomeLyricsClient(
            LyricsLookupResult(
                "ready",
                (LyricLine(0, 0.0, "line"),),
            )
        )
        reader = GSMTCReader(
            "http://127.0.0.1:9090",
            "x" * 32,
            adapter=_FakeAdapter(),
            http_client=_ScriptedClient([HttpResult(200, "{}")]),
            lyrics_client=lyrics_client,
            lock=_FakeLock(),
        )
        current = session()
        reader.notify_loop = asyncio.get_running_loop()
        await reader.lyrics.start()
        try:
            reader.lyrics.observe(current)
            for _ in range(100):
                if reader.lyrics.snapshot_for(current).status == "ready":
                    break
                await asyncio.sleep(0)
            self.assertEqual(reader.lyrics.snapshot_for(current).status, "ready")
            self.assertFalse(reader.notify.is_set())
        finally:
            await reader.lyrics.close()


class _JsonResponse:
    def __init__(self, payload):
        self.status = 200
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class _RecordingLyricsOpener:
    def __init__(self, songs=None, lyric_payload=None):
        self.requests = []
        self.songs = songs if songs is not None else [
            {
                "id": 123,
                "name": "Song A",
                "artist": "Artist A",
                "album": "Album A",
                "durationSeconds": 100.0,
                "cover": "",
            }
        ]
        self.lyric_payload = lyric_payload if lyric_payload is not None else {
            "ok": True,
            "lrc": "[00:00.00]line",
            "tlyric": "[00:00.00]译文",
        }

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if "/music/search?" in request.full_url:
            return _JsonResponse(
                {
                    "ok": True,
                    "songs": self.songs,
                }
            )
        return _JsonResponse(self.lyric_payload)


class LyricsHttpClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_uses_header_auth_encoded_gets_and_parses_translation(self):
        token = "x" * 32
        client = LyricsHttpClient("http://127.0.0.1:9090", token)
        opener = _RecordingLyricsOpener()
        client._opener = opener

        result = await client.lookup(session())

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.lines[0].translation, "译文")
        self.assertEqual(result.catalog_song_id, "123")
        self.assertEqual(len(opener.requests), 2)
        search_request = opener.requests[0][0]
        lyric_request = opener.requests[1][0]
        self.assertEqual(search_request.get_method(), "GET")
        self.assertEqual(lyric_request.get_method(), "GET")
        self.assertIn("/music/search?q=Song+A+Artist+A", search_request.full_url)
        self.assertIn("/music/lyric?id=123", lyric_request.full_url)
        for request, timeout in opener.requests:
            headers = {name.lower(): value for name, value in request.header_items()}
            self.assertEqual(headers["x-auth-token"], token)
            self.assertNotIn(token, request.full_url)
            self.assertEqual(timeout, 12.0)

    async def test_strict_catalog_can_resolve_when_lyrics_match_is_ambiguous(self):
        client = LyricsHttpClient("http://127.0.0.1:9090", "x" * 32)
        opener = _RecordingLyricsOpener(
            [
                {
                    "id": 123,
                    "name": "Song A",
                    "artist": "Artist A",
                    "album": "Album A",
                    "durationSeconds": 100.0,
                },
                {
                    "id": 456,
                    "name": "Song A",
                    "artist": "Artist A",
                    "album": "Live Album",
                    "durationSeconds": 140.0,
                },
            ]
        )
        client._opener = opener

        result = await client.lookup(session())

        self.assertEqual(result.status, "none")
        self.assertEqual(result.catalog_song_id, "123")
        self.assertEqual(len(opener.requests), 1)

    async def test_lyric_failure_preserves_catalog_in_error_attempt_record(self):
        client = LyricsHttpClient("http://127.0.0.1:9090", "x" * 32)
        opener = _RecordingLyricsOpener(
            songs=[
                {
                    "id": "000123",
                    "name": "Song A",
                    "artist": "Artist A",
                    "album": "Album A",
                    "durationSeconds": 100.0,
                }
            ],
            lyric_payload={"ok": False},
        )
        client._opener = opener
        changed = asyncio.Event()
        enricher = LyricsEnricher(client, changed.set)
        current = session()
        await enricher.start()
        try:
            enricher.observe(current)
            await asyncio.wait_for(changed.wait(), timeout=1.0)
            failed = enricher.snapshot_for(current)
            self.assertEqual(failed.status, "error")
            self.assertEqual(failed.catalog_song_id, "123")

            enricher.observe(None)
            enricher.observe(current)
            restored = enricher.snapshot_for(current)
            self.assertEqual(restored.status, "error")
            self.assertEqual(restored.catalog_song_id, "123")
            self.assertEqual(len(opener.requests), 2)
        finally:
            await enricher.close()

    async def test_missing_artist_fails_closed_without_search_request(self):
        client = LyricsHttpClient("http://127.0.0.1:9090", "x" * 32)
        opener = _RecordingLyricsOpener()
        client._opener = opener

        result = await client.lookup(session(artist=""))

        self.assertEqual(result.status, "none")
        self.assertEqual(opener.requests, [])


class EndpointTest(unittest.TestCase):
    def test_endpoint_rejects_embedded_credentials_and_accepts_https(self):
        self.assertEqual(
            _normalize_endpoint("https://eryu.example.test/"),
            "https://eryu.example.test",
        )
        with self.assertRaises(ValueError):
            _normalize_endpoint("https://user:secret@eryu.example.test")
        with self.assertRaises(ValueError):
            _normalize_endpoint("https://eryu.example.test\nhttps://attacker.test")
        with self.assertRaises(ValueError):
            _normalize_endpoint("http://eryu.example.test")

    def test_basic_auth_requires_both_parts_and_token_is_strong(self):
        with self.assertRaises(ValueError):
            PresenceHttpClient(
                "https://eryu.example.test",
                "x" * 32,
                basic_auth_user="reader",
            )
        with self.assertRaises(ValueError):
            PresenceHttpClient("https://eryu.example.test", "short")


class SingleInstanceTest(unittest.TestCase):
    def test_only_one_reader_can_hold_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "reader.lock"
            first = SingleInstanceGuard(lock_path)
            second = SingleInstanceGuard(lock_path)
            third = SingleInstanceGuard(lock_path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
            third.release()


if __name__ == "__main__":
    unittest.main()
