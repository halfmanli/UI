"""
perfetto_trace.py - Zero-dependency Perfetto trace generator.

Supports:
  - Hierarchical tracks (unlimited nesting, custom names)
  - Overlapping events on the same track
  - Flow arrows connecting events across tracks
  - Instant events (point-in-time markers)
  - Counter tracks (numeric line charts)
  - Custom args on events and instants (shown in Perfetto detail panel)
  - Zero-anchor: timeline always starts at t=0 (matches text logs)

Usage:
    from perfetto_trace import PerfettoTrace

    trace = PerfettoTrace(time_unit="us")

    # Build track hierarchy (unlimited depth)
    soc  = trace.add_track("SoC Pipeline")
    noc  = trace.add_track("NoC Subsystem", parent=soc)
    rtr0 = trace.add_track("Router 0",      parent=noc)
    mem  = trace.add_track("MemCtrl",        parent=soc)

    # Counter track: just set counter=True
    qdepth = trace.add_track("Queue Depth", parent=rtr0, counter=True)

    # Events with args (overlapping on same track is OK)
    e1 = trace.add_event(rtr0, "Pkt#1", start=0,  end=80,
                         args={"pkt_id": 1, "size": 256, "src": "DMA"})
    e2 = trace.add_event(rtr0, "Pkt#2", start=30, end=120)
    e3 = trace.add_event(mem,  "Pkt#1", start=90, end=200,
                         args={"pkt_id": 1, "channel": 0})

    # Flow arrows
    trace.add_flow([e1, e3])

    # Instant with args
    trace.add_instant(rtr0, "CRC Error", ts=50,
                      args={"port": 3, "expected": "0xDEAD"})

    # Counter data points
    trace.add_counter(qdepth, ts=0,  value=0)
    trace.add_counter(qdepth, ts=30, value=1)

    # Save
    trace.save("output.perfetto-trace")

Requires: Python >= 3.9 (only uses struct from stdlib)
"""

from __future__ import annotations

import struct
from typing import Any


# ====================================================================
# Type aliases
# ====================================================================

TrackHandle = int
EventHandle = int

# Supported arg value types for DebugAnnotation
ArgValue = str | int | float | bool
Args = dict[str, ArgValue | dict[str, Any]] | None


# ====================================================================
# Protobuf wire-format encoder (zero external dependencies)
# ====================================================================

def _encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    if value < 0:
        value = value & 0xFFFFFFFFFFFFFFFF
    result: list[int] = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _field_varint(field_number: int, value: int) -> bytes:
    """Wire type 0: varint."""
    return _encode_varint((field_number << 3) | 0) + _encode_varint(value)


def _field_fixed64(field_number: int, value: int) -> bytes:
    """Wire type 1: 64-bit fixed."""
    return _encode_varint((field_number << 3) | 1) + struct.pack('<Q', value)


def _field_double(field_number: int, value: float) -> bytes:
    """Wire type 1: double (64-bit float)."""
    return _encode_varint((field_number << 3) | 1) + struct.pack('<d', value)


def _field_bytes(field_number: int, data: bytes) -> bytes:
    """Wire type 2: length-delimited bytes."""
    return (_encode_varint((field_number << 3) | 2)
            + _encode_varint(len(data))
            + data)


def _field_string(field_number: int, s: str) -> bytes:
    """Wire type 2: length-delimited string."""
    return _field_bytes(field_number, s.encode('utf-8'))


# ====================================================================
# DebugAnnotation encoder
# ====================================================================
# DebugAnnotation    { uint64 name_iid     = 1;   // interned name
#                      bool   bool_value   = 2;
#                      uint64 uint_value   = 3;
#                      int64  int_value    = 4;
#                      double double_value = 5;
#                      string string_value = 6;
#                      string name         = 10;  // non-interned name
#                      repeated DebugAnnotation dict_entries = 11; }

def _build_debug_annotation(name: str, value: Any) -> bytes:
    """Build a single DebugAnnotation message."""
    msg = _field_string(10, name)  # field 10 = non-interned name

    if isinstance(value, bool):
        # bool must be checked before int (bool is subclass of int)
        msg += _field_varint(2, 1 if value else 0)
    elif isinstance(value, int):
        msg += _field_varint(4, value)
    elif isinstance(value, float):
        msg += _field_double(5, value)
    elif isinstance(value, str):
        msg += _field_string(6, value)
    elif isinstance(value, dict):
        for k, v in value.items():
            child = _build_debug_annotation(str(k), v)
            msg += _field_bytes(11, child)
    else:
        # Fallback: convert to string
        msg += _field_string(6, str(value))

    return msg


def _build_debug_annotations(args: dict[str, Any]) -> bytes:
    """Build all DebugAnnotation entries for a TrackEvent."""
    msg = b''
    for key, val in args.items():
        annotation = _build_debug_annotation(str(key), val)
        # TrackEvent field 4: repeated DebugAnnotation debug_annotations
        msg += _field_bytes(4, annotation)
    return msg


# ====================================================================
# Perfetto protobuf message builders
# ====================================================================
# Proto field numbers reference:
#
# Trace              { repeated TracePacket packet = 1; }
#
# TracePacket        { uint64  timestamp                  = 8;
#                      uint32  trusted_packet_sequence_id = 10;
#                      TrackEvent      track_event        = 11;
#                      uint32  sequence_flags              = 13;
#                      TrackDescriptor track_descriptor    = 60; }
#                    sequence_flags: 1 = SEQ_INCREMENTAL_STATE_CLEARED
#
# TrackDescriptor    { uint64  uuid        = 1;
#                      string  name        = 2;
#                      uint64  parent_uuid = 5;
#                      CounterDescriptor counter = 8; }
#
# CounterDescriptor  { (empty sub-message marks track as counter) }
#
# TrackEvent         { repeated DebugAnnotation debug_annotations = 4;
#                      Type    type                        = 9;
#                      uint64  track_uuid                 = 11;
#                      string  name                        = 23;
#                      int64   counter_value               = 30;
#                      double  double_counter_value        = 44;
#                      repeated fixed64 flow_ids           = 47;
#                      repeated fixed64 terminating_flow_ids = 48; }
#
# TrackEvent.Type    { SLICE_BEGIN=1; SLICE_END=2; INSTANT=3; COUNTER=4; }


def _build_track_descriptor(
    uuid: int,
    name: str,
    parent_uuid: int | None = None,
    is_counter: bool = False,
) -> bytes:
    msg = _field_varint(1, uuid)
    msg += _field_string(2, name)
    if parent_uuid is not None:
        msg += _field_varint(5, parent_uuid)
    if is_counter:
        msg += _field_bytes(8, b'')  # empty CounterDescriptor
    return msg


def _build_track_event(
    track_uuid: int,
    event_type: int,
    name: str | None = None,
    counter_value: int | float | None = None,
    flow_ids: list[int] | None = None,
    terminating_flow_ids: list[int] | None = None,
    args: dict[str, Any] | None = None,
) -> bytes:
    msg = _field_varint(11, track_uuid)
    msg += _field_varint(9, event_type)
    if name is not None:
        msg += _field_string(23, name)
    if counter_value is not None:
        if isinstance(counter_value, float) and not counter_value.is_integer():
            msg += _field_double(44, counter_value)       # double_counter_value
        else:
            msg += _field_varint(30, int(counter_value))  # int64 counter_value
    if flow_ids:
        for fid in flow_ids:
            msg += _field_fixed64(47, fid)
    if terminating_flow_ids:
        for fid in terminating_flow_ids:
            msg += _field_fixed64(48, fid)
    if args:
        msg += _build_debug_annotations(args)
    return msg


def _build_trace_packet(
    timestamp_ns: int | None = None,
    track_descriptor: bytes | None = None,
    track_event: bytes | None = None,
    sequence_id: int = 0,
    sequence_flags: int = 0,
) -> bytes:
    msg = b''
    if timestamp_ns is not None:
        msg += _field_varint(8, timestamp_ns)
    if sequence_id:
        msg += _field_varint(10, sequence_id)
    if sequence_flags:
        msg += _field_varint(13, sequence_flags)
    if track_descriptor is not None:
        msg += _field_bytes(60, track_descriptor)
    if track_event is not None:
        msg += _field_bytes(11, track_event)
    return msg


# ====================================================================
# Internal data structures
# ====================================================================

class _TrackInfo:
    __slots__ = ("uuid", "name", "parent_uuid", "is_counter")

    def __init__(self, uuid: int, name: str,
                 parent_uuid: int | None, is_counter: bool) -> None:
        self.uuid = uuid
        self.name = name
        self.parent_uuid = parent_uuid
        self.is_counter = is_counter


class _EventInfo:
    __slots__ = ("track_uuid", "name", "start_ns", "end_ns",
                 "flow_ids", "term_flow_ids", "seq_id", "args")

    def __init__(self, track_uuid: int, name: str,
                 start_ns: int, end_ns: int, seq_id: int,
                 args: dict[str, Any] | None) -> None:
        self.track_uuid = track_uuid
        self.name = name
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.flow_ids: list[int] = []
        self.term_flow_ids: list[int] = []
        self.seq_id = seq_id
        self.args = args


class _InstantInfo:
    __slots__ = ("track_uuid", "name", "ts_ns", "seq_id", "args")

    def __init__(self, track_uuid: int, name: str,
                 ts_ns: int, seq_id: int,
                 args: dict[str, Any] | None) -> None:
        self.track_uuid = track_uuid
        self.name = name
        self.ts_ns = ts_ns
        self.seq_id = seq_id
        self.args = args


# ====================================================================
# PerfettoTrace - the main helper class
# ====================================================================

class PerfettoTrace:
    """
    Build Perfetto-native trace files with zero external dependencies.

    All timestamps use the unit specified in the constructor.
    Supported units: "ns", "us" (default), "ms", "s".
    """

    _TIME_SCALES: dict[str, int] = {
        "ns": 1,
        "us": 1_000,
        "ms": 1_000_000,
        "s":  1_000_000_000,
    }

    def __init__(self, time_unit: str = "us") -> None:
        if time_unit not in self._TIME_SCALES:
            raise ValueError(
                "time_unit must be one of: {}".format(
                    ", ".join(self._TIME_SCALES.keys())))
        self._ts_scale: int = self._TIME_SCALES[time_unit]

        self._next_uuid: int = 1
        self._next_flow_id: int = 1
        self._next_event_id: int = 0
        self._next_seq_id: int = 1

        self._tracks: dict[int, _TrackInfo] = {}
        self._events: dict[int, _EventInfo] = {}
        self._instants: list[_InstantInfo] = []
        self._counter_seq_ids: dict[int, int] = {}
        self._counter_values: dict[int, list[tuple[int, int | float]]] = {}

    # ---- internal helpers ----

    def _alloc_uuid(self) -> int:
        uid = self._next_uuid
        self._next_uuid += 1
        return uid

    def _alloc_flow_id(self) -> int:
        fid = self._next_flow_id
        self._next_flow_id += 1
        return fid

    def _alloc_seq_id(self) -> int:
        sid = self._next_seq_id
        self._next_seq_id += 1
        return sid

    def _to_ns(self, ts: int | float) -> int:
        return int(ts * self._ts_scale)

    def _get_track(self, handle: TrackHandle) -> _TrackInfo:
        if handle not in self._tracks:
            raise ValueError("Invalid track handle: {}".format(handle))
        return self._tracks[handle]

    def _assert_not_counter(self, track: _TrackInfo) -> None:
        if track.is_counter:
            raise TypeError(
                "Track '{}' is a counter track. "
                "Use add_counter() instead of add_event()/add_instant().".format(
                    track.name))

    def _assert_is_counter(self, track: _TrackInfo) -> None:
        if not track.is_counter:
            raise TypeError(
                "Track '{}' is not a counter track. "
                "Use add_track(..., counter=True) to create a counter track.".format(
                    track.name))

    # ---- public API ----

    def add_track(
        self,
        name: str,
        parent: TrackHandle | None = None,
        counter: bool = False,
    ) -> TrackHandle:
        """
        Add a track (or grouping container).

        Args:
            name:    Display name in Perfetto UI.
            parent:  Parent track handle, or None for top-level.
            counter: If True, rendered as a line chart.
                     Only add_counter() can write data to it.

        Returns:
            Track handle (int).
        """
        uuid = self._alloc_uuid()
        if parent is not None and parent not in self._tracks:
            raise ValueError("Invalid parent track handle: {}".format(parent))
        self._tracks[uuid] = _TrackInfo(
            uuid=uuid,
            name=name,
            parent_uuid=parent,
            is_counter=counter,
        )
        if counter:
            self._counter_seq_ids[uuid] = self._alloc_seq_id()
            self._counter_values[uuid] = []
        return uuid

    def add_event(
        self,
        track: TrackHandle,
        name: str,
        start: int | float,
        end: int | float,
        args: Args = None,
    ) -> EventHandle:
        """
        Add a duration event on a track.
        Multiple events on the same track may overlap in time.

        Args:
            track: Track handle from add_track().
            name:  Event label (e.g. "Pkt#42").
            start: Start timestamp (in time_unit from constructor).
            end:   End timestamp.
            args:  Optional dict of custom attributes.
                   Shown in Perfetto detail panel when clicking the event.
                   Supports str, int, float, bool values, and nested dicts.
                   Example: {"pkt_id": 1, "size": 256, "src": "DMA",
                             "flags": {"urgent": True, "retry": 3}}

        Returns:
            Event handle (int). Use in add_flow() to connect events.

        Raises:
            TypeError:  If track is a counter track.
            ValueError: If end < start.
        """
        info = self._get_track(track)
        self._assert_not_counter(info)

        if end < start:
            raise ValueError(
                "Event '{}' has end ({}) < start ({}).".format(name, end, start))

        eid = self._next_event_id
        self._next_event_id += 1
        self._events[eid] = _EventInfo(
            track_uuid=track,
            name=name,
            start_ns=self._to_ns(start),
            end_ns=self._to_ns(end),
            seq_id=self._alloc_seq_id(),
            args=args,
        )
        return eid

    def add_flow(self, event_handles: list[EventHandle]) -> None:
        """
        Connect a sequence of events with flow arrows.

        Args:
            event_handles: Ordered list of event handles.
                           Arrows: event_handles[0] -> [1] -> [2] -> ...
        """
        if len(event_handles) < 2:
            return
        for i in range(len(event_handles) - 1):
            fid = self._alloc_flow_id()
            src = event_handles[i]
            dst = event_handles[i + 1]
            if src not in self._events:
                raise ValueError("Invalid event handle: {}".format(src))
            if dst not in self._events:
                raise ValueError("Invalid event handle: {}".format(dst))
            self._events[src].flow_ids.append(fid)
            self._events[dst].term_flow_ids.append(fid)

    def add_instant(
        self,
        track: TrackHandle,
        name: str,
        ts: int | float,
        args: Args = None,
    ) -> None:
        """
        Add a point-in-time marker on a track.

        Args:
            track: Track handle.
            name:  Label (e.g. "CRC Error", "IRQ Fired").
            ts:    Timestamp.
            args:  Optional dict of custom attributes.

        Raises:
            TypeError: If track is a counter track.
        """
        info = self._get_track(track)
        self._assert_not_counter(info)

        self._instants.append(_InstantInfo(
            track_uuid=track,
            name=name,
            ts_ns=self._to_ns(ts),
            seq_id=self._alloc_seq_id(),
            args=args,
        ))

    def add_counter(
        self,
        track: TrackHandle,
        ts: int | float,
        value: int | float,
    ) -> None:
        """
        Add a data point to a counter track.

        Args:
            track: Counter track handle (created with counter=True).
            ts:    Timestamp.
            value: Numeric value (int or float).

        Raises:
            TypeError: If track is not a counter track.
        """
        info = self._get_track(track)
        self._assert_is_counter(info)

        self._counter_values[track].append(
            (self._to_ns(ts), value)
        )

    # ---- serialization ----

    def save(self, path: str) -> dict[str, int | str]:
        """
        Serialize the trace and write to a .perfetto-trace file.

        Args:
            path: Output file path (e.g. "output.perfetto-trace").

        Returns:
            Dict with statistics about the generated trace.
        """
        raw_packets: list[bytes] = []

        # --- Zero-anchor: force Perfetto timeline to start at t=0 ---
        # Without this, Perfetto auto-offsets the timeline to the
        # earliest event, making timestamps mismatch with text logs.
        anchor_uuid = self._alloc_uuid()
        anchor_seq = self._alloc_seq_id()
        td = _build_track_descriptor(anchor_uuid, "t=0")
        raw_packets.append(_build_trace_packet(track_descriptor=td))
        te = _build_track_event(
            track_uuid=anchor_uuid,
            event_type=3,  # INSTANT
            name="t=0",
        )
        raw_packets.append(_build_trace_packet(
            timestamp_ns=0,
            track_event=te,
            sequence_id=anchor_seq,
            sequence_flags=1,
        ))

        # --- Track descriptors ---
        for uuid, t in self._tracks.items():
            td = _build_track_descriptor(
                t.uuid, t.name, t.parent_uuid, t.is_counter)
            raw_packets.append(_build_trace_packet(track_descriptor=td))

        _SEQ_CLEARED = 1  # SEQ_INCREMENTAL_STATE_CLEARED

        # --- Events (SLICE_BEGIN / SLICE_END) ---
        for eid, e in self._events.items():
            # SLICE_BEGIN (carries name, flow, and args)
            te = _build_track_event(
                track_uuid=e.track_uuid,
                event_type=1,
                name=e.name,
                flow_ids=e.flow_ids or None,
                terminating_flow_ids=e.term_flow_ids or None,
                args=e.args,
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=e.start_ns,
                track_event=te,
                sequence_id=e.seq_id,
                sequence_flags=_SEQ_CLEARED,
            ))
            # SLICE_END (no name/args needed)
            te = _build_track_event(
                track_uuid=e.track_uuid,
                event_type=2,
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=e.end_ns,
                track_event=te,
                sequence_id=e.seq_id,
            ))

        # --- Instant events ---
        for inst in self._instants:
            te = _build_track_event(
                track_uuid=inst.track_uuid,
                event_type=3,
                name=inst.name,
                args=inst.args,
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=inst.ts_ns,
                track_event=te,
                sequence_id=inst.seq_id,
                sequence_flags=_SEQ_CLEARED,
            ))

        # --- Counter values ---
        for uuid, values in self._counter_values.items():
            seq = self._counter_seq_ids[uuid]
            first = True
            for ts_ns, val in sorted(values):
                te = _build_track_event(
                    track_uuid=uuid,
                    event_type=4,
                    counter_value=val,
                )
                raw_packets.append(_build_trace_packet(
                    timestamp_ns=ts_ns,
                    track_event=te,
                    sequence_id=seq,
                    sequence_flags=_SEQ_CLEARED if first else 0,
                ))
                first = False

        # --- Write binary ---
        trace_data = bytearray()
        for pkt in raw_packets:
            trace_data += _field_bytes(1, pkt)

        with open(path, 'wb') as f:
            f.write(trace_data)

        return {
            "path": path,
            "size_bytes": len(trace_data),
            "tracks": sum(1 for t in self._tracks.values() if not t.is_counter),
            "counter_tracks": sum(1 for t in self._tracks.values() if t.is_counter),
            "events": len(self._events),
            "instants": len(self._instants),
            "counter_points": sum(len(v) for v in self._counter_values.values()),
            "flow_arrows": sum(len(e.flow_ids) for e in self._events.values()),
        }
