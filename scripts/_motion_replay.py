#!/usr/bin/env python3
"""Offline replay of an accelerometer capture through the gesture detector.

Runs :class:`custom_components.colmi_ring.gestures.GestureDetector` over a
capture produced by ``scripts/_accel_probe.py`` (or any CSV with
``t,x,y,z`` in g) so thresholds can be tuned without touching the ring.

Nothing here imports Home Assistant; the codec and detector are pure, so this
runs on a plain Python with nothing but the repo checked out.

    python scripts/_motion_replay.py data/capture.csv --plot-out data/plots
    python scripts/_motion_replay.py data/capture.csv --sensitivity 0.65
    python scripts/_motion_replay.py data/capture.csv --events-only

Input formats
-------------
* ``_accel_probe.py`` capture: one JSON object per line with a ``packets``
  list of ``{"t": ..., "type": [cmd, sub], "hex": "..."}``; the axes are
  decoded exactly as the integration decodes them, so a wrong packing guess in
  ``colmi_codec/motion.py`` shows up here as a flat or absurd signal.
* CSV: ``t,x,y,z`` headers, or a headerless first row of four numbers. ``t``
  may be epoch seconds or an offset; it is rebased to zero either way.

Known limitation: the detector is wall-clock driven, so a capture whose
timestamps run faster than real time (a burst dump) compresses every time
constant. Pass ``--resample`` to re-time the stream at the median gap instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """Register *name* as a package without executing its ``__init__``.

    The real ``custom_components.colmi_ring/__init__.py`` is a Home Assistant
    integration and imports ``homeassistant``, which a plain dev checkout does
    not have. The codec and the detector are pure, though, so pointing the
    import machinery at the directory lets them load on their own.
    """
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


if "custom_components.colmi_ring" not in sys.modules:  # pragma: no cover
    _bootstrap_package("custom_components", REPO_ROOT / "custom_components")
    _bootstrap_package(
        "custom_components.colmi_ring",
        REPO_ROOT / "custom_components" / "colmi_ring",
    )

from custom_components.colmi_ring.colmi_codec.motion import (  # noqa: E402
    AccelSample,
    parse_accel_packet,
)
from custom_components.colmi_ring.gestures import (  # noqa: E402
    GESTURE_CLAP,
    GESTURE_LOWER,
    GESTURE_RAISE,
    GestureDetector,
)

ALL_GESTURES = (GESTURE_CLAP, GESTURE_RAISE, GESTURE_LOWER)


@dataclass(slots=True)
class Row:
    """One (time, sample) pair from a capture."""

    t: float
    sample: AccelSample


def load_rows(path: Path) -> list[Row]:
    """Load samples from a JSON capture or a CSV, rebased to t=0."""
    text = path.read_text(encoding="utf-8")
    rows = (
        _load_capture(text) if path.suffix.lower() == ".json" else _load_csv(text)
    )
    if not rows:
        msg = f"no accelerometer samples found in {path}"
        raise ValueError(msg)
    start = rows[0].t
    return [Row(row.t - start, row.sample) for row in rows]


def _load_capture(text: str) -> list[Row]:
    """Decode an ``_accel_probe.py`` capture, using the integration's decoder."""
    rows: list[Row] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        record: dict[str, Any] = json.loads(line)
        for packet in record.get("packets", []):
            raw = bytearray.fromhex(packet["hex"])
            sample = parse_accel_packet(raw)
            if sample is not None:
                rows.append(Row(float(packet["t"]), sample))
    return sorted(rows, key=lambda row: row.t)


def _load_csv(text: str) -> list[Row]:
    rows: list[Row] = []
    for record in csv.reader(text.splitlines()):
        if len(record) < 4:
            continue
        try:
            t, x, y, z = (float(value) for value in record[:4])
        except ValueError:
            continue  # header row
        rows.append(Row(t, AccelSample(x=x, y=y, z=z)))
    return rows


def _resample(rows: list[Row]) -> list[Row]:
    """Re-time the stream at its median gap, undoing a burst-dump compression."""
    gaps = _diffs([row.t for row in rows])
    if not gaps:
        return rows
    step = statistics.median(gaps)
    return [Row(i * step, row.sample) for i, row in enumerate(rows)]


def _diffs(values: list[float]) -> list[float]:
    """Successive differences, dropping any that are not positive."""
    return [
        later - earlier
        for earlier, later in zip(values, values[1:], strict=False)
        if later > earlier
    ]


def report_signal(rows: list[Row]) -> None:
    """Sanity-check the decoded signal before trusting any detection result."""
    magnitudes = [row.sample.magnitude_g for row in rows]
    print("\n## Signal")
    print(
        f"  {len(rows)} samples over {rows[-1].t:.1f}s"
        f" = {(len(rows) - 1) / rows[-1].t:.1f} Hz"
        if rows[-1].t > 0
        else f"  {len(rows)} samples (zero-length span)"
    )
    print(
        f"  magnitude |a| g: min {min(magnitudes):.2f}"
        f" median {statistics.median(magnitudes):.2f}"
        f" max {max(magnitudes):.2f}"
    )
    for axis in ("x", "y", "z"):
        values = [getattr(row.sample, axis) for row in rows]
        print(
            f"  {axis}: min {min(values):+.2f} median"
            f" {statistics.median(values):+.2f} max {max(values):+.2f}"
            f" (stdev {statistics.pstdev(values):.2f})"
        )
    print(
        "  note: a resting wrist should sit near |a| = 1.0 g. If it does not,"
        " the scale or packing guess is wrong."
    )


def run_detector(
    rows: list[Row], sensitivity: float, stream_clap: bool
) -> dict[str, list[float]]:
    """Feed the whole capture through one detector; return fire times."""
    detector = GestureDetector(sensitivity=sensitivity, stream_clap=stream_clap)
    fired: dict[str, list[float]] = {gesture: [] for gesture in ALL_GESTURES}
    for row in rows:
        for event in detector.feed(row.sample, now=row.t):
            fired.setdefault(event.gesture, []).append(row.t)
            print(
                f"  t={row.t:8.2f}s  {event.gesture:<10} source={event.source}"
                f"  peak={event.peak_g:.2f}g"
                f"  v={event.velocity_g_s:+.3f} g*s"
            )
    print(f"\n  detector saw {detector.events_seen} event(s)")
    if detector.sample_rate_hz is not None:
        print(f"  measured rate {detector.sample_rate_hz:.1f} Hz")
    return fired


def report_gestures(fired: dict[str, list[float]]) -> None:
    print("\n## Detections")
    if not any(fired.values()):
        print("  none -- lower --sensitivity, or check the signal above")
        return
    for gesture in ALL_GESTURES:
        times = fired.get(gesture, [])
        print(f"  {gesture:<10} {len(times):>3}" + _spacing(times))


def _spacing(times: list[float]) -> str:
    gaps = _diffs(times)
    if not gaps:
        return ""
    return f"  (min gap {min(gaps):.2f}s, median {statistics.median(gaps):.2f}s)"


def write_plots(rows: list[Row], fired: dict[str, list[float]], out_dir: Path) -> None:
    """Optional matplotlib plots; skipped quietly when matplotlib is absent."""
    try:
        import matplotlib  # noqa: PLC0415

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except ImportError:
        print("\n(plt) matplotlib is not installed; skipping plots")
        return

    times = [row.t for row in rows]
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    for axis, ax in zip(("x", "y", "z"), axes, strict=True):
        ax.plot(times, [getattr(row.sample, axis) for row in rows], lw=0.6)
        ax.set_ylabel(f"{axis} (g)")
        for gesture, at in fired.items():
            for t in at:
                ax.axvline(t, ls=":", lw=0.8, label=gesture)
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper right", fontsize=8)
    axes[-1].set_xlabel("seconds")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "accel.png"
    fig.savefig(target, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\n(plt) wrote {target}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("capture", type=Path, help="JSON capture or CSV of samples")
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=1.0,
        help="detector sensitivity multiplier (higher fires sooner)",
    )
    parser.add_argument(
        "--no-stream-clap",
        action="store_true",
        help="disable the stream-derived clap detector",
    )
    parser.add_argument(
        "--resample",
        action="store_true",
        help="re-time the stream at its median gap (undoes burst dumping)",
    )
    parser.add_argument(
        "--events-only",
        action="store_true",
        help="skip the signal report and plots",
    )
    parser.add_argument(
        "--plot-out",
        type=Path,
        default=None,
        help="directory for accel.png (no plots if omitted)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = load_rows(args.capture)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"cannot read {args.capture}: {error}", file=sys.stderr)
        return 2

    print(f"## {args.capture}")
    if not args.events_only:
        report_signal(rows)
    if args.resample:
        rows = _resample(rows)

    print("\n## Events")
    if not any(math.isfinite(row.sample.x) for row in rows):
        print("  non-finite samples present; results are meaningless")
    fired = run_detector(rows, args.sensitivity, not args.no_stream_clap)
    report_gestures(fired)

    if args.plot_out is not None and not args.events_only:
        write_plots(rows, fired, args.plot_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
