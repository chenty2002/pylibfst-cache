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
    - When run as a script it filters a waveform for a target address, pairs Acquire/Grant and Release/ReleaseAck transactions (including multi-beat transfers), and emits a chronological log of handshake beats.
    - It skips incomplete channel bundles gracefully (instead of failing) when an endpoint is missing some signals in the trace.
- `cache/tllog_visual.py`:
    - Exposes `parse_log(log_text)` to render the parser output as a multi-column timeline and highlight state mismatches.
    - Supports multi-beat data opcodes (`ReleaseData`, `ProbeAckData`, `GrantData`) and displays merged data beats as `[beat0,beat1]`.
	- Provides a CLI (`python cache/tllog_visual.py tl.log`) that expects the sorted log emitted by `tllog_parser.py`.
- `cache/coherence_checker.py`:
    - Checks coherence properties directly from FST (all addresses) or from a parser log (`--log`).
    - Reports violation contexts for L2 peer mutual exclusion, L1/L2 legality (inclusive constraints), and related state-transition inconsistencies.

All of the above modules use the same CFFI bindings, so they can be mixed and matched—for example, importing `detect_cache_widths` alongside `helpers.get_scopes_signals2` to build custom analyses.


## Cache Analysis Toolkit

The `cache/` directory hosts helper scripts that post-process TileLink cache-controller traces captured as FST waveforms. The scripts rely on the higher-level helpers in this repository and expect hierarchical naming that mirrors the `VerifyTop` design used in the included examples.

### Supported waveforms

To work out-of-the-box the waveform must satisfy:

- **File format**: GTKWave `.fst` produced with rising-edge sampling (the scripts iterate timestamps in steps of two to skip negedges).
- **Hierarchy anchors**: top-level scope `VerifyTop` with child instances `coupledL2`, `coupledL2_1`, `coupledL2AsL1`, and `coupledL2AsL1_1` for TileLink channels, plus `VerifyTop.l3` for the last-level cache.
- **MSHR signals**: per-slice signals following the pattern `...mshrs_<index>.req_valid`, along with matching `req_tag`, `req_set`, and `req_off` bitfields that either expose single-bit names or append ` [<msb>:0]` to the signal name.
- **TileLink bus signals**: channel bundles exported as `auto_out_<channel>_{valid,ready,bits_opcode,bits_param,bits_address,bits_source,bits_size,bits_data}` for channels A/B/C/D.

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

- **Address filter**: provide the desired address in hex; the parser tracks source IDs and expected beats to pair Acquire/GrantData and Release/ReleaseAck correctly.
- **Channel coverage**: channels A-D are handled directly from the waveform; channel E is not required for the current use case.
- **Robustness**: missing channel fields on partial traces are skipped safely instead of raising `NoneType` attribute errors.
- **Usage**:
	```bash
	python3 cache/tllog_parser.py <path/to/trace.fst> <target_addr_hex>
	```

### `cache/tllog_visual.py`

The visualizer consumes the sorted log produced by `tllog_parser.py` and renders a terminal timeline across the L1/L2/L3 hierarchy. Each column represents a link between cache levels, and per-node state (N/B/T) is tracked to detect illegal transitions.

- **State consistency checks**: Release/ProbeAck beats that report a state inconsistent with the latest Grant observation are highlighted as potential bugs.
- **Multi-beat display**: repeated beats for `ReleaseData`/`ProbeAckData`/`GrantData` are merged and shown as `[beat0,beat1]` to keep the timeline compact.
- **Layout**: prints time on both left and right sides to improve readability on wide traces.
- **Usage**:

	```bash
	python3 cache/tllog_parser.py <trace> <addr> | sort -k 1 -n > tl.log
	python3 cache/tllog_visual.py tl.log
	```

### `cache/coherence_checker.py`

`coherence_checker.py` verifies cache coherence properties from either a waveform or parser log and prints focused context around violations.

- **Input modes**:
    - FST mode: parse all addresses from waveform transactions and check each address independently.
    - Log mode (`--log`): consume text output from `tllog_parser.py`.
- **Checks performed**:
    - L2 peer mutual exclusion violations.
    - L1/L2 legality violations under inclusive constraints.
    - Inclusive violations where L1 is valid but parent L2 is invalid.
- **Output style**:
    - Reuses timeline-like rows and prints violation-centered windows (`--context`) for quick triage.

```bash
# FST mode (all addresses)
python3 cache/coherence_checker.py <path/to/trace.fst>

# FST mode (single address)
python3 cache/coherence_checker.py <path/to/trace.fst> --addr 0x14

# Log mode
python3 cache/tllog_parser.py <trace> 0x14 | sort -k 1 -n > tl.log
python3 cache/coherence_checker.py --log tl.log --addr 0x14
```

![tllog_visual](cache/doc/tllog_visual.png)

![tllog_visual_state_debug](cache/doc/tllog_visual_state_debug.png)

## Pylibfst API Documentation

### Core Module APIs

The core `pylibfst` module provides Python wrappers around the FST C library for reading and analyzing waveform traces.

#### Opening and Closing FST Files

```python
import pylibfst

# Open an FST file
fst = pylibfst.lib.fstReaderOpen(filename.encode("UTF-8"))
if fst == pylibfst.ffi.NULL:
    print("Unable to open file!")
    sys.exit(1)

# Close the FST file when done
pylibfst.lib.fstReaderClose(fst)
```

**Requirements:**
- Filename must be encoded as UTF-8 bytes
- FST file must be a valid Fast Signal Trace format generated by GTKWave-compatible tools
- File must exist and be readable

#### File Metadata APIs

```python
# Get file version string
verStr = pylibfst.lib.fstReaderGetVersionString(fst)
print(pylibfst.string(verStr))

# Get date string
date = pylibfst.lib.fstReaderGetDateString(fst)
print(pylibfst.string(date))

# Get file type (Verilog, VHDL, etc.)
fileType = pylibfst.lib.fstReaderGetFileType(fst)

# Get counts
varCount = pylibfst.lib.fstReaderGetVarCount(fst)
scopeCount = pylibfst.lib.fstReaderGetScopeCount(fst)
aliasCount = pylibfst.lib.fstReaderGetAliasCount(fst)

# Get time information
startTime = pylibfst.lib.fstReaderGetStartTime(fst)
endTime = pylibfst.lib.fstReaderGetEndTime(fst)
timeScale = pylibfst.lib.fstReaderGetTimescale(fst)
timeZero = pylibfst.lib.fstReaderGetTimezero(fst)

# Get value change section count
valChSecCnt = pylibfst.lib.fstReaderGetValueChangeSectionCount(fst)
```

### Helper Function APIs

#### `string(val)`

Converts FFI cdata pointer to Python UTF-8 string.

```python
name_str = pylibfst.string(fstHier.u.var.name)
```

**Parameters:**
- `val`: CFFI pointer (cdata) from libfstapi

**Returns:**
- Python string (empty string if val is NULL)

---

#### `get_scopes_signals2(fst)`

**(Recommended)** Iterates the hierarchy and returns detailed signal information with bidirectional lookup.

```python
(scopes, signals) = pylibfst.get_scopes_signals2(fst)

# Access signals by name
for signal in signals.by_name.values():
    print(f"Signal: {signal.name}, Length: {signal.length}, Handle: {signal.handle}")

# Access signals by handle
signal_info = signals.by_handle[handle_id]
print(f"Name: {signal_info.name}")
```

**Parameters:**
- `fst`: Open FST file handle

**Returns:**
- Tuple of `(scopes, signals)` where:
  - `scopes`: List of scope names (strings)
  - `signals`: Named tuple `Signals(by_name, by_handle)` containing:
    - `by_name`: Dict mapping signal names → `Signal(name, length, handle)` namedtuple
    - `by_handle`: Dict mapping handles → `Signal(name, length, handle)` namedtuple

**Signal Structure:**
```python
Signal = namedtuple("Signal", "name length handle")
# name: Full hierarchical signal name (str)
# length: Bit width of signal (int)
# handle: Unique handle for the signal (int)
```

**Note:** Multiple signal names may map to the same handle (aliases), so `by_handle` may have fewer entries than `by_name`.

---

#### `get_scopes_signals(fst)`

**(Deprecated)** Legacy version that returns only name→handle mapping.

```python
(scopes, signal_dict) = pylibfst.get_scopes_signals(fst)
# signal_dict: {signal_name: handle}
```

**Recommendation:** Use `get_scopes_signals2()` instead for complete signal metadata.

---

#### `fstReaderIterBlocks(fst, value_change_callback, user_callback_data=None, vcdhandle=None)`

Iterate through value change blocks and invoke a callback for each signal transition.

```python
def my_callback(user_data, time, facidx, value):
    signal = signals.by_handle[facidx]
    value_str = pylibfst.string(value)
    print(f"Time {time}: {signal.name} = {value_str}")

# Enable all signals for processing
pylibfst.lib.fstReaderSetFacProcessMaskAll(fst)

# Iterate blocks
ret = pylibfst.fstReaderIterBlocks(fst, my_callback, user_callback_data=None)
```

**Parameters:**
- `fst`: Open FST file handle
- `value_change_callback`: Python function with signature `callback(user_data, time, facidx, value)`
  - `user_data`: User-provided callback data
  - `time`: Simulation time (int)
  - `facidx`: Signal handle/index (int) - lookup via `signals.by_handle[facidx]`
  - `value`: Signal value as CFFI cdata (use `pylibfst.string(value)` to convert)
- `user_callback_data` (optional): Arbitrary Python object passed to callback
- `vcdhandle` (optional): VCD handle filter (use `None` for all signals)

**Returns:**
- Integer status code

**Requirements:**
- Must call `pylibfst.lib.fstReaderSetFacProcessMaskAll(fst)` or set specific signal masks before iterating
- Callback is invoked for every value change event
- Use `get_scopes_signals2()` first to create signal handle→name mapping

---

#### `fstReaderIterBlocks2(fst, value_change_callback, value_change_callback_varlen, user_callback_data=None, vcdhandle=None)`

Extended version supporting variable-length vectors with a secondary callback.

```python
def fixed_callback(user_data, time, facidx, value):
    print(f"Fixed: Time {time}, Handle {facidx}, Value {pylibfst.string(value)}")

def varlen_callback(user_data, time, facidx, value, length):
    print(f"Varlen: Time {time}, Handle {facidx}, Value {pylibfst.string(value)}, Length {length}")

ret = pylibfst.fstReaderIterBlocks2(fst, fixed_callback, varlen_callback, user_callback_data=None)
```

**Parameters:**
- All parameters from `fstReaderIterBlocks()` plus:
- `value_change_callback_varlen`: Python function with signature `callback(user_data, time, facidx, value, length)`
  - `length`: Bit length of variable-length signal (int)

**Use Case:** Handle both fixed-width and variable-length signals separately.

---

### Timestamp and Value Query APIs

#### Get All Timestamps

```python
# Enable all signals
pylibfst.lib.fstReaderSetFacProcessMaskAll(fst)

# Get timestamp array
timestamps = pylibfst.lib.fstReaderGetTimestamps(fst)

# Iterate through timestamps
for ts in range(timestamps.nvals):
    time = timestamps.val[ts]
    print(f"Timestamp: {time}")

# Free timestamps when done
pylibfst.lib.fstReaderFreeTimestamps(timestamps)
```

**Requirements:**
- Must call `fstReaderSetFacProcessMaskAll()` or set signal masks first
- Always free timestamps after use to prevent memory leaks

---

#### Query Signal Value at Specific Time

```python
buf = pylibfst.ffi.new("char[256]")

# Get value for a specific signal at a specific time
value = pylibfst.string(
    pylibfst.lib.fstReaderGetValueFromHandleAtTime(
        fst, time, signal.handle, buf
    )
)
print(f"At time {time}, signal {signal.name} = {value}")
```

**Parameters:**
- `fst`: Open FST file handle
- `time`: Simulation time to query
- `handle`: Signal handle (from `signal.handle`)
- `buf`: Pre-allocated character buffer (minimum 256 bytes recommended)

**Returns:**
- CFFI pointer to value string (use `pylibfst.string()` to convert)

**Requirements:**
- Buffer must be large enough to hold the value (256 bytes sufficient for most cases)
- Time must be within the simulation range

---

### Hierarchy Iteration APIs

#### Manual Hierarchy Traversal

```python
pylibfst.lib.fstReaderIterateHierRewind(fst)

while True:
    fstHier = pylibfst.lib.fstReaderIterateHier(fst)
    if fstHier == pylibfst.ffi.NULL:
        break
    
    if fstHier.htyp == pylibfst.lib.FST_HT_SCOPE:
        scope_name = pylibfst.string(fstHier.u.scope.name)
        scope_type = fstHier.u.scope.typ
        print(f"Scope: {scope_name} (type {scope_type})")
    
    elif fstHier.htyp == pylibfst.lib.FST_HT_VAR:
        var_name = pylibfst.string(fstHier.u.var.name)
        var_handle = fstHier.u.var.handle
        var_length = fstHier.u.var.length
        var_type = fstHier.u.var.typ
        var_direction = fstHier.u.var.direction
        print(f"Variable: {var_name}, Handle: {var_handle}, Length: {var_length}")
    
    elif fstHier.htyp == pylibfst.lib.FST_HT_UPSCOPE:
        print("Up scope")
```

**Hierarchy Types:**
- `FST_HT_SCOPE`: Entering a new scope (module, task, etc.)
- `FST_HT_UPSCOPE`: Exiting current scope
- `FST_HT_VAR`: Signal/variable declaration
- `FST_HT_ATTRBEGIN`: Attribute block begin
- `FST_HT_ATTREND`: Attribute block end
- `FST_HT_TREEBEGIN`: Tree structure begin
- `FST_HT_TREEEND`: Tree structure end

---

## Pylibfst-Cache: Application-Specific Utilities

This section describes the custom cache analysis tools built on top of pylibfst.

### Deadlock Parser

**Purpose:** Analyzes deadlock scenarios in cache simulations by parsing the final state of MSHRs (Miss Status Holding Registers).

**Usage:**
```bash
python3 cache/deadlock_parser.py <fst_path>
```

**Features:**
- Identifies all pending MSHRs at deadlock
- Extracts start time, end time, address, and unfinished state machines
- Provides color-coded visual output for analysis
- Auto-detects tag/set/offset widths from signal names with safe fallback defaults

**Signal Requirements:**
The FST waveform must contain:
- MSHR valid signals indicating active entries
- Address fields (`req_tag`, `req_set`, `req_off`) for reconstructing addresses
- State machine status signals for tracking unfinished operations

**Output:**
- Visual timeline showing MSHR states at deadlock
- Address information for each pending transaction
- Colored indicators for different state types

**Limitations:**
- Signal regexes are tuned for the shipped VerifyTop hierarchy; custom hierarchies may require regex updates
- Unknown (`x`/`z`) address bits are treated as zero during address reconstruction

**Example Output:**
```
Time: 12345
MSHR[0]: addr=0x1000, state=WAIT_GRANT, start=12000, end=12345
MSHR[2]: addr=0x2040, state=WAIT_PROBE_ACK, start=11800, end=12345
```

---

### Transaction Log Parser, Visualizer, and Coherence Checker

**Purpose:** Extracts and visualizes TileLink transactions for a specific memory address across all cache levels.

**Usage:**
```bash
# Step 1: Extract transactions and sort by time
python3 cache/tllog_parser.py <fst_path> <target_addr> | sort -k 1 -n > tl.log

# Step 2: Visualize the timeline
python3 cache/tllog_visual.py tl.log
```

**Parameters:**
- `<fst_path>`: Path to FST waveform file
- `<target_addr>`: Target address in hexadecimal (e.g., `0x1000`)

**Features:**
1. **Transaction Extraction:**
    - Monitors TileLink channels A-D
   - Filters transactions by target address
    - Records opcode, parameters, and data for each transaction
    - Matches multi-beat flows using source IDs and `size`-derived beat counts

2. **Multi-Level Cache Support:**
   - Tracks transactions across L1, L2, and L3 cache levels
   - Maps cache instances to logical names:
     - `L3_L2[0]`: CoupledL2 instance 0
     - `L3_L2[1]`: CoupledL2 instance 1
     - `L2_L1[0].C[0]`: L1 cache core 0
     - `L2_L1[1].C[0]`: L1 cache core 1

3. **State Verification:**
   - Tracks client state through Grant operations (`toB`, `toT`)
   - Verifies state consistency on Release/ProbeAck (`TtoN`, `TtoB`, `BtoN`)
   - Detects state inconsistencies that indicate protocol violations

4. **Transaction Pairing:**
   - Matches Acquire → Grant sequences using source ID
   - Matches Release → ReleaseAck sequences
    - Handles multi-beat GrantData/ReleaseData consistently

5. **Robust Trace Handling:**
    - Skips incomplete per-channel signal bundles for endpoints that are partially dumped
    - Avoids `NoneType` crashes when some interfaces are absent in the FST

**Signal Requirements:**
The FST file must contain TileLink channel signals for each cache level:
```
<cache_instance>.auto_out_<channel>_valid
<cache_instance>.auto_out_<channel>_ready
<cache_instance>.auto_out_<channel>_bits_opcode
<cache_instance>.auto_out_<channel>_bits_address
<cache_instance>.auto_out_<channel>_bits_param
<cache_instance>.auto_out_<channel>_bits_source
<cache_instance>.auto_out_<channel>_bits_size
<cache_instance>.auto_out_<channel>_bits_data
```

Where `<channel>` is one of: `a`, `b`, `c`, `d`

**TileLink Operations Decoded:**

| Channel | Opcode | Operation | Description |
|---------|--------|-----------|-------------|
| A | 0-7 | PutFullData, PutPartialData, ArithmeticData, LogicalData, Get, Hint, AcquireBlock, AcquirePerm | Client requests |
| B | 0-6 | PutFullData, PutPartialData, ArithmeticData, LogicalData, Get, Hint, Probe | Master requests |
| C | 0-7 | AccessAck, AccessAckData, HintAck, ProbeAck, ProbeAckData, Release, ReleaseData | Client responses |
| D | 0-6 | AccessAck, AccessAckData, HintAck, Grant, GrantData, ReleaseAck | Master responses |

**Parameters:**
- Capability: `toT` (to Top), `toB` (to Branch), `toN` (to None)
- Grow: `NtoB`, `NtoT`, `BtoT`
- Report: `TtoB`, `TtoN`, `BtoN`, `TtoT`, `BtoB`, `NtoN`

**Output Format:**
```
<time> <cache_site> <channel> <operation> X <param> <data>
```

Example:
```
1000  L3_L2[0]        A AcquireBlock  X NtoT 0
1005  L3_L2[0]        D GrantData     X toT  deadbeef
```

**Visualization Output:**
- Timeline showing transaction sequences
- State transition tracking
- Error highlighting for state inconsistencies
- Color-coded cache levels and operation types
- Merged two-beat data display for data-carrying opcodes

**Performance Optimization:**
The tool processes cache levels sequentially (L20 → L21 → L10 → L11) then sorts by time, which is more efficient than processing all levels simultaneously.

**Coherence Property Checking:**
`coherence_checker.py` extends timeline-based debugging with property checks on top of parsed events:
- L2 peer mutual exclusion violations
- L1/L2 legal-state violations (inclusive constraints)
- Inclusive violations where L1 is valid while L2 is invalid
- Violation-focused context windows for faster root-cause analysis

**Example Debugging Scenario:**
```
1000  L2_L1[0].C[0]   A AcquireBlock  X NtoT 0
1005  L2_L1[0].C[0]   D GrantData     X toT  deadbeef  [Client state: T]
1020  L2_L1[0].C[0]   C ProbeAck      X BtoN 0          [ERROR: Reported B but was T]
```

This indicates a state tracking bug where the client lost track of its exclusive (T) state.

---

## Waveform File Requirements

### General FST Format Requirements

1. **File Format:**
   - Valid FST (Fast Signal Trace) format
   - Compatible with GTKWave viewer
   - Supported source formats: Verilog VCD, VHDL, or direct FST generation

2. **Simulation Requirements:**
   - Must contain at least one value change section
   - Time scale must be properly defined
   - Timestamps must be monotonically increasing

3. **Signal Naming:**
   - Hierarchical names with '.' separators (e.g., `top.module.signal`)
   - Signal names must be unique within their scope
   - Avoid special characters that interfere with regex matching

### Cache Analysis Specific Requirements

For `deadlock_parser.py`:
- MSHR valid signals for all entries
- Address signals with proper bit width
- State machine status signals
- Clock signal for timestamp reference

For `tllog_parser.py`, `tllog_visual.py`, and `coherence_checker.py`:
- TileLink channel signals (A, B, C, D) with standard naming
- Valid/ready handshake signals for each channel
- Opcode, address, param, source, and data fields
- `bits_size` for accurate multi-beat inference
- Signals must follow naming convention: `<instance>.auto_out_<ch>_<field>`

### Signal Width Conventions

- **Opcode:** 3 bits (0-7)
- **Param:** 3 bits (0-7)
- **Source:** Varies by configuration (typically 4-8 bits)
- **Address:** Must match cache configuration
- **Data:** Typically 64, 128, or 256 bits per beat

### Timing Considerations

- Sample on positive edge only (parser skips negative edges via `step=2`)
- Ensure sufficient simulation time to capture complete transactions
- For deadlock analysis, simulation must run until actual deadlock occurs

---

## Complete Example: End-to-End Cache Analysis

```python
import sys
import pylibfst

# Open FST file
filename = "cache_simulation.fst"
fst = pylibfst.lib.fstReaderOpen(filename.encode("UTF-8"))
if fst == pylibfst.ffi.NULL:
    print(f"Unable to open file '{filename}'!")
    sys.exit(1)

# Get file metadata
print(f"Start Time: {pylibfst.lib.fstReaderGetStartTime(fst)}")
print(f"End Time: {pylibfst.lib.fstReaderGetEndTime(fst)}")
print(f"Variables: {pylibfst.lib.fstReaderGetVarCount(fst)}")

# Get signal hierarchy
(scopes, signals) = pylibfst.get_scopes_signals2(fst)
print(f"Found {len(scopes)} scopes and {len(signals.by_name)} signals")

# Find TileLink channel A valid signal
tl_a_valid = None
for signal in signals.by_name.values():
    if "auto_out_a_valid" in signal.name:
        tl_a_valid = signal
        break

if tl_a_valid:
    print(f"Found TileLink A valid: {tl_a_valid.name}")
    
    # Get all timestamps
    pylibfst.lib.fstReaderSetFacProcessMaskAll(fst)
    timestamps = pylibfst.lib.fstReaderGetTimestamps(fst)
    
    # Query signal at specific times
    buf = pylibfst.ffi.new("char[256]")
    for ts in range(min(10, timestamps.nvals)):
        time = timestamps.val[ts]
        value = pylibfst.string(
            pylibfst.lib.fstReaderGetValueFromHandleAtTime(
                fst, time, tl_a_valid.handle, buf
            )
        )
        print(f"Time {time}: {tl_a_valid.name} = {value}")
    
    pylibfst.lib.fstReaderFreeTimestamps(timestamps)

# Close file
pylibfst.lib.fstReaderClose(fst)
print("Analysis complete!")
```

---

## Best Practices

1. **Memory Management:**
   - Always close FST files with `fstReaderClose()`
   - Free timestamps with `fstReaderFreeTimestamps()`
   - Reuse buffers instead of creating new ones in loops

2. **Performance:**
   - Use `fstReaderSetFacProcessMaskAll()` only when needed
   - Consider filtering signals to reduce processing time
   - For large waveforms, process in chunks or filter by time range

3. **Error Handling:**
   - Check for NULL returns from file operations
   - Validate signal handles before querying values
   - Ensure time values are within valid range

4. **Cache Configuration:**
   - Keep tag/set/offset bit configurations synchronized with RTL
   - Document cache hierarchy in code comments
   - Use constants for magic numbers

5. **Debugging:**
   - Enable verbose output during development
   - Use color-coded output for different transaction types
   - Save intermediate results for multi-step analysis

---
