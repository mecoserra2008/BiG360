"""Stream workers (R1.2). Registry: name -> class (see `all_streams()`)."""

from __future__ import annotations

from typing import Any


def registry() -> dict[str, Any]:
    from app.realtime.streams.disclosures import DartStream, FcaNsmStream, HkexStream, TdnetStream
    from app.realtime.streams.edgar import EdgarStream
    from app.realtime.streams.gdelt import GdeltStream
    from app.realtime.streams.macro_capture import MacroCaptureStream
    from app.realtime.streams.news import PolygonNewsStream, RssStream

    return {c.name: c for c in (EdgarStream, FcaNsmStream, TdnetStream, HkexStream, DartStream, RssStream,
                                MacroCaptureStream, GdeltStream, PolygonNewsStream)}
