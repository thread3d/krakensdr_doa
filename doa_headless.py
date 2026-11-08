#!/usr/bin/env python3
"""doa_headless.py -- compute direction-of-arrival with no web interface.

krakensdr_doa normally runs behind a Dash web UI. The DSP core does not need it:
_sdr/ imports only numpy/scipy/numba/pyargus, and its one UI-side import
(`variables`) pulls in nothing but numpy and plotly. So the MUSIC pipeline can be
driven directly against HeIMDALL's `delay_sync_iq` shared memory, which is what
this does. Useful for headless work (an OGN receiver has no browser) and for
checking that the array is coherent without a GUI in the way.

Runs against a live DAQ chain:

    sudo ./scripts/sudo-daq-run.sh          # in the heimdall checkout, chain stays up
    ./doa_headless.py --freq 868.3 --radius 0.21

Requires an interpreter with numba + pyargus (the `kraken` conda env).

The array geometry matters: `--radius` is the radius of the circular array in
metres and a wrong value skews or splits the bearing. The default 0.21 m is the
same default the web interface uses (`ant_spacing_meters`) -- measure your array
before trusting an absolute bearing. Relative bearings, and the fact that a
coherent array produces a single sharp peak at all, are still meaningful without
that.

Exit codes: 0 = at least one DoA estimate produced, 1 = none, 2 = setup failed.
"""

import argparse
import logging
import os
import queue
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [
    os.path.join(ROOT, "_sdr", "_receiver"),
    os.path.join(ROOT, "_sdr", "_signal_processing"),
    os.path.join(ROOT, "_ui", "_web_interface"),
]


def main():
    ap = argparse.ArgumentParser(description="Headless KrakenSDR DoA (MUSIC) reader")
    ap.add_argument("--freq", type=float, default=868.3, help="centre frequency in MHz (default 868.3)")
    ap.add_argument("--radius", type=float, default=0.21,
                    help="circular array radius in metres (default 0.21, same as the web UI default)")
    ap.add_argument("--elements", type=int, default=5, help="number of antennas (default 5)")
    ap.add_argument("--algorithm", default="MUSIC",
                    choices=["MUSIC", "Bartlett", "Capon", "MEM"], help="DoA algorithm (default MUSIC)")
    ap.add_argument("--sources", type=int, default=1, help="expected number of sources (default 1)")
    ap.add_argument("--gain", type=float, default=40.0, help="DAQ rx gain in dB (default 40)")
    ap.add_argument("--count", type=int, default=10, help="number of DoA estimates to print (default 10)")
    ap.add_argument("--timeout", type=float, default=120.0, help="give up after this many seconds")
    ap.add_argument("--quiet", action="store_true", help="only print results, not library logging")
    args = ap.parse_args()

    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")

    # The processor writes DOA_value.html into <repo>/_share and expects it there.
    share = os.path.join(ROOT, "_share")
    os.makedirs(share, exist_ok=True)

    try:
        from kraken_sdr_receiver import ReceiverRTLSDR
        from kraken_sdr_signal_processor import SignalProcessor
    except Exception as e:  # noqa: BLE001 - report, do not traceback at the user
        print(f"cannot import the DSP core: {e}", file=sys.stderr)
        print("Is this interpreter able to import numba, pyargus and scipy?", file=sys.stderr)
        return 2

    # Queue(1) is what the web interface uses. The processor puts non-blocking and
    # discards on a full queue, so read promptly.
    sp_que: queue.Queue = queue.Queue(1)
    rx_que: queue.Queue = queue.Queue(1)

    print(f"opening delay_sync_iq (shmem) for {args.freq:.3f} MHz, {args.elements} elements, {args.algorithm}")
    receiver = ReceiverRTLSDR(data_que=rx_que, data_interface="shmem", logging_level=level)
    receiver.daq_center_freq = args.freq * 10**6
    receiver.daq_rx_gain = args.gain

    if not getattr(receiver, "in_shmem_iface", None) or not receiver.in_shmem_iface.init_ok:
        # Distinguish "the DAQ is not running" from "it is, but not as this user".
        # The second is the common one and the underlying error is an errno inside
        # a log line, so name it outright.
        fifo = os.path.join(receiver.daq_shmem_control_path, "bw_delay_sync_iq")
        if os.path.exists(fifo) and not os.access(fifo, os.W_OK):
            import pwd
            try:
                owner = pwd.getpwuid(os.stat(fifo).st_uid).pw_name
            except KeyError:
                owner = str(os.stat(fifo).st_uid)
            print(f"cannot attach to delay_sync_iq: {fifo} is not writable by "
                  f"{__import__('getpass').getuser()}", file=sys.stderr)
            print(f"the DAQ chain created it as '{owner}' -- run this as the same user,"
                  " or run the DAQ chain as this one.", file=sys.stderr)
        else:
            print("cannot attach to delay_sync_iq -- is the DAQ chain running?", file=sys.stderr)
            print("(the chain publishes delay_sync_iq only once delay_sync.py is up)", file=sys.stderr)
        return 2

    processor = SignalProcessor(data_que=sp_que, module_receiver=receiver, logging_level=level)

    # Array geometry. Same relationship the web interface uses:
    #   adjacent spacing of an N-gon = 2 * r * sin(pi / N)
    # expressed as a fraction of the wavelength.
    spacing_m = 2.0 * args.radius * np.sin(np.pi / args.elements)
    wavelength_m = 300.0 / args.freq
    processor.DOA_algorithm = args.algorithm
    processor.DOA_ant_alignment = "UCA"
    processor.DOA_UCA_radius_m = args.radius
    processor.DOA_inter_elem_space = spacing_m / wavelength_m
    processor.DOA_expected_num_of_sources = args.sources
    processor.doa_measure = "Linear"      # keep thetas as 0..359, no compass offset
    processor.en_DOA_estimation = True

    print(f"array: UCA radius {args.radius} m, element spacing {spacing_m:.3f} m "
          f"({processor.DOA_inter_elem_space:.3f} lambda)")
    print("starting the DSP thread; waiting for frames...\n")

    processor.start()

    deadline = time.time() + args.timeout
    got = 0
    seen_frames = 0
    try:
        while got < args.count and time.time() < deadline:
            try:
                packet = sp_que.get(timeout=2.0)
            except queue.Empty:
                hdr = getattr(receiver, "iq_header", None)
                if hdr is not None and getattr(hdr, "cpi_index", None):
                    seen_frames = hdr.cpi_index
                print(f"  ... waiting for DoA ({seen_frames} CPI so far)")
                continue

            data = dict(packet)
            if "DoA Max" not in data:
                continue

            theta = float(data["DoA Max"])
            conf = float(data.get("DoA Confidence", float("nan")))
            spectrum = data.get("DoA Result")
            thetas = data.get("doa_thetas")
            got += 1

            line = f"DoA {got:3d}: bearing {theta:6.1f} deg   confidence {conf:5.2f}"
            if spectrum is not None and thetas is not None:
                spec = np.asarray(spectrum, dtype=float)
                axis = np.asarray(thetas, dtype=float)
                if spec.size == axis.size and spec.size > 1:
                    pk = int(np.argmax(spec))
                    med = float(np.median(spec))
                    line += f"   peak {spec[pk]:.1f} vs median {med:.1f}"
            print(line)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        try:
            processor.en_DOA_estimation = False
            processor.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    print()
    if got == 0:
        print("no DoA estimates obtained.")
        print("Check, in order:")
        print("  1. the DAQ chain is running and delay_sync.py is up (delay_sync_iq exists)")
        print("  2. there is a signal in the passband -- DoA needs one above the squelch")
        print("  3. the calibration has completed; the first frames are CAL, not DATA")
        return 1

    print(f"{got} DoA estimate(s). Bearings are relative to the array's reference element;")
    print("correct them with --radius and the array's rotation before believing an absolute bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
