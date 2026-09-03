from __future__ import annotations

import json

from app.services.orchestration_budget import NoProgressDetector
from app.tools.registry import ToolExecution


def execution(
    query: str, items: list[str], *, cached: bool = False, limit: int = 10
) -> ToolExecution:
    return ToolExecution(
        name="search_library",
        arguments={"query": query, "limit": limit},
        output=json.dumps({"ok": True, "cached": cached, "result": {"items": items}}),
        summary={},
        duration_ms=1,
        status="completed",
    )


def test_rephrased_calls_with_reordered_equivalent_results_do_not_look_like_progress() -> None:
    detector = NoProgressDetector()
    assert not detector.observe(execution("first query", ["recording A", "recording B"]))
    assert not detector.observe(
        execution("second query", ["recording B", "recording A"], cached=True)
    )
    assert detector.observe(execution("third query", ["recording A", "recording B"]))


def test_distinct_missing_tracks_do_not_look_like_a_loop() -> None:
    detector = NoProgressDetector()
    for index in range(49):
        assert not detector.observe(execution(f"artist and track {index}", []))


def test_larger_limit_with_new_candidates_resets_repetition() -> None:
    detector = NoProgressDetector()
    assert not detector.observe(execution("track", ["one"], limit=1))
    assert not detector.observe(execution("track", ["one"], limit=2))
    assert not detector.observe(execution("track", ["one", "two"], limit=3))
    assert not detector.observe(execution("track", ["one", "two"], limit=4))
    assert detector.observe(execution("track", ["one", "two"], limit=5))


def test_interleaved_legitimate_tracks_reset_no_progress_streak() -> None:
    detector = NoProgressDetector()
    for query in ("A", "B", "A", "B", "A", "B", "A"):
        assert not detector.observe(execution(query, [query]))


def test_multiple_identical_calls_in_one_round_do_not_count_as_multiple_stalled_rounds() -> None:
    detector = NoProgressDetector()
    for _ in range(5):
        assert not detector.observe(execution("track", ["one"]), round_number=1)
    assert not detector.observe(execution("track", ["one"]), round_number=2)
    assert detector.observe(execution("track", ["one"]), round_number=3)


def test_case_sensitive_provider_ids_remain_distinct_in_arguments_and_results() -> None:
    detector = NoProgressDetector()
    for identifier in ("AbCdEf", "abcdef", "ABCDEF", "AbCdEf"):
        observed = ToolExecution(
            name="probe_media_source",
            arguments={"evidence_id": identifier},
            output=json.dumps({"ok": True, "result": {"items": [{"source_id": identifier}]}}),
            summary={},
            duration_ms=1,
            status="completed",
        )
        assert not detector.observe(observed)
    # Even identical empty/error results must not merge distinct executable IDs.
    detector = NoProgressDetector()
    for identifier in ("AbCdEf", "abcdef", "ABCDEF"):
        observed = ToolExecution(
            name="probe_media_source",
            arguments={"evidence_id": identifier},
            output='{"ok":true,"result":{"items":[]}}',
            summary={},
            duration_ms=1,
            status="completed",
        )
        assert not detector.observe(observed)
