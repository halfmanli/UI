"""
demo_all_features.py - Comprehensive test of PerfettoTrace helper.

Covers every feature:
  1. Hierarchical tracks (4 levels deep)
  2. Overlapping events on the same track
  3. Flow arrows connecting events across tracks
  4. Instant events with and without args
  5. Counter tracks (int and float values)
  6. Custom args: str, int, float, bool, nested dict
  7. Non-zero start time (verifies t=0 anchor)
  8. Events without args (verifies args=None is fine)
  9. Multiple flow chains
  10. Validation checks (TypeError / ValueError)
"""

from perfetto_trace import PerfettoTrace


def main() -> None:
    trace = PerfettoTrace(time_unit="ns")

    # All timestamps start at 1ms = 1_000_000 ns to verify t=0 anchor
    T = 1_000_000  # base offset in ns

    # ==============================================================
    # 1. Hierarchical tracks - 4 levels deep
    # ==============================================================
    #
    # SoC Pipeline
    # ├── NoC Subsystem
    # │   ├── Router 0
    # │   │   ├── Input Port 0          (4th level)
    # │   │   └── Input Port 1          (4th level)
    # │   ├── Router 1
    # │   └── NoC Bridge
    # ├── Memory Subsystem
    # │   ├── L2 Cache
    # │   └── DDR Controller
    # │       ├── Channel 0             (4th level)
    # │       └── Channel 1             (4th level)
    # ├── Compute Cluster
    # │   ├── Compute Unit 0
    # │   └── Compute Unit 1
    # └── DMA Engine

    soc = trace.add_track("SoC Pipeline")

    # -- NoC --
    noc      = trace.add_track("NoC Subsystem",     parent=soc)
    router0  = trace.add_track("Router 0",          parent=noc)
    rtr0_in0 = trace.add_track("Input Port 0",      parent=router0)
    rtr0_in1 = trace.add_track("Input Port 1",      parent=router0)
    router1  = trace.add_track("Router 1",          parent=noc)
    bridge   = trace.add_track("NoC Bridge",        parent=noc)

    # -- Memory --
    mem_sub  = trace.add_track("Memory Subsystem",  parent=soc)
    cache    = trace.add_track("L2 Cache",          parent=mem_sub)
    ddr      = trace.add_track("DDR Controller",    parent=mem_sub)
    ddr_ch0  = trace.add_track("Channel 0",         parent=ddr)
    ddr_ch1  = trace.add_track("Channel 1",         parent=ddr)

    # -- Compute --
    compute  = trace.add_track("Compute Cluster",   parent=soc)
    cu0      = trace.add_track("Compute Unit 0",    parent=compute)
    cu1      = trace.add_track("Compute Unit 1",    parent=compute)

    # -- DMA --
    dma      = trace.add_track("DMA Engine",        parent=soc)

    # ==============================================================
    # Counter tracks (under various parents)
    # ==============================================================
    dma_queue    = trace.add_track("DMA Queue Depth",      parent=dma,     counter=True)
    rtr0_bw      = trace.add_track("Router 0 BW (MB/s)",   parent=router0, counter=True)
    cache_occ    = trace.add_track("L2 Occupancy (%)",     parent=cache,   counter=True)
    ddr_ch0_util = trace.add_track("Ch0 Utilization (%)",  parent=ddr_ch0, counter=True)

    # ==============================================================
    # 2 & 3. Overlapping events + flow arrows + 6. rich args
    # ==============================================================

    # --- Pkt#1: DMA -> Router0/Port0 -> DDR/Ch0 -> L2 Cache ---
    # Full args on every hop
    p1 = [
        trace.add_event(dma,      "Pkt#1", T + 0,    T + 800,
                        args={"pkt_id": 1, "size": 256, "priority": "high",
                              "src_addr": "0xFF00", "dst_addr": "0x0100"}),
        trace.add_event(rtr0_in0, "Pkt#1", T + 900,  T + 2000,
                        args={"pkt_id": 1, "input_port": 0, "output_port": 2,
                              "routing": {"algorithm": "XY", "hops": 2}}),
        trace.add_event(ddr_ch0,  "Pkt#1", T + 2100, T + 4000,
                        args={"pkt_id": 1, "channel": 0, "bank": 3,
                              "row": 1024, "col": 64,
                              "timing": {"tRCD": 13.5, "tRP": 14.0}}),
        trace.add_event(cache,    "Pkt#1", T + 4100, T + 5000,
                        args={"pkt_id": 1, "hit": False, "way": 7,
                              "set": 128, "state": "EXCLUSIVE"}),
    ]
    trace.add_flow(p1)

    # --- Pkt#2: overlaps Pkt#1 on DMA, goes through Port1 -> CU0 -> Cache ---
    p2 = [
        trace.add_event(dma,      "Pkt#2", T + 300,  T + 1200,   # overlaps Pkt#1 on DMA
                        args={"pkt_id": 2, "size": 128, "priority": "low"}),
        trace.add_event(rtr0_in1, "Pkt#2", T + 1300, T + 2500,   # different port than Pkt#1
                        args={"pkt_id": 2, "input_port": 1}),
        trace.add_event(cu0,      "Pkt#2", T + 2600, T + 4500,
                        args={"pkt_id": 2, "op": "MAC", "cycles": 1900}),
        trace.add_event(cache,    "Pkt#2", T + 4600, T + 6000,   # overlaps Pkt#1 on L2 Cache
                        args={"pkt_id": 2, "hit": True, "way": 3}),
    ]
    trace.add_flow(p2)

    # --- Pkt#3: through NoC Bridge path ---
    p3 = [
        trace.add_event(dma,      "Pkt#3", T + 500,  T + 1500,   # triple overlap on DMA
                        args={"pkt_id": 3, "size": 512}),
        trace.add_event(bridge,   "Pkt#3", T + 1600, T + 2800,
                        args={"pkt_id": 3, "protocol": "AXI4",
                              "burst": {"type": "INCR", "len": 8}}),
        trace.add_event(router1,  "Pkt#3", T + 2900, T + 3800,
                        args={"pkt_id": 3}),
        trace.add_event(cu1,      "Pkt#3", T + 3900, T + 5500,
                        args={"pkt_id": 3, "op": "CONV2D", "cycles": 1600}),
        trace.add_event(cache,    "Pkt#3", T + 5600, T + 7000,
                        args={"pkt_id": 3, "hit": False}),
    ]
    trace.add_flow(p3)

    # --- Pkt#4: DMA -> Router0 -> DDR/Ch1 -> Cache ---
    p4 = [
        trace.add_event(dma,      "Pkt#4", T + 600,  T + 1700,
                        args={"pkt_id": 4, "size": 64,
                              "flags": {"urgent": True, "retry": 0, "compress": False}}),
        trace.add_event(rtr0_in0, "Pkt#4", T + 1800, T + 3100,   # overlaps Pkt#1 on Port0
                        args={"pkt_id": 4}),
        trace.add_event(ddr_ch1,  "Pkt#4", T + 3200, T + 5000,
                        args={"pkt_id": 4, "channel": 1, "bank": 7}),
        trace.add_event(cache,    "Pkt#4", T + 5100, T + 6500,
                        args={"pkt_id": 4}),
    ]
    trace.add_flow(p4)

    # --- Pkt#5 ~ Pkt#10: no args, tests args=None path + more overlaps ---
    for pkt_id, hops in [
        (5,  [(dma, 1000, 2000), (bridge, 2100, 3300), (router1, 3400, 4200), (cu0, 4300, 5800)]),
        (6,  [(dma, 1400, 2500), (rtr0_in0, 2600, 3700), (cu1, 3800, 5200), (ddr_ch0, 5300, 6800)]),
        (7,  [(dma, 1800, 3000), (rtr0_in1, 3100, 4300), (ddr_ch1, 4400, 5900), (cache, 6000, 7500)]),
        (8,  [(dma, 2200, 3500), (bridge, 3600, 4700), (cu0, 4800, 6200), (cache, 6300, 8000)]),
        (9,  [(dma, 2800, 4000), (rtr0_in0, 4100, 5200), (cu1, 5300, 6700), (ddr_ch0, 6800, 8500)]),
        (10, [(dma, 3200, 4500), (rtr0_in1, 4600, 5600), (ddr_ch0, 5700, 7200), (cache, 7300, 9000)]),
    ]:
        handles = [trace.add_event(t, "Pkt#{}".format(pkt_id), T + s, T + e)
                   for t, s, e in hops]
        trace.add_flow(handles)

    # ==============================================================
    # 9. Multiple independent flow chains (non-packet use case)
    # ==============================================================
    # Simulate a request/response pair
    req = trace.add_event(cu0, "REQ_A", T + 8000, T + 8500,
                          args={"type": "read", "addr": "0x4000"})
    resp = trace.add_event(ddr_ch0, "RESP_A", T + 8600, T + 9200,
                           args={"type": "read_data", "latency_ns": 700})
    trace.add_flow([req, resp])

    # ==============================================================
    # 4. Instant events - with and without args
    # ==============================================================
    trace.add_instant(router0, "CRC Error", ts=T + 1500,
                      args={"port": 3,
                            "expected_crc": "0xDEADBEEF",
                            "actual_crc":   "0xCAFEBABE",
                            "severity": "critical"})
    trace.add_instant(router0, "Buffer Full", ts=T + 3500,
                      args={"queue_id": 2, "depth": 64, "dropped": True})
    trace.add_instant(dma, "IRQ Trigger", ts=T + 5000)      # no args
    trace.add_instant(ddr, "ECC Corrected", ts=T + 4500,
                      args={"bank": 5, "row": 1024, "bit_pos": 13})
    trace.add_instant(cu0, "Pipeline Stall", ts=T + 3000,
                      args={"reason": "RAW hazard", "cycles": 12})
    trace.add_instant(bridge, "Timeout", ts=T + 6000)        # no args
    trace.add_instant(cache, "Eviction", ts=T + 7000,
                      args={"way": 2, "set": 64, "dirty": True,
                            "victim": {"addr": "0xABCD", "state": "MODIFIED"}})

    # ==============================================================
    # 5. Counter tracks - int and float values
    # ==============================================================

    # DMA queue depth (integer counter)
    for ts, val in [(0,0), (300,1), (500,2), (600,3), (800,2),
                    (1000,3), (1200,2), (1400,3), (1500,2),
                    (1700,1), (1800,2), (2000,1), (2200,2),
                    (2500,1), (2800,2), (3000,1), (3200,2),
                    (3500,1), (4000,0), (4500,1), (5000,0)]:
        trace.add_counter(dma_queue, ts=T + ts, value=val)

    # Router 0 bandwidth (integer counter, MB/s)
    for ts, val in [(0,0), (900,120), (1300,240), (1800,360),
                    (2000,180), (2500,120), (2600,280), (3100,400),
                    (3700,200), (4300,80), (5000,0), (6000,150),
                    (7000,0)]:
        trace.add_counter(rtr0_bw, ts=T + ts, value=val)

    # L2 occupancy (float counter, %)
    for ts, val in [(0, 0.0), (4100, 25.5), (4600, 48.3), (5100, 65.7),
                    (5600, 73.2), (6000, 82.9), (6300, 91.4), (7000, 68.1),
                    (7500, 45.6), (8000, 30.2), (9000, 15.8)]:
        trace.add_counter(cache_occ, ts=T + ts, value=val)

    # DDR Ch0 utilization (integer counter, %)
    for ts, val in [(0,0), (2100,45), (3200,0), (5300,72),
                    (5700,88), (6800,55), (7200,30), (8500,0),
                    (8600,20), (9200,0)]:
        trace.add_counter(ddr_ch0_util, ts=T + ts, value=val)

    # ==============================================================
    # 10. Validation checks
    # ==============================================================
    print("=== Validation checks ===")

    # add_event on counter track -> TypeError
    try:
        trace.add_event(dma_queue, "bad", 0, 10)
        print("FAIL: expected TypeError")
    except TypeError as e:
        print("OK: add_event on counter -> {}".format(e))

    # add_instant on counter track -> TypeError
    try:
        trace.add_instant(cache_occ, "bad", 0)
        print("FAIL: expected TypeError")
    except TypeError as e:
        print("OK: add_instant on counter -> {}".format(e))

    # add_counter on normal track -> TypeError
    try:
        trace.add_counter(dma, ts=0, value=42)
        print("FAIL: expected TypeError")
    except TypeError as e:
        print("OK: add_counter on normal -> {}".format(e))

    # invalid parent handle -> ValueError
    try:
        trace.add_track("Bad", parent=99999)
        print("FAIL: expected ValueError")
    except ValueError as e:
        print("OK: invalid parent -> {}".format(e))

    # end < start -> ValueError
    try:
        trace.add_event(dma, "bad", start=100, end=50)
        print("FAIL: expected ValueError")
    except ValueError as e:
        print("OK: end < start -> {}".format(e))

    # invalid track handle -> ValueError
    try:
        trace.add_event(99999, "bad", 0, 10)
        print("FAIL: expected ValueError")
    except ValueError as e:
        print("OK: invalid track -> {}".format(e))

    # invalid event handle in flow -> ValueError
    try:
        trace.add_flow([0, 99999])
        print("FAIL: expected ValueError")
    except ValueError as e:
        print("OK: invalid flow handle -> {}".format(e))

    # ==============================================================
    # Save
    # ==============================================================
    stats = trace.save("demo_all_features.perfetto-trace")

    print("\n=== Trace saved ===")
    for k, v in stats.items():
        print("  {}: {}".format(k, v))

    print("\nFeatures to verify in Perfetto UI:")
    print("  1. Track hierarchy: 4 levels (expand SoC > NoC > Router 0 > Input Port 0)")
    print("  2. Overlapping events: DMA track has 10+ overlapping slices")
    print("  3. Flow arrows: click any Pkt# event, see flow arrows to next hop")
    print("  4. Instant events: vertical markers on Router 0, DMA, etc.")
    print("  5. Counter tracks: line charts under DMA, Router 0, L2 Cache, Ch0")
    print("  6. Args: click Pkt#1 on DDR Ch0, see timing.tRCD=13.5 nested dict")
    print("  7. Timeline starts at t=0, events appear at ~1ms mark")
    print("  8. Events without args: Pkt#5~#10 have no debug section")
    print("  9. REQ_A -> RESP_A flow: independent from packet flows")


if __name__ == "__main__":
    main()
