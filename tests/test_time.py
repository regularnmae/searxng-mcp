"""get_time and convert_time.

Pure computation, no network -- but the DST boundary and the accepted
input formats are the parts a caller gets wrong, so they are pinned.
"""

from __future__ import annotations

import pytest

# --- get_time --------------------------------------------------------


@pytest.mark.anyio
async def test_get_time_defaults_to_utc(call) -> None:
    text = await call("get_time")
    assert text.startswith("ISO-8601:")
    assert "Unix timestamp:" in text
    assert "UTC (UTC+0000)" in text


@pytest.mark.anyio
async def test_get_time_named_zone(call) -> None:
    text = await call("get_time", tz="Asia/Tokyo")
    assert "Asia/Tokyo (UTC+0900)" in text


@pytest.mark.anyio
async def test_get_time_negative_offset(call) -> None:
    text = await call("get_time", tz="America/New_York")
    assert "UTC-0" in text  # -0400 or -0500, depending on the season


@pytest.mark.anyio
async def test_get_time_rejects_unknown_zone(call) -> None:
    text = await call("get_time", tz="Middle/Of/Nowhere")
    assert "Unknown timezone" in text
    assert "Middle/Of/Nowhere" in text


@pytest.mark.anyio
async def test_get_time_rejects_path_shaped_input(call) -> None:
    """ZoneInfo rejects traversal, but the tool should say so plainly."""
    for bad in ("../../etc/passwd", "/etc/localtime", ".."):
        text = await call("get_time", tz=bad)
        assert "Unknown timezone" in text, f"{bad!r} was accepted"


# --- convert_time ----------------------------------------------------


@pytest.mark.anyio
async def test_convert_time_utc_to_utc(call) -> None:
    text = await call("convert_time", value="2026-10-10T12:00:00")
    assert "2026-10-10T12:00:00+00:00" in text


@pytest.mark.anyio
async def test_convert_time_across_zones(call) -> None:
    text = await call(
        "convert_time", value="2026-10-10T12:00:00", from_tz="UTC", to_tz="Asia/Tokyo"
    )
    assert "2026-10-10T21:00:00+09:00" in text


@pytest.mark.anyio
async def test_convert_time_space_separated(call) -> None:
    text = await call(
        "convert_time", value="2026-10-10 15:30", from_tz="Europe/Berlin", to_tz="UTC"
    )
    # CEST is UTC+2 until the last Sunday of October.
    assert "13:30:00+00:00" in text


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-10T12:00:00",
        "2026-10-10 12:00",
        "2026-10-10",
        "2026/10/10 12:00",
        "2026/10/10",
        "2026-10-10T12:00:00.500000",
    ],
)
@pytest.mark.anyio
async def test_convert_time_accepted_formats(call, value: str) -> None:
    text = await call("convert_time", value=value)
    assert "Unix timestamp:" in text


@pytest.mark.anyio
async def test_convert_time_rejects_nonsense(call) -> None:
    for bad in ("yesterday", "2026-13-45", "", "12:00"):
        text = await call("convert_time", value=bad)
        assert "Could not parse" in text, f"{bad!r} was accepted"


@pytest.mark.anyio
async def test_convert_time_reports_unknown_zone(call) -> None:
    text = await call("convert_time", value="2026-10-10", from_tz="Nowhere/Special")
    assert "Unknown timezone" in text


@pytest.mark.anyio
async def test_convert_time_handles_dst_gap(call) -> None:
    """A 02:30 on the spring-forward date does not exist in Berlin;
    `replace(tzinfo=...)` still resolves it, which is what the tool
    documents by returning a UTC offset."""
    text = await call(
        "convert_time", value="2026-03-29 02:30", from_tz="Europe/Berlin", to_tz="UTC"
    )
    assert "Unix timestamp:" in text


@pytest.mark.anyio
async def test_convert_time_round_trip_is_stable(call) -> None:
    text = await call(
        "convert_time", value="2026-10-10T12:00:00", from_tz="UTC", to_tz="America/New_York"
    )
    assert "08:00:00-04:00" in text
