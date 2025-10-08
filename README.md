# [Pylibfst](https://github.com/mschlaegl/pylibfst): Handle *Fast Signal Traces* (fst) in Python

Pylibfst is a python cffi wrapper for a slightly extended version of the fst C library contained in gtkwave.
It was initially created to add support for fst to [WAL](https://wal-lang.org) (*Waveform Analysis Language*), developed at the [Institute for Complex Systems](https://ics.jku.at/) at Johannes Kepler University, Linz.

For details of Installation, Usage and Examples of pylibfst, check [pylibfst/README.md](https://github.com/mschlaegl/pylibfst/blob/master/README.md)

## Waveform Processing APIs

### pylibfst core bindings

- `lib`: exposes the complete FST C reader/writer surface area via CFFI (see `pylibfst/libfstapi.cdef` for the exhaustive signature list).
	- **Reader lifecycle**: `fstReaderOpen`, `fstReaderClose`, `fstReaderReset`, `fstReaderReopen` manage trace handles.
	- **Metadata & enums**: `fstReaderGetVersionString`, `fstReaderGetDateString`, `fstReaderGetVarCount`, `fstReaderGetScopeCount`, `fstReaderGetAliasCount`, `fstReaderGetStartTime`, `fstReaderGetEndTime`, `fstReaderGetTimescale`, `fstReaderGetTimezero` describe a waveform.
	- **Hierarchy traversal**: `fstReaderIterateHierRewind` + `fstReaderIterateHier` let you walk scopes and variables; their payload is described by the `fstHier` struct re-exported through CFFI.
	- **Signal sampling**: `fstReaderSetFacProcessMaskAll`, `fstReaderClrFacProcessMask`, `fstReaderGetTimestamps`, and `fstReaderGetValueFromHandleAtTime` drive random access reads, while `fstReaderIterBlocks`/`fstReaderIterBlocks2` stream value changes.
	- **Writer support**: `fstWriterCreate`, `fstWriterCreateVar`, `fstWriterEmitValueChange*`, `fstWriterEmitTimeChange`, `fstWriterSetTimescale`, and `fstWriterClose` enable generating new `.fst` traces.
- `ffi`: raw CFFI object for allocating buffers, casting pointers, and registering callbacks.

### pylibfst helpers (Python ergonomics)

- `helpers.string(val)`: decodes C strings returned by the C API.
- `helpers.get_scopes_signals2(fst)`: walks the hierarchy and returns `(scopes, Signals)` with per-signal `name`, `length`, and `handle` lookup tables.
- `helpers.get_scopes_signals(fst)`: legacy helper that preserves the old `(scopes, {name: handle})` contract.
- `helpers.get_signal_name_by_handle(signals, handle)`: reverse lookup for deprecated call sites that rely on the legacy dictionary shape.
- `helpers.fstReaderIterBlocks(...)` / `helpers.fstReaderIterBlocks2(...)`: Python-callable wrappers around the streaming callbacks; accept ordinary Python functions and optional user data without manual `ffi` plumbing.

### Cache analysis utilities (`cache/`)

- `cache/deadlock_parser.py`:
	- Functions such as `dumpInfo`, `detect_cache_widths`, `first_halt_mshrid`, and `dump_signals` can be imported to introspect traces programmatically.
	- The CLI (`python cache/deadlock_parser.py <trace.fst>`) opens a trace via `pylibfst.lib`, auto-detects tag/set/offset widths, and reports MSHRs that stay asserted until the final timestamp.
- `cache/tllog_parser.py`:
	- Helper functions `opcode_str`, `param_str`, and `tllog_site` normalize TileLink fields.
	- When run as a script it filters a waveform for a target address, pairs Acquire/Grant and Release/ReleaseAck transactions, and emits a chronological log of the handshake beats.
- `cache/tllog_visual.py`:
	- Exposes `parse_log(log_text)` to render the parser output as a multi-column timeline and highlight state mismatches.
	- Provides a CLI (`python cache/tllog_visual.py tl.log`) that expects the sorted log emitted by `tllog_parser.py`.

All of the above modules use the same CFFI bindings, so they can be mixed and matched—for example, importing `detect_cache_widths` alongside `helpers.get_scopes_signals2` to build custom analyses.


## Cache Analysis Toolkit

The `cache/` directory hosts helper scripts that post-process TileLink cache-controller traces captured as FST waveforms. The scripts rely on the higher-level helpers in this repository and expect hierarchical naming that mirrors the `VerifyTop` design used in the included examples.

### Supported waveforms

To work out-of-the-box the waveform must satisfy:

- **File format**: GTKWave `.fst` produced with rising-edge sampling (the scripts iterate timestamps in steps of two to skip negedges).
- **Hierarchy anchors**: top-level scope `VerifyTop` with child instances `coupledL2`, `coupledL2_1`, `coupledL2AsL1`, and `coupledL2AsL1_1` for TileLink channels, plus `VerifyTop.l3` for the last-level cache.
- **MSHR signals**: per-slice signals following the pattern `...mshrs_<index>.req_valid`, along with matching `req_tag`, `req_set`, and `req_off` bitfields that either expose single-bit names or append ` [<msb>:0]` to the signal name.
- **TileLink bus signals**: channel bundles exported as `auto_out_<channel>_{valid,ready,bits_opcode,bits_param,bits_address,bits_source,bits_data}` for channels A/B/C/D.

Other hierarchies can be supported by adjusting the regexes in the scripts, but the above layout matches the shipped automation.

### `cache/deadlock_parser.py`

This script identifies cache deadlocks by reporting MSHRs that stay asserted at the final timestamp of the trace. For each stuck request it prints the first cycle where the request became valid, the final timestamp, reconstructed addresses (tag/set/offset), and any state-machine bits that remain low.

- **Automatic width detection**: the script infers tag/set/offset bit widths by scanning signal names; a fallback default is emitted with a warning if the expected naming pattern is missing.
- **Highlighting**: ANSI colors highlight the earliest unresolved request to help triage the root cause quickly.
- **Usage**:

	```bash
	python3 cache/deadlock_parser.py <path/to/trace.fst>
	```

![deadlock_visual](cache/doc/deadlock.png)

### `cache/tllog_parser.py`

`tllog_parser.py` reconstructs the TileLink transaction timeline for a single cache-line address. It walks all timestamped value changes, applies the TileLink handshake rules (`valid & ready`), and prints a concise log that records the site, channel, opcode mnemonic, param mnemonic, and accompanying data beats.

- **Address filter**: provide the desired address in hex; the parser keeps track of Acquire/Grant and Release/ReleaseAck pairings to suppress duplicate beats.
- **Channel coverage**: channels A–D are handled directly from the waveform; channel E is not required for the current use case.
- **Usage**:
	```bash
	python3 cache/tllog_parser.py <path/to/trace.fst> <target_addr_hex>
	```

### `cache/tllog_visual.py`

The visualizer consumes the sorted log produced by `tllog_parser.py` and renders a terminal timeline across the L1/L2/L3 hierarchy. Each column represents a link between cache levels, and per-node state (N/B/T) is tracked to detect illegal transitions.

- **State consistency checks**: Release/ProbeAck beats that report a state inconsistent with the latest Grant observation are highlighted as potential bugs.
- **Duplicate filtering**: repeated beats (e.g., the second cycle of `ReleaseData`) are skipped to keep the timeline compact.
- **Usage**:

	```bash
	python3 cache/tllog_parser.py <trace> <addr> | sort -k 1 -n > tl.log
	python3 cache/tllog_visual.py tl.log
	```

![tllog_visual](cache/doc/tllog_visual.png)

![tllog_visual_state_debug](cache/doc/tllog_visual_state_debug.png)