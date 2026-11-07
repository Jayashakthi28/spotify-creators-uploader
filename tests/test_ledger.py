"""Ledger round-trip, multi-show keying, corruption tolerance and private-file tests."""

from __future__ import annotations

import json
import stat

from spotify_uploader.ledger import Ledger
from spotify_uploader.models import PublishedEpisode, PublishStatus


def _episode(
    sha256: str = "a" * 64, title: str = "Episode", show_id: str = "show1"
) -> PublishedEpisode:
    return PublishedEpisode(
        status=PublishStatus.PUBLISHED,
        title=title,
        show_id=show_id,
        sha256=sha256,
        published_at="2026-01-01T00:00:00+00:00",
        episode_id="episode1",
        url=f"https://creators.spotify.com/pod/show/{show_id}/episode/episode1",
    )


def test_record_get_and_all_round_trip(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.json")
    first = _episode("a" * 64, "First")
    second = _episode("b" * 64, "Second")

    assert ledger.get(first.sha256) is None
    ledger.record(first)
    ledger.record(second)

    assert ledger.get(first.sha256) == first
    assert ledger.get(second.sha256) == second
    assert sorted(episode.title for episode in ledger.all()) == ["First", "Second"]


def test_recording_the_same_sha_twice_replaces_it(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.record(_episode("a" * 64, "Old"))
    ledger.record(_episode("a" * 64, "New"))

    assert ledger.get("a" * 64) == _episode("a" * 64, "New")
    assert ledger.get("a" * 64, "show1") == _episode("a" * 64, "New")
    assert len(ledger.all()) == 1


def test_the_same_video_can_be_recorded_for_two_shows(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.json")
    first = _episode("a" * 64, "On show1", show_id="show1")
    second = _episode("a" * 64, "On show2", show_id="show2")

    ledger.record(first)
    ledger.record(second)

    assert ledger.get("a" * 64, "show1") == first
    assert ledger.get("a" * 64, "show2") == second
    assert ledger.get("a" * 64, "show3") is None
    assert len(ledger.all()) == 2


def test_a_legacy_bare_sha_entry_is_found_through_its_recorded_show(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    legacy = _episode("a" * 64, "Legacy")
    path.write_text(json.dumps({legacy.sha256: legacy.to_dict()}), encoding="utf-8")
    ledger = Ledger(path)

    assert ledger.get(legacy.sha256, "show1") == legacy
    assert ledger.get(legacy.sha256, "show2") is None
    assert ledger.get(legacy.sha256) == legacy
    assert ledger.all() == [legacy]


def test_recording_migrates_a_matching_legacy_entry_to_the_show_key(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    legacy = _episode("a" * 64, "Legacy")
    path.write_text(json.dumps({legacy.sha256: legacy.to_dict()}), encoding="utf-8")
    ledger = Ledger(path)

    updated = _episode("a" * 64, "Updated")
    ledger.record(updated)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert legacy.sha256 not in raw
    assert raw[f"{updated.show_id}:{updated.sha256}"]["title"] == "Updated"
    assert ledger.get(legacy.sha256, "show1") == updated
    assert len(ledger.all()) == 1


def test_recording_never_drops_a_legacy_entry_for_another_show(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    other = _episode("a" * 64, "Other show", show_id="show2")
    path.write_text(json.dumps({other.sha256: other.to_dict()}), encoding="utf-8")
    ledger = Ledger(path)

    ledger.record(_episode("a" * 64, "Mine"))

    assert ledger.get("a" * 64, "show2") == other
    assert ledger.get("a" * 64, "show1") == _episode("a" * 64, "Mine")
    assert len(ledger.all()) == 2


def test_all_returns_a_legacy_and_show_keyed_duplicate_once(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    episode = _episode("a" * 64, "Same")
    path.write_text(
        json.dumps(
            {
                episode.sha256: episode.to_dict(),
                f"{episode.show_id}:{episode.sha256}": episode.to_dict(),
            }
        ),
        encoding="utf-8",
    )

    assert Ledger(path).all() == [episode]


def test_corrupt_ledger_is_tolerated(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{torn json", encoding="utf-8")
    ledger = Ledger(path)

    assert ledger.get("a" * 64) is None
    assert ledger.get("a" * 64, "show1") is None
    assert ledger.all() == []


def test_recording_over_a_corrupt_ledger_rewrites_it(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{torn json", encoding="utf-8")
    ledger = Ledger(path)
    episode = _episode()

    ledger.record(episode)

    assert ledger.get(episode.sha256) == episode
    assert ledger.get(episode.sha256, episode.show_id) == episode
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw[f"{episode.show_id}:{episode.sha256}"]["status"] == "published"


def test_invalid_entries_are_skipped(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "missing_fields": {"title": "x"},
                "bad_status": {
                    "status": "not-a-status",
                    "title": "x",
                    "show_id": "s",
                    "sha256": "missing_fields",
                    "published_at": "now",
                },
                "not-an-object": "value",
            }
        ),
        encoding="utf-8",
    )
    ledger = Ledger(path)

    assert ledger.get("missing_fields") is None
    assert ledger.get("missing_fields", "s") is None
    assert ledger.all() == []


def test_ledger_file_is_private_and_nested_directory_is_created(tmp_path) -> None:
    path = tmp_path / "nested" / "state" / "ledger.json"
    Ledger(path).record(_episode())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_all_ignores_non_object_top_level(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

    assert Ledger(path).all() == []
