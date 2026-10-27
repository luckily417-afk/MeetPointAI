"""Record / Replay：离线回放、版本校验、缺数据报错。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from diagnostics.cases import CaseConfig
from diagnostics.recording import DATA_VERSION, Recording, RecordingError
from diagnostics.runner import RunOptions, run_search
from diagnostics.sources import Budget, RecordingAMap, ReplayAMap, ReplayMiss, ReplayDeepSeek
from diagnostics.mockdata import generate_pois
from geometry.regions import CircleRegion
from services.deepseek import DeepSeekError


def _case(name: str = "rr") -> CaseConfig:
    return CaseConfig.model_validate(
        {
            "name": name,
            "a": {"name": "A", "lng": 117.210089, "lat": 39.135951},
            "b": {"name": "B", "lng": 117.216000, "lat": 39.140000},
            "region": {"mode": "circle", "center": [117.2115, 39.136], "radius_m": 600},
            "top_n": 3,
            "rec_mode": "ai",
        }
    )


def _ids(outcome) -> list[str]:
    result = outcome.result
    if not hasattr(result, "recommendations"):
        return []
    return [rec.poi.id for rec in result.recommendations]


def test_record_then_replay_is_deterministic(tmp_path):
    case = _case()
    record_dir = tmp_path / "rec"
    first = asyncio.run(run_search(case, RunOptions(mock=True, record=str(record_dir))))
    assert first.status == "ok", first.errors
    assert first.recording_path and Path(first.recording_path).is_file()
    original_ids = _ids(first)
    assert original_ids

    replay = asyncio.run(
        run_search(case, RunOptions(replay=first.recording_path, ai_mode="mock", offline=True))
    )
    assert replay.status == "ok", replay.errors
    assert replay.data_mode == "replay"
    assert _ids(replay) == original_ids


def test_replay_missing_request_raises():
    empty = Recording()
    amap = ReplayAMap(empty, budget=Budget())
    with pytest.raises(ReplayMiss) as exc:
        asyncio.run(amap.place_around((117.21, 39.13), 500))
    assert exc.value.code == "REPLAY_MISS"


def test_replay_data_version_mismatch(tmp_path):
    payload = {"schema_version": "1.0", "data_version": DATA_VERSION + 1, "amap": {}, "deepseek": []}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecordingError) as exc:
        Recording.load(path)
    assert exc.value.code == "RECORDING_VERSION_MISMATCH"


def test_replay_deepseek_without_recording_raises():
    ds = ReplayDeepSeek(Recording())
    with pytest.raises(DeepSeekError):
        asyncio.run(ds.chat_json([{"role": "user", "content": "{}"}]))


def test_replay_deepseek_rejects_unmatched_prompt():
    recording = Recording()
    recording.add_deepseek(
        [{"role": "user", "content": "original"}],
        {"content": "recorded", "data": {"items": []}},
        {},
    )
    ds = ReplayDeepSeek(recording)
    with pytest.raises(DeepSeekError, match="没有与当前请求匹配"):
        asyncio.run(ds.chat_json([{"role": "user", "content": "different"}]))


def test_recording_wrapper_captures_requests(tmp_path):
    from diagnostics.sources import MockAMap

    recording = Recording(case={"name": "wrap"})
    source = RecordingAMap(MockAMap(budget=Budget(), seed_text="wrap"), recording)
    pois = asyncio.run(source.place_around((117.21, 39.13), 500, page=1, offset=25))
    assert pois.pois
    assert recording.amap, "应记录至少一个请求"
    entry = next(iter(recording.amap.values()))
    assert entry["kind"] == "around"
    assert entry["payload"]["pois"]


def test_replay_recording_roundtrip_file(tmp_path):
    case = _case("roundtrip")
    rec = Recording(case=case.describe(), run_id="run-1")
    rec.add_amap("around", "around:abc", {"total": 0, "page": 1, "offset": 25, "pois": []}, {"center": [117.2, 39.1]})
    path = rec.save(tmp_path)
    loaded = Recording.load(path)
    assert loaded.amap["around:abc"]["kind"] == "around"
    assert loaded.data_version == DATA_VERSION
