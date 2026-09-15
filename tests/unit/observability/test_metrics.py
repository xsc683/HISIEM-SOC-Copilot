from __future__ import annotations

from typing import Any

from hisiem_soc_copilot.infrastructure.observability import metrics


class _Counter:
    def __init__(self) -> None:
        self.measurements: list[tuple[int, dict[str, str]]] = []

    def add(self, value: int, attributes: dict[str, str]) -> None:
        self.measurements.append((value, attributes))


class _Meter:
    def __init__(self) -> None:
        self.counter = _Counter()
        self.created: list[str] = []

    def create_counter(self, *, name: str, unit: str) -> _Counter:
        assert unit == "1"
        self.created.append(name)
        return self.counter


def test_metric_dimensions_are_bounded_and_unknown_dimensions_drop() -> None:
    assert metrics.sanitize_metric_attributes(
        {"tool_name": "hisiem.search_events", "tool_provider": "native"}
    ) == {"tool_name": "hisiem.search_events", "tool_provider": "native"}
    assert metrics.sanitize_metric_attributes({"investigation_id": "inv-1"}) is None
    assert metrics.sanitize_metric_attributes({"tool_name": "tenant-controlled"}) is None
    assert metrics.sanitize_metric_attributes({"operation": "graph.invoke"}) == {
        "operation": "graph.invoke"
    }


def test_counter_rejects_unbounded_labels_before_recording(monkeypatch: Any) -> None:
    meter = _Meter()
    monkeypatch.setattr(metrics, "_METER", meter)
    monkeypatch.setattr(metrics, "_INSTRUMENTS", {})

    metrics.record_counter(
        "tool.calls",
        attributes={"tool_name": "hisiem.search_events", "request_id": "req-1"},
    )
    assert meter.created == []

    metrics.record_counter(
        "tool.calls",
        attributes={"tool_name": "hisiem.search_events", "tool_provider": "native"},
    )
    assert meter.created == ["tool.calls"]
    assert meter.counter.measurements == [
        (1, {"tool_name": "hisiem.search_events", "tool_provider": "native"})
    ]


def test_meter_failure_is_best_effort(monkeypatch: Any) -> None:
    class BrokenMeter:
        def create_counter(self, **_kwargs: object) -> object:
            raise RuntimeError("exporter unavailable")

    monkeypatch.setattr(metrics, "_METER", BrokenMeter())
    monkeypatch.setattr(metrics, "_INSTRUMENTS", {})
    metrics.record_counter("llm.calls", attributes={"model_provider": "command_code"})
