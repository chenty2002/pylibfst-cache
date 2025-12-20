# TileLink Log Visualizer
# Visualizes TileLink transactions with state tracking and data display
# 
# Features:
# - Shows time on both left and right sides
# - For multi-beat data transactions (ReleaseData, ProbeAckData, GrantData),
#   displays both beats of data in format: [data1,data2]
# - Color-coded cache states: N (None/Invalid), B (Branch), T (Trunk/Tip)
# - Transaction direction indicated by arrows (-> or <-)
#
# Usage: python tllog_visual.py <log_file>

import sys

def parse_log(log):
    # Column widths - reduced to fit in standard terminal (80-120 chars)
    COL_WIDTH = 30  # Width for each transaction display column
    
    # Print header - build it carefully to avoid line wrapping
    print(f"{'time':>6} ", end="")
    print("[L1|0]", end="")
    print(" " * COL_WIDTH, end="")
    print("[L2|0]", end="")
    print(" " * COL_WIDTH, end="")
    print("[ L3 ]", end="")
    print(" " * COL_WIDTH, end="")
    print("[L2|1]", end="")
    print(" " * COL_WIDTH, end="")
    print(f"[L1|1] {'time':>6}")
    
    # Calculate and print separator line
    total_width = 6 + 1 + 6 + COL_WIDTH + 6 + COL_WIDTH + 6 + COL_WIDTH + 6 + COL_WIDTH + 7 + 6
    print("-" * total_width)

    states = ['  ' for _ in range(5)]  # N, B, T states for 5 cache levels

    lines = log.strip().split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i]
        parts = line.split()
        if len(parts) < 6:
            i += 1
            continue

        time = parts[0]
        site = parts[1]
        channel = parts[2]
        opcode = parts[3]
        param = parts[5]  # parts[4] is 'X'
        data = parts[6] if len(parts) >= 7 else None

        # Determine site mapping
        site_map = {
            "L2_L1[0].C[0]": (0, 0, "->"),  # (column_id, node_id, direction)
            "L3_L2[0]": (1, 1, "->"),
            "L3_L2[1]": (2, 3, "<-"),
            "L2_L1[1].C[0]": (3, 4, "<-")
        }
        
        if site not in site_map:
            i += 1
            continue
            
        column_id, node_id, direction = site_map[site]

        # Reverse direction for B and D channels
        if channel in ["B", "D"]:
            direction = "<-" if direction == "->" else "->"

        # Check for multi-beat data transaction
        data_beats = []
        is_data_opcode = opcode in ["ReleaseData", "ProbeAckData", "GrantData"]
        
        if is_data_opcode and data and i + 1 < len(lines):
            next_parts = lines[i + 1].split()
            if (len(next_parts) >= 6 and 
                next_parts[1] == site and 
                next_parts[2] == channel and 
                next_parts[3] == opcode and 
                next_parts[5] == param):
                # Second beat exists
                next_data = next_parts[6] if len(next_parts) >= 7 else None
                data_beats = [data, next_data]
                i += 1  # Skip next line

        # Update cache state
        if channel == "A":
            states[node_id] = ' ' + param[0]
        elif channel == "C":
            original_state = param[0]
            if original_state != states[node_id].strip():
                print(f"\033[36m↓↓↓ State mismatch at {time} for {site}: expected {states[node_id]}, got {original_state} ↓↓↓\033[0m")
            states[node_id] = ' ' + param[-1]
        elif channel == "D" and opcode != "ReleaseAck":
            states[node_id] = ' ' + param[-1]

        # Build transaction display string
        trans_str = f"{opcode} {param}"
        if data_beats:
            d1 = data_beats[0][:8] if data_beats[0] and len(data_beats[0]) > 8 else (data_beats[0] if data_beats[0] else "0")
            d2 = data_beats[1][:8] if data_beats[1] and len(data_beats[1]) > 8 else (data_beats[1] if data_beats[1] else "0")
            trans_str = f"{opcode} {param} [{d1},{d2}]"

        # Build column strings
        cols = [" " * COL_WIDTH for _ in range(4)]
        direction_colored = f"\033[33m{direction}\033[0m"
        
        if column_id in [0, 1]:
            cols[column_id] = (" " + trans_str).ljust(COL_WIDTH - 2) + direction_colored
        else:
            cols[column_id] = direction_colored + (" " + trans_str).ljust(COL_WIDTH - 2)

        # Print row - build it part by part to avoid wrapping
        color_map = {
            '  ': "\033[0m",
            ' N': "\033[0m",
            ' B': "\033[32m",
            ' T': "\033[31m"
        }
        
        print(f"{time:>6} ", end="")
        for j in range(5):
            print(f"[ {color_map[states[j]]}{states[j]}{color_map['  ']} ]", end="")
            if j < 4:
                print(cols[j], end="")
        print(f" {time:>6}")
        
        i += 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tllog_visual.py <log_file>")
        sys.exit(1)

    filename = sys.argv[1]
    with open(filename, "r") as f:
        log = f.read()
        parse_log(log)
