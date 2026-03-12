"""
perfetto_trace.py - Zero-dependency Perfetto trace generator.

Supports:
  - Hierarchical tracks (unlimited nesting, custom names)
  - Overlapping slices on the same track
  - Flow arrows connecting slices across tracks
  - Instant events (point-in-time markers)
  - Counter tracks (numeric line charts)

Usage:
    from perfetto_trace import PerfettoTrace

    trace = PerfettoTrace(time_unit="us")

    # 1. Build track hierarchy (unlimited depth)
    soc   = trace.add_track("SoC Pipeline")
    noc   = trace.add_track("NoC Subsystem", parent=soc)
    rtr0  = trace.add_track("Router 0",      parent=noc)
    rtr1  = trace.add_track("Router 1",      parent=noc)
    mem   = trace.add_track("MemCtrl",        parent=soc)
    cache = trace.add_track("L2 Cache",       parent=mem)

    # 2. Add slices (overlapping on same track is OK)
    s1 = trace.add_slice(rtr0, "Pkt#1", start=0,  end=80)
    s2 = trace.add_slice(rtr0, "Pkt#2", start=30, end=120)  # overlaps s1
    s3 = trace.add_slice(cache, "Pkt#1", start=90, end=200)

    # 3. Connect slices with flow arrows
    trace.add_flow([s1, s3])  # arrow from s1 -> s3

    # 4. Convenience: add a packet with auto slice + flow
    trace.add_packet("Pkt#3", [
        (rtr0,  300, 400),
        (rtr1,  410, 500),
        (cache, 510, 650),
    ])

    # 5. Instant event
    trace.add_instant(rtr0, "CRC Error", ts=50)

    # 6. Counter track
    qdepth = trace.add_counter_track("Queue Depth", parent=rtr0)
    trace.add_counter(qdepth, ts=0,   value=0)
    trace.add_counter(qdepth, ts=30,  value=1)
    trace.add_counter(qdepth, ts=80,  value=2)
    trace.add_counter(qdepth, ts=120, value=0)

    # 7. Save
    trace.save("output.perfetto-trace")

Requires: Python 3.x (only uses struct from stdlib)
"""

import struct


# ====================================================================
# Protobuf wire-format encoder (zero external dependencies)
# ====================================================================

def _encode_varint(value):
    """Encode an unsigned integer as a protobuf varint."""
    if value < 0:
        value = value & 0xFFFFFFFFFFFFFFFF
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _field_varint(field_number, value):
    """Wire type 0: varint."""
    return _encode_varint((field_number << 3) | 0) + _encode_varint(value)


def _field_fixed64(field_number, value):
    """Wire type 1: 64-bit fixed."""
    return _encode_varint((field_number << 3) | 1) + struct.pack('<Q', value)


def _field_double(field_number, value):
    """Wire type 1: double (64-bit float)."""
    return _encode_varint((field_number << 3) | 1) + struct.pack('<d', value)


def _field_bytes(field_number, data):
    """Wire type 2: length-delimited bytes."""
    return (_encode_varint((field_number << 3) | 2)
            + _encode_varint(len(data))
            + data)


def _field_string(field_number, s):
    """Wire type 2: length-delimited string."""
    return _field_bytes(field_number, s.encode('utf-8'))


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
#                      TrackDescriptor track_descriptor    = 60; }
#
# TrackDescriptor    { uint64  uuid        = 1;
#                      string  name        = 2;
#                      uint64  parent_uuid = 5;
#                      CounterDescriptor counter = 8; }
#
# CounterDescriptor  { (empty is enough to mark as counter track) }
#
# TrackEvent         { uint64  track_uuid             = 11;
#                      Type    type                    = 9;
#                      string  name                    = 23;
#                      int64   counter_value           = 30;
#                      double  double_counter_value    = 44;
#                      repeated fixed64 flow_ids       = 47;
#                      repeated fixed64 terminating_flow_ids = 48; }
#
# TrackEvent.Type    { SLICE_BEGIN=1; SLICE_END=2; INSTANT=3; COUNTER=4; }


def _build_track_descriptor(uuid, name, parent_uuid=None, is_counter=False):
    msg = _field_varint(1, uuid)
    msg += _field_string(2, name)
    if parent_uuid is not None:
        msg += _field_varint(5, parent_uuid)
    if is_counter:
        # CounterDescriptor as empty sub-message on field 8
        msg += _field_bytes(8, b'')
    return msg


def _build_track_event(track_uuid, event_type, name=None,
                       counter_value=None,
                       flow_ids=None, terminating_flow_ids=None):
    msg = _field_varint(11, track_uuid)
    msg += _field_varint(9, event_type)
    if name is not None:
        msg += _field_string(23, name)
    if counter_value is not None:
        if isinstance(counter_value, float) and not counter_value.is_integer():
            msg += _field_double(44, counter_value)      # double_counter_value
        else:
            msg += _field_varint(30, int(counter_value))  # int64 counter_value
    if flow_ids:
        for fid in flow_ids:
            msg += _field_fixed64(47, fid)
    if terminating_flow_ids:
        for fid in terminating_flow_ids:
            msg += _field_fixed64(48, fid)
    return msg


def _build_trace_packet(timestamp_ns=None, track_descriptor=None,
                        track_event=None, sequence_id=0):
    msg = b''
    if timestamp_ns is not None:
        msg += _field_varint(8, timestamp_ns)
    if sequence_id:
        msg += _field_varint(10, sequence_id)
    if track_descriptor is not None:
        msg += _field_bytes(60, track_descriptor)
    if track_event is not None:
        msg += _field_bytes(11, track_event)
    return msg


# ====================================================================
# PerfettoTrace - the main helper class
# ====================================================================

class PerfettoTrace:
    """
    Build Perfetto-native trace files with zero external dependencies.

    All timestamps use the unit specified in the constructor.
    Supported units: "ns", "us" (default), "ms", "s".
    """

    _TIME_SCALES = {
        "ns": 1,
        "us": 1_000,
        "ms": 1_000_000,
        "s":  1_000_000_000,
    }

    def __init__(self, time_unit="us"):
        """
        Args:
            time_unit: "ns" | "us" | "ms" | "s"
                       All start/end/ts values you pass will be in this unit.
                       Internally converted to nanoseconds for Perfetto.
        """
        if time_unit not in self._TIME_SCALES:
            raise ValueError(
                "time_unit must be one of: {}".format(
                    ", ".join(self._TIME_SCALES.keys())))
        self._ts_scale = self._TIME_SCALES[time_unit]

        self._next_uuid = 1
        self._next_flow_id = 1
        self._next_slice_id = 0
        self._next_seq_id = 1        # per-slice sequence IDs

        # Storage
        self._tracks = []             # [{uuid, name, parent_uuid}]
        self._slices = {}             # slice_id -> {track_uuid, name, start, end,
                                      #              flow_ids, term_flow_ids, seq_id}
        self._instants = []           # [{track_uuid, name, ts}]
        self._counter_tracks = {}     # uuid -> {name, parent_uuid}
        self._counter_values = {}     # uuid -> [(ts_ns, value)]

    # ---- internal helpers ----

    def _alloc_uuid(self):
        uid = self._next_uuid
        self._next_uuid += 1
        return uid

    def _alloc_flow_id(self):
        fid = self._next_flow_id
        self._next_flow_id += 1
        return fid

    def _alloc_seq_id(self):
        sid = self._next_seq_id
        self._next_seq_id += 1
        return sid

    def _to_ns(self, ts):
        return int(ts * self._ts_scale)

    # ---- public API ----

    def add_track(self, name, parent=None):
        """
        Add a track (or grouping container).

        Args:
            name:   Display name in Perfetto UI.
            parent: Parent track handle (returned by a previous add_track),
                    or None for a top-level track.

        Returns:
            Track handle (int) to use as parent for child tracks,
            or as the track argument in add_slice / add_instant.
        """
        uuid = self._alloc_uuid()
        self._tracks.append({
            "uuid": uuid,
            "name": name,
            "parent_uuid": parent,
        })
        return uuid

    def add_slice(self, track, name, start, end):
        """
        Add a duration slice on a track.
        Multiple slices on the same track may overlap in time.

        Args:
            track: Track handle from add_track().
            name:  Slice label (e.g. "Pkt#42").
            start: Start timestamp (in the time_unit from constructor).
            end:   End timestamp.

        Returns:
            Slice handle (int). Use in add_flow() to connect slices.
        """
        sid = self._next_slice_id
        self._next_slice_id += 1
        seq = self._alloc_seq_id()
        self._slices[sid] = {
            "track_uuid": track,
            "name": name,
            "start": self._to_ns(start),
            "end": self._to_ns(end),
            "flow_ids": [],
            "term_flow_ids": [],
            "seq_id": seq,
        }
        return sid

    def add_flow(self, slice_handles):
        """
        Connect a sequence of slices with flow arrows.

        Args:
            slice_handles: Ordered list of slice handles.
                           Arrows: slice_handles[0] -> [1] -> [2] -> ...
        """
        if len(slice_handles) < 2:
            return
        for i in range(len(slice_handles) - 1):
            fid = self._alloc_flow_id()
            src = slice_handles[i]
            dst = slice_handles[i + 1]
            if src not in self._slices or dst not in self._slices:
                raise ValueError("Invalid slice handle in add_flow()")
            self._slices[src]["flow_ids"].append(fid)
            self._slices[dst]["term_flow_ids"].append(fid)

    def add_packet(self, name, hops):
        """
        Convenience method: add one packet that traverses multiple modules.
        Creates slices + flow arrows automatically.

        Args:
            name: Packet label (e.g. "Pkt#1").
            hops: List of (track_handle, start, end) tuples.

        Returns:
            List of slice handles (one per hop).
        """
        handles = []
        for track, start, end in hops:
            h = self.add_slice(track, name, start, end)
            handles.append(h)
        if len(handles) > 1:
            self.add_flow(handles)
        return handles

    def add_instant(self, track, name, ts):
        """
        Add a point-in-time marker on a track.

        Args:
            track: Track handle.
            name:  Label (e.g. "CRC Error", "IRQ Fired").
            ts:    Timestamp.
        """
        self._instants.append({
            "track_uuid": track,
            "name": name,
            "ts": self._to_ns(ts),
            "seq_id": self._alloc_seq_id(),
        })

    def add_counter_track(self, name, parent=None):
        """
        Add a counter track (rendered as a line chart in Perfetto UI).

        Args:
            name:   Counter name (e.g. "Queue Depth").
            parent: Parent track handle, or None.

        Returns:
            Counter track handle. Use in add_counter().
        """
        uuid = self._alloc_uuid()
        self._counter_tracks[uuid] = {
            "name": name,
            "parent_uuid": parent,
            "seq_id": self._alloc_seq_id(),
        }
        self._counter_values[uuid] = []
        return uuid

    def add_counter(self, counter_track, ts, value):
        """
        Add a data point to a counter track.

        Args:
            counter_track: Handle from add_counter_track().
            ts:    Timestamp.
            value: Numeric value (int or float).
        """
        if counter_track not in self._counter_tracks:
            raise ValueError("Invalid counter_track handle")
        self._counter_values[counter_track].append(
            (self._to_ns(ts), value)
        )

    # ---- serialization ----

    def save(self, path):
        """
        Serialize the trace and write to a .perfetto-trace file.

        Args:
            path: Output file path (e.g. "output.perfetto-trace").
        """
        raw_packets = []

        # --- Track descriptors ---
        for t in self._tracks:
            td = _build_track_descriptor(
                t["uuid"], t["name"], t["parent_uuid"])
            raw_packets.append(
                _build_trace_packet(track_descriptor=td))

        # --- Counter track descriptors ---
        for uuid, info in self._counter_tracks.items():
            td = _build_track_descriptor(
                uuid, info["name"], info["parent_uuid"], is_counter=True)
            raw_packets.append(
                _build_trace_packet(track_descriptor=td))

        # --- Slices ---
        # Each slice uses its own sequence_id so that BEGIN/END pairing
        # is unambiguous, even when slices overlap on the same track.
        for sid, s in self._slices.items():
            # SLICE_BEGIN
            te = _build_track_event(
                track_uuid=s["track_uuid"],
                event_type=1,
                name=s["name"],
                flow_ids=s["flow_ids"] or None,
                terminating_flow_ids=s["term_flow_ids"] or None,
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=s["start"],
                track_event=te,
                sequence_id=s["seq_id"],
            ))
            # SLICE_END
            te = _build_track_event(
                track_uuid=s["track_uuid"],
                event_type=2,
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=s["end"],
                track_event=te,
                sequence_id=s["seq_id"],
            ))

        # --- Instant events ---
        for inst in self._instants:
            te = _build_track_event(
                track_uuid=inst["track_uuid"],
                event_type=3,
                name=inst["name"],
            )
            raw_packets.append(_build_trace_packet(
                timestamp_ns=inst["ts"],
                track_event=te,
                sequence_id=inst["seq_id"],
            ))

        # --- Counter values ---
        for uuid, values in self._counter_values.items():
            seq = self._counter_tracks[uuid]["seq_id"]
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
                ))

        # --- Write binary ---
        trace_data = b''
        for pkt in raw_packets:
            trace_data += _field_bytes(1, pkt)

        with open(path, 'wb') as f:
            f.write(trace_data)

        return {
            "path": path,
            "size_bytes": len(trace_data),
            "tracks": len(self._tracks),
            "counter_tracks": len(self._counter_tracks),
            "slices": len(self._slices),
            "instants": len(self._instants),
            "counter_points": sum(len(v) for v in self._counter_values.values()),
            "flow_arrows": sum(
                len(s["flow_ids"]) for s in self._slices.values()),
        }


# ====================================================================
# Demo
# ====================================================================

if __name__ == "__main__":

    trace = PerfettoTrace(time_unit="us")

    # ---- Track hierarchy (4 levels deep) ----
    soc = trace.add_track("SoC Pipeline")

    noc     = trace.add_track("NoC Subsystem",    parent=soc)
    router0 = trace.add_track("Router 0",         parent=noc)
    router1 = trace.add_track("Router 1",         parent=noc)
    bridge  = trace.add_track("NoC Bridge",       parent=noc)

    mem_sub = trace.add_track("Memory Subsystem",  parent=soc)
    cache   = trace.add_track("L2 Cache",          parent=mem_sub)
    memctrl = trace.add_track("DDR Controller",    parent=mem_sub)

    compute = trace.add_track("Compute Cluster",   parent=soc)
    cu0     = trace.add_track("Compute Unit 0",    parent=compute)
    cu1     = trace.add_track("Compute Unit 1",    parent=compute)

    dma     = trace.add_track("DMA Engine",        parent=soc)

    # ---- Packets (overlapping slices + flow arrows) ----
    trace.add_packet("Pkt#1", [
        (dma,     0,   80),
        (router0, 90,  200),
        (memctrl, 210, 400),
        (cache,   410, 500),
    ])
    trace.add_packet("Pkt#2", [
        (dma,     30,  120),   # overlaps Pkt#1 on DMA
        (router0, 130, 250),   # overlaps Pkt#1 on Router 0
        (cu0,     260, 450),
        (cache,   460, 600),
    ])
    trace.add_packet("Pkt#3", [
        (dma,     50,  150),
        (bridge,  160, 280),
        (router1, 290, 380),
        (cu1,     390, 550),
        (cache,   560, 700),
    ])
    trace.add_packet("Pkt#4", [
        (dma,     60,  170),
        (router0, 180, 310),
        (memctrl, 320, 500),
        (cache,   510, 650),
    ])
    trace.add_packet("Pkt#5", [
        (dma,     100, 200),
        (bridge,  210, 330),
        (router1, 340, 420),
        (cu0,     430, 580),
    ])
    trace.add_packet("Pkt#6", [
        (dma,     140, 250),
        (router0, 260, 370),
        (cu1,     380, 520),
        (memctrl, 530, 680),
    ])
    trace.add_packet("Pkt#7", [
        (dma,     180, 300),
        (router0, 310, 430),
        (memctrl, 440, 590),
        (cache,   600, 750),
    ])
    trace.add_packet("Pkt#8", [
        (dma,     220, 350),
        (bridge,  360, 470),
        (cu0,     480, 620),
        (cache,   630, 800),
    ])

    # ---- Instant events ----
    trace.add_instant(router0, "CRC Error",    ts=150)
    trace.add_instant(router0, "Buffer Full",  ts=350)
    trace.add_instant(dma,     "IRQ Trigger",  ts=500)
    trace.add_instant(memctrl, "ECC Corrected", ts=450)

    # ---- Counter tracks ----
    dma_q = trace.add_counter_track("DMA Queue Depth", parent=dma)
    for ts, val in [(0,0), (30,1), (50,2), (60,3), (80,2),
                    (100,3), (120,2), (140,3), (150,2),
                    (170,1), (180,2), (200,1), (220,2),
                    (250,1), (280,1), (300,0), (320,1),
                    (350,0), (400,0)]:
        trace.add_counter(dma_q, ts=ts, value=val)

    rtr0_bw = trace.add_counter_track("Router 0 Bandwidth", parent=router0)
    for ts, val in [(0,0), (90,120), (130,240), (180,360),
                    (200,180), (250,120), (260,240), (310,360),
                    (370,120), (430,0)]:
        trace.add_counter(rtr0_bw, ts=ts, value=val)

    cache_occ = trace.add_counter_track("L2 Occupancy (%)", parent=cache)
    for ts, val in [(0,0), (410,25), (460,50), (510,65),
                    (560,75), (600,85), (630,95), (700,70),
                    (750,50), (800,30)]:
        trace.add_counter(cache_occ, ts=ts, value=val)

    # ---- Save ----
    stats = trace.save("/mnt/user-data/outputs/demo_trace.perfetto-trace")

    print("=== Trace saved ===")
    for k, v in stats.items():
        print("  {}: {}".format(k, v))