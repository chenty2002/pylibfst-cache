#!/usr/bin/env python3
"""
coherence_checker.py  —  TileLink 一致性性质检测工具
=====================================================
支持两种输入模式：

模式 1（默认）：直接读取 FST 波形文件，提取所有地址的 TileLink 事务。
模式 2（--log）：读取 tllog_parser.py 输出的文本日志文件。

按 properties.tex 中的性质进行检测：

性质 1  — L2 peer 互斥 (L2 Mutual Exclusion)
    对同一地址，两个 L2（coupledL2 / coupledL2_1）不能同时处于 Tip-Tip 或 Tip-Branch。

性质 2  — L1/L2 垂直状态约束 (inclusive 约束)
    按 Table 1 检查每个 L1/L2 对的状态组合，同时检查 inclusive 约束。

性质 3  — Inclusive 约束（L1 有效则 L2 必须有效）

输出格式与 tllog_visual.py 保持一致，违规行附近有说明标注。

用法：
    # FST 模式（自动扫描所有地址）
    python coherence_checker.py <fst_file>
    python coherence_checker.py <fst_file> --addr 0x14   # 只分析特定地址
    python coherence_checker.py <fst_file> --verbose     # 输出所有事务

    # 日志模式（使用 tllog_parser.py 的文本输出）
    python tllog_parser.py <fst_file> <addr> > log.txt
    python coherence_checker.py --log log.txt
    python coherence_checker.py --log log.txt --addr 0x14
"""

import sys
import re
import argparse
from collections import defaultdict

# 复用 deadlock_parser 中的 pylibfst 加载及自动检测逻辑
sys.path.insert(0, ".")
from deadlock_parser import (
    detect_cache_widths,
    get_signal,
    l1tagbits, l1setbits, l1offbits,
    l2tagbits, l2setbits, l2offbits,
    l3tagbits, l3setbits, l3offbits,
)
import deadlock_parser as dp
import pylibfst

# ── ANSI 颜色 ───────────────────────────────────────────────────────────────
GREEN        = '\033[92m'
CYAN         = '\033[96m'
MAGENTA      = '\033[95m'
YELLOW       = '\033[93m'
RED          = '\033[91m'
BOLD_RED     = '\033[1;91m'
BLUE         = '\033[94m'
DIM          = '\033[2m'
RESET        = '\033[0m'

# ── TileLink 操作码 / 参数字符串（与 tllog_parser.py 一致） ──────────────────
_ALLOPS = {
    "a": ["PutFullData","PutPartialData","ArithmeticData","LogicalData","Get","Hint","AcquireBlock","AcquirePerm"],
    "b": ["PutFullData","PutPartialData","ArithmeticData","LogicalData","Get","Hint","Probe"],
    "c": ["AccessAck","AccessAckData","HintAck","Invalid","ProbeAck","ProbeAckData","Release","ReleaseData"],
    "d": ["AccessAck","AccessAckData","HintAck","Invalid","Grant","GrantData","ReleaseAck"],
    "e": ["GrantAck"],
}
_CAP    = ["toT","toB","toN"]
_GROW   = ["NtoB","NtoT","BtoT"]
_REPORT = ["TtoB","TtoN","BtoN","TtoT","BtoB","NtoN"]
_ALLPARAMS = {"a":_GROW,"b":_CAP,"c":_REPORT,"d":_CAP,"e":[""]}

def opcode_str(chn, opcode):
    ops = _ALLOPS.get(chn, [])
    return ops[opcode] if opcode < len(ops) else "Unknown"

def param_str(chn, param):
    ps = _ALLPARAMS.get(chn, [])
    return ps[param] if param < len(ps) else "Unknown"

# ── 缓存层次 / 站点定义 ───────────────────────────────────────────────────────
# site_key → (column_idx, visual_label)
# 列顺序: L1|0(0)  L2|0(1)  L3(2)  L2|1(3)  L1|1(4)
SITE_BY_MODULE = {
    "VerifyTop.coupledL2AsL1":   ("L2_L1[0].C[0]", 0),
    "VerifyTop.coupledL2":       ("L3_L2[0]",       1),
    "VerifyTop.coupledL2_1":     ("L3_L2[1]",       3),
    "VerifyTop.coupledL2AsL1_1": ("L2_L1[1].C[0]",  4),
}

# 节点逻辑编号  L1|0=0  L2|0=1  L3=2  L2|1=3  L1|1=4
# 在可视化列中：列 0 对应节点 0，列 1 对应节点 1，列 3 对应节点 3，列 4 对应节点 4
# L3 节点(2)不是独立事件产生者，但由两端事务推断

# ── 状态转移辅助 ─────────────────────────────────────────────────────────────
# 节点的状态用单字符: 'N','B','T'（TrunkTip/Trunk 均记为 'T'）
INITIAL_STATE = 'N'

# TileLink 中真正影响缓存权限状态的操作
# A 通道: AcquireBlock(6) / AcquirePerm(7)  → 客户端升级
# C 通道: ProbeAck(4) / ProbeAckData(5)     → 客户端被动降级（响应 Probe）
#          Release(6) / ReleaseData(7)       → 客户端主动降级
# D 通道: Grant(4) / GrantData(5)            → 客户端状态升级（服务端发出）
#          ReleaseAck(6)                      → 服务端确认释放（客户端已在 C 通道更新）
# B 通道: Probe → 不直接改变本节点状态（等对方 C ProbeAck 时更新）

_ACQUIRE_OPS  = frozenset({'AcquireBlock', 'AcquirePerm'})
_PROBEACK_OPS = frozenset({'ProbeAck', 'ProbeAckData'})
_RELEASE_OPS  = frozenset({'Release', 'ReleaseData'})
_GRANT_OPS    = frozenset({'Grant', 'GrantData'})

def _next_state_from_channel(chn, opcode_s, param_s, current):
    """根据通道/操作码/参数推断节点新状态（字符 N/B/T）。

    状态更新规则（符合 TileLink 协议语义）：
    - A 通道 Acquire：发起方请求权限，但权限在 D 通道 Grant 返回时才确认 → 不更新
    - C 通道 ProbeAck/Release：降级完成，立即更新 → 更新
    - D 通道 Grant/GrantData：权限授予完成 → 更新
    - 其他（Put/Get/AccessAck/ReleaseAck/Probe 等）→ 不更新
    """
    if chn == 'C' and opcode_s in (_PROBEACK_OPS | _RELEASE_OPS):
        # ProbeAck/Release TtoN→N, TtoB→B, BtoN→N, etc.
        last = param_s[-1]
        return last if last in ('B', 'T', 'N') else current
    elif chn == 'D' and opcode_s in _GRANT_OPS:
        # GrantData toT→T, toB→B
        last = param_s[-1]
        return last if last in ('B', 'T') else current
    # 其余所有操作（AcquireBlock, PutFullData, AccessAck, ReleaseAck, Probe 等）不改变状态
    return current

# ── properties.tex 中的约束表 ─────────────────────────────────────────────────
# Table 1: L1/L2 合法状态对 (L2_col, L1_col) → True=合法
# TT(T) / T(T) / B / N  — 实现中 T 表示 Tip 或 Trunk
L1L2_VALID = {
    # (L2, L1)
    ('T','T'): False,  # ×
    ('T','B'): True,   # ✓
    ('T','N'): True,   # ✓
    ('B','T'): False,  # ×
    ('B','B'): True,   # ✓ (Branch-Branch 可共存)
    ('B','N'): True,   # ✓
    ('N','T'): False,  # ×
    ('N','B'): False,  # ×
    ('N','N'): True,   # ✓
    # L2=T时L1只能是B或N；L2=B时L1B/N都行；L2=N时L1必须是N
    # 从表中读取：
    # L2\L1 | TT  T   B   N
    #  TT   |  ×  ×   ✓   ✓
    #  T    |  ✓  ×   ×   ×   ← L2=T(Trunk) 时L1=T(TrunkTip) 合法
    #  B    |  ×  ×   ✓   ✓
    #  N    |  ×  ×   ×   ✓
    # 注：表中 TT(TrunkTip) 和 T(Trunk) 分别对应不同角色
    # 简化：本实现不区分 TrunkTip vs Trunk，统一用 T
    # 根据表：L2=T时，L1=T(TrunkTip) ✓，L1=B ×，L1=N ×
    #         L2=T(Trunk)时 L1=T ×，L1=B ×，L1=N ×  (实际上 TrunkTip 才允许)
    # 由于FST层不区分，用如下规则：
}

# L1/L2 合法状态对（inclusive 约束，按 properties.tex Table 1）
# TileLink 协议中 T 同时表示 Trunk 和 TrunkTip，无法从 FST 参数区分。
# 合法性规则：
#   L2=T, L1=T → 合法（L2 持 Trunk，L1 持 TrunkTip）
#   L2=T, L1=B → 非法（L2 已升 T，L1 不应保有 Branch；L2 TrunkTip 时 L1 必须是 B，
#                       但 L2 Trunk 时 L1 不能是 B，这里保守处理为非法）
#   L2=B, L1=T → 非法（L1 不能比 L2 权限高）
#   L2=N, L1=T/B → 非法（Inclusive 要求 L1 有效则 L2 必须有效）
L1L2_LEGAL = {
    # (L2_state, L1_state)
    ('T', 'T'): True,   # ✓ L2=Trunk, L1=TrunkTip（合法）
    ('T', 'B'): False,  # × L2=T 时 L1 不能是 B（L2 是 TrunkTip 时 L1 可以是 B，但无法区分）
    ('T', 'N'): True,   # ✓
    ('B', 'T'): False,  # × L1 权限不能超过 L2
    ('B', 'B'): True,   # ✓ 多个 L1 可共享 Branch
    ('B', 'N'): True,   # ✓
    ('N', 'T'): False,  # × Inclusive 违规
    ('N', 'B'): False,  # × Inclusive 违规
    ('N', 'N'): True,   # ✓
}

# L2/L3 合法状态对（non-inclusive 约束，按 properties.tex Table 2）
L2L3_LEGAL = {
    # (L3_state, L2_state)
    ('T', 'T'): False,
    ('T', 'B'): True,
    ('T', 'N'): True,
    ('B', 'T'): False,
    ('B', 'B'): True,
    ('B', 'N'): True,
    ('N', 'T'): True,
    ('N', 'B'): True,
    ('N', 'N'): True,
}

# L2 peer 互斥（两个 L2 对同一地址）：不能同时 T-T 或 T-B
L2PEER_LEGAL = {
    ('T', 'T'): False,
    ('T', 'B'): False,
    ('B', 'T'): False,
    ('B', 'B'): True,
    ('T', 'N'): True,
    ('N', 'T'): True,
    ('B', 'N'): True,
    ('N', 'B'): True,
    ('N', 'N'): True,
}

# ── 事务记录 ─────────────────────────────────────────────────────────────────
class TxEvent:
    """一条 TileLink 通道握手事件。"""
    __slots__ = ('time','module','site','col','chn','opcode_s','param_s','data','address')
    def __init__(self, time, module, site, col, chn, opcode_s, param_s, data, address):
        self.time     = time
        self.module   = module
        self.site     = site
        self.col      = col       # 0-4 列编号
        self.chn      = chn       # 'A','B','C','D'
        self.opcode_s = opcode_s
        self.param_s  = param_s
        self.data     = data
        self.address  = address


# ── FST 解析 ─────────────────────────────────────────────────────────────────
def _get_val(fst, time, signal, buf, base=2):
    if signal is None:
        return 0
    raw = pylibfst.helpers.string(
        pylibfst.lib.fstReaderGetValueFromHandleAtTime(fst, time, signal.handle, buf))
    try:
        return int(raw, base=base)
    except ValueError:
        return 0


def extract_events(fst_file):
    """
    读取 fst_file，返回 dict: address → list[TxEvent]（按时间排序）。
    同时返回波形时间范围 (start_time, end_time)。
    """
    fst = pylibfst.lib.fstReaderOpen(fst_file.encode("UTF-8"))
    if fst == pylibfst.ffi.NULL:
        print(f"{RED}无法打开 FST 文件: {fst_file}{RESET}")
        sys.exit(1)

    (scopes, signals) = pylibfst.get_scopes_signals2(fst)

    # 自动检测位宽（写入 deadlock_parser 全局变量）
    detect_cache_widths(signals)

    pylibfst.lib.fstReaderSetFacProcessMaskAll(fst)
    timestamps = pylibfst.lib.fstReaderGetTimestamps(fst)
    if timestamps.nvals == 0:
        print(f"{RED}没有时间戳数据{RESET}")
        sys.exit(1)

    start_time = timestamps.val[0]
    end_time   = timestamps.val[timestamps.nvals - 1]

    buf = pylibfst.ffi.new("char[256]")

    # FST 信号名可能带有位宽后缀，如 "auto_out_a_bits_opcode [2:0]"
    # 用宽松匹配：允许可选的 " [msb:lsb]" 后缀
    _BW_SUFFIX = r'(?: \[\d+:\d+\])?$'

    def get_sig(base_name):
        """按精确名或带位宽后缀名查找信号。"""
        pat = re.compile(rf"{re.escape(base_name)}{_BW_SUFFIX}")
        for sig in signals.by_name.values():
            if pat.match(sig.name):
                return sig
        return None

    # 预先加载每个模块每个通道的信号句柄
    modules = list(SITE_BY_MODULE.keys())
    caches_sigs = {}  # module → chn → {valid,ready,opcode,param,address,source,size,data}
    for mod in modules:
        caches_sigs[mod] = {}
        for chn in ['a', 'b', 'c', 'd']:
            caches_sigs[mod][chn] = {
                "valid":   get_sig(f"{mod}.auto_out_{chn}_valid"),
                "ready":   get_sig(f"{mod}.auto_out_{chn}_ready"),
                "opcode":  get_sig(f"{mod}.auto_out_{chn}_bits_opcode"),
                "param":   get_sig(f"{mod}.auto_out_{chn}_bits_param"),
                "address": get_sig(f"{mod}.auto_out_{chn}_bits_address"),
                "source":  get_sig(f"{mod}.auto_out_{chn}_bits_source"),
                "size":    get_sig(f"{mod}.auto_out_{chn}_bits_size"),
                "data":    get_sig(f"{mod}.auto_out_{chn}_bits_data"),
            }

    # 计算每个模块的数据总线字节数（用于多拍数据）
    data_bus_bytes = {}
    for mod in modules:
        d_data = caches_sigs[mod]['d']['data']
        if d_data and d_data.length > 0:
            data_bus_bytes[mod] = max(1, d_data.length // 8)
        else:
            data_bus_bytes[mod] = 8

    # 每个模块的 pending 状态（用于 GrantData 多拍绑定）
    mod_state = {mod: {
        'a_src': -1, 'a_beats': 0,
        'c_src': -1, 'c_beats': 0,
    } for mod in modules}

    addr_events = defaultdict(list)  # addr → [TxEvent]

    for ts in range(0, timestamps.nvals, 2):  # 只取正边沿
        time = timestamps.val[ts]

        for mod in modules:
            site, col = SITE_BY_MODULE[mod]
            ms = mod_state[mod]
            dbb = data_bus_bytes[mod]

            for chn in ['a', 'b', 'c', 'd']:
                sigs = caches_sigs[mod][chn]
                valid = _get_val(fst, time, sigs["valid"], buf)
                ready = _get_val(fst, time, sigs["ready"], buf)
                if not (valid == 1 and ready == 1):
                    continue

                opcode_v = _get_val(fst, time, sigs["opcode"], buf)
                param_v  = _get_val(fst, time, sigs["param"],  buf)
                source_v = _get_val(fst, time, sigs["source"], buf)
                data_v   = _get_val(fst, time, sigs["data"],   buf)
                ops      = opcode_str(chn, opcode_v)
                prs      = param_str(chn, param_v)

                if chn != 'd':
                    addr_v = _get_val(fst, time, sigs["address"], buf)

                    if chn == 'a':
                        ms['a_src'] = source_v
                        size_v = _get_val(fst, time, sigs["size"], buf)
                        ms['a_beats'] = max(1, (1 << size_v) // dbb)
                    if chn == 'c' and opcode_v in (6, 7):  # Release/ReleaseData
                        ms['c_src'] = source_v
                        size_v = _get_val(fst, time, sigs["size"], buf)
                        ms['c_beats'] = max(1, (1 << size_v) // dbb)

                    ev = TxEvent(time, mod, site, col, chn.upper(), ops, prs, data_v, addr_v)
                    addr_events[addr_v].append(ev)

                else:  # D 通道：通过 source 匹配 A/C 事务的地址
                    match_a = (opcode_v in (4, 5)) and source_v == ms['a_src'] and ms['a_beats'] > 0
                    match_c = (opcode_v == 6)       and source_v == ms['c_src'] and ms['c_beats'] > 0

                    if match_a:
                        ms['a_beats'] -= 1
                        if ms['a_beats'] == 0:
                            addr_v = None  # 最后一拍后清空，地址已记录在 A 事务
                        else:
                            addr_v = None
                        # 用 a_src 找到对应 A 事务地址
                        # (简化: 遍历最近事件找地址)
                        target_addr = _find_recent_a_addr(addr_events, mod, source_v)
                        if target_addr is not None:
                            ev = TxEvent(time, mod, site, col, 'D', ops, prs, data_v, target_addr)
                            addr_events[target_addr].append(ev)
                        if ms['a_beats'] == 0:
                            ms['a_src'] = -1

                    if match_c:
                        ms['c_beats'] -= 1
                        target_addr = _find_recent_c_addr(addr_events, mod, source_v)
                        if target_addr is not None:
                            ev = TxEvent(time, mod, site, col, 'D', ops, prs, data_v, target_addr)
                            addr_events[target_addr].append(ev)
                        if ms['c_beats'] == 0:
                            ms['c_src'] = -1

    pylibfst.lib.fstReaderFreeTimestamps(timestamps)
    pylibfst.lib.fstReaderClose(fst)

    # 排序
    for addr in addr_events:
        addr_events[addr].sort(key=lambda e: e.time)

    return dict(addr_events), start_time, end_time


def _find_recent_a_addr(addr_events, mod, source):
    """在 addr_events 中找 mod 发出的最近 A 通道事件，返回其地址。"""
    best_time = -1
    best_addr = None
    for addr, evs in addr_events.items():
        for ev in reversed(evs):
            if ev.module == mod and ev.chn == 'A' and ev.time > best_time:
                best_time = ev.time
                best_addr = addr
                break
    return best_addr

def _find_recent_c_addr(addr_events, mod, source):
    """在 addr_events 中找 mod 发出的最近 C 通道 Release 事件，返回其地址。"""
    best_time = -1
    best_addr = None
    for addr, evs in addr_events.items():
        for ev in reversed(evs):
            if (ev.module == mod and ev.chn == 'C'
                    and ev.opcode_s in ('Release','ReleaseData')
                    and ev.time > best_time):
                best_time = ev.time
                best_addr = addr
                break
    return best_addr


# ── 状态追踪 ─────────────────────────────────────────────────────────────────
def track_states(events):
    """
    按时间顺序重放事件列表，追踪五个节点的状态。
    列编号: 0=L1|0, 1=L2|0, 2=L3(推断), 3=L2|1, 4=L1|1
    返回: list of (event, states_before, states_after)
           states = ['N','N','N','N','N']  (5个节点)
    """
    states = ['N'] * 5
    result = []
    for ev in events:
        before = states[:]
        node = ev.col
        new_s = _next_state_from_channel(ev.chn, ev.opcode_s, ev.param_s, states[node])
        states[node] = new_s
        result.append((ev, before[:], states[:]))
    return result


# ── 违规检测 ─────────────────────────────────────────────────────────────────
class Violation:
    CATEGORIES = {
        'L2_PEER':     'L2 Peer 互斥违规 (Tip-Tip / Tip-Branch)',
        'L1L2_LEGAL':  'L1/L2 状态组合违规 (inclusive 约束)',
        'L2L3_LEGAL':  'L2/L3 状态组合违规 (non-inclusive 约束)',
        'L1_INCLUSIVE': 'L1 Inclusive 违规 (L1 有效但 L2 无效)',
    }

    def __init__(self, category, time, address, states, ev, description):
        self.category    = category
        self.time        = time
        self.address     = address
        self.states      = states   # 违规时刻的状态快照 (5个节点)
        self.ev          = ev       # 触发违规的事件（可为 None）
        self.description = description


def detect_violations(tracked, address):
    """
    遍历 (event, before, after) 三元组，检测各类违规。

    跳过规则（避免假阳性）：
    - 若某个节点正在被 Probe（B 通道收到 Probe，C 通道 ProbeAck 尚未发出），
      该节点处于"降级进行中"过渡状态，跳过该节点相关的 L1/L2 和 Inclusive 检查。
    - A 通道 Acquire 进行中时（等待 D 通道 Grant），同样跳过检查。

    返回 list[Violation]。
    """
    violations = []

    # 跟踪哪些节点有 pending Probe / pending Acquire
    # pending_probe: set of col_id（已收到 B Probe，未收到 C ProbeAck）
    # pending_acquire: set of col_id（已发 A Acquire，未收到 D Grant）
    pending_probe   = set()   # col → True
    pending_acquire = set()   # col → True

    for ev, before, after in tracked:
        # 更新 pending 状态
        if ev.chn == 'B' and ev.opcode_s == 'Probe':
            pending_probe.add(ev.col)
        elif ev.chn == 'C' and ev.opcode_s in _PROBEACK_OPS:
            pending_probe.discard(ev.col)
        elif ev.chn == 'A' and ev.opcode_s in _ACQUIRE_OPS:
            pending_acquire.add(ev.col)
        elif ev.chn == 'D' and ev.opcode_s in _GRANT_OPS:
            pending_acquire.discard(ev.col)

        s = after
        time = ev.time

        # ── 性质 1: L2 peer 互斥 (节点1=L2|0, 节点3=L2|1)
        # 只要两个 L2 都处于稳定状态（无 pending），就检查互斥
        if 1 not in pending_probe and 3 not in pending_probe \
                and 1 not in pending_acquire and 3 not in pending_acquire:
            pair = (s[1], s[3])
            if not L2PEER_LEGAL.get(pair, True):
                violations.append(Violation(
                    'L2_PEER', time, address, s[:], ev,
                    f"L2|0={s[1]}, L2|1={s[3]} — 同时持有互斥状态"
                ))

        # ── 性质 2: L1/L2 垂直 (节点0=L1|0, 节点1=L2|0)
        if 0 not in pending_probe and 1 not in pending_probe \
                and 0 not in pending_acquire and 1 not in pending_acquire:
            p01 = (s[1], s[0])
            if not L1L2_LEGAL.get(p01, True):
                cat = 'L1_INCLUSIVE' if (s[0] != 'N' and s[1] == 'N') else 'L1L2_LEGAL'
                violations.append(Violation(
                    cat, time, address, s[:], ev,
                    f"L2|0={s[1]}, L1|0={s[0]} — L1/L2[0] 状态非法"
                ))

        if 3 not in pending_probe and 4 not in pending_probe \
                and 3 not in pending_acquire and 4 not in pending_acquire:
            p34 = (s[3], s[4])
            if not L1L2_LEGAL.get(p34, True):
                cat = 'L1_INCLUSIVE' if (s[4] != 'N' and s[3] == 'N') else 'L1L2_LEGAL'
                violations.append(Violation(
                    cat, time, address, s[:], ev,
                    f"L2|1={s[3]}, L1|1={s[4]} — L1/L2[1] 状态非法"
                ))

    # 去重（同一时间同一类别）
    seen = set()
    unique = []
    for v in violations:
        key = (v.category, v.time, v.address)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


# ── 可视化输出 ────────────────────────────────────────────────────────────────
COL_WIDTH = 30

_COLOR_STATE = {
    'N': RESET,
    'B': GREEN,
    'T': RED,
}

def _fmt_state(s):
    c = _COLOR_STATE.get(s, RESET)
    return f"{c} {s}{RESET}"

def _print_header():
    print(f"{'time':>6} ", end="")
    print("[L1|0]", end="")
    print(" " * COL_WIDTH, end="")
    print("[L2|0]", end="")
    print(" " * COL_WIDTH, end="")
    print("[ L3 ]", end="")
    print(" " * COL_WIDTH, end="")
    print("[L2|1]", end="")
    print(" " * COL_WIDTH, end="")
    print("[L1|1]")
    total = 6+1+6+COL_WIDTH+6+COL_WIDTH+6+COL_WIDTH+6+COL_WIDTH+6
    print("-" * total)


def _print_event_row(ev, states_after):
    """打印单行事件（tllog_visual.py 格式）。"""
    time    = ev.time
    col     = ev.col
    ops     = ev.opcode_s
    prs     = ev.param_s
    chn     = ev.chn

    # 确定箭头方向：A/C 通道从左向右发出；B/D 从右向左
    if col in (0, 1):  # 左侧节点
        base_dir = "->"
    else:
        base_dir = "<-"
    if chn in ("B", "D"):
        base_dir = "<-" if base_dir == "->" else "->"

    arrow_colored = f"\033[33m{base_dir}\033[0m"
    trans_str = f"{ops} {prs}"

    cols = [" " * COL_WIDTH for _ in range(4)]
    if col in (0, 1):
        cols[col] = (" " + trans_str).ljust(COL_WIDTH - 2) + arrow_colored
    elif col in (3, 4):
        c = col - 1  # 映射到 cols 索引 (0-3)
        cols[c] = arrow_colored + (" " + trans_str).ljust(COL_WIDTH - 2)

    print(f"{time:>6} ", end="")
    for j in range(5):
        s = states_after[j] if j < len(states_after) else 'N'
        c = _COLOR_STATE.get(s, RESET)
        print(f"[{c} {s}{RESET} ]", end="")
        if j < 4:
            print(cols[j], end="")
    print()


def print_violation_context(addr, violations, tracked, context_window=5):
    """
    打印包含违规的上下文事务片段（前后各 context_window 行），
    并突出标注违规行。
    """
    if not violations:
        return

    viol_times = {v.time for v in violations}

    # 按违规类别分组输出
    by_cat = defaultdict(list)
    for v in violations:
        by_cat[v.category].append(v)

    for cat, vlist in by_cat.items():
        label = Violation.CATEGORIES.get(cat, cat)
        print(f"\n{BOLD_RED}{'='*70}{RESET}")
        print(f"{BOLD_RED}[地址 0x{addr:x}] 检测到: {label}{RESET}")
        print(f"{BOLD_RED}{'='*70}{RESET}")
        print(f"  共 {len(vlist)} 处违规时间点: "
              + ", ".join(str(v.time) for v in vlist))
        print()

        # 找到所有违规时间点附近的事件索引
        ev_indices = set()
        for v in vlist:
            for idx, (ev, before, after) in enumerate(tracked):
                if ev.time == v.time:
                    for k in range(max(0, idx - context_window),
                                   min(len(tracked), idx + context_window + 1)):
                        ev_indices.add(k)

        if not ev_indices:
            print(f"{DIM}(无关联事件){RESET}")
            continue

        sorted_indices = sorted(ev_indices)
        _print_header()

        prev_idx = -1
        for idx in sorted_indices:
            if prev_idx >= 0 and idx > prev_idx + 1:
                print(f"{DIM}  ... (省略 {idx - prev_idx - 1} 行) ...{RESET}")
            prev_idx = idx

            ev, before, after = tracked[idx]
            is_viol = ev.time in viol_times
            if is_viol:
                # 找出这一时刻的违规描述
                descs = [v.description for v in vlist if v.time == ev.time]
                print(f"{BOLD_RED}↓↓↓ 违规时刻 {ev.time} | {' | '.join(descs)} ↓↓↓{RESET}")
            _print_event_row(ev, after)

        print()

    print(f"{GREEN}片段输出完毕{RESET}")


# ── 文本日志解析（tllog_parser.py 输出格式） ─────────────────────────────────
# 格式: "<time> <site> <CHN> <opcode> X <param> [data]"
# 站点: L2_L1[0].C[0]  L3_L2[0]  L3_L2[1]  L2_L1[1].C[0]

_SITE_TO_COL = {
    "L2_L1[0].C[0]": 0,
    "L3_L2[0]":      1,
    "L3_L2[1]":      3,
    "L2_L1[1].C[0]": 4,
}

_SITE_TO_MODULE = {
    "L2_L1[0].C[0]": "VerifyTop.coupledL2AsL1",
    "L3_L2[0]":      "VerifyTop.coupledL2",
    "L3_L2[1]":      "VerifyTop.coupledL2_1",
    "L2_L1[1].C[0]": "VerifyTop.coupledL2AsL1_1",
}

def parse_log_text(log_text, filter_addr=None):
    """
    解析 tllog_parser.py 的文本输出，返回 dict: address → list[TxEvent]。
    由于日志中没有地址字段（通过 tllog_parser 的 target_addr 参数过滤时已指定），
    所有事件都归到一个伪地址 `filter_addr` 或 0。
    """
    # tllog_parser.py 的输出中没有地址列，需要从 tllog_parser 调用时传入的地址推断
    # 格式: "  1510 L3_L2[0]         A AcquireBlock X NtoB 0"
    pat = re.compile(
        r'^\s*(\d+)\s+(L[^\ ]+)\s+([A-E])\s+(\S+)\s+X\s+(\S+)(?:\s+([0-9a-fA-F]+))?'
    )
    addr = filter_addr if filter_addr is not None else 0
    events = []
    for line in log_text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        time_v   = int(m.group(1))
        site     = m.group(2)
        chn      = m.group(3)
        opcode_s = m.group(4)
        param_s  = m.group(5)
        data_s   = m.group(6)
        data_v   = int(data_s, 16) if data_s else 0

        if site not in _SITE_TO_COL:
            continue
        col = _SITE_TO_COL[site]
        mod = _SITE_TO_MODULE[site]
        ev  = TxEvent(time_v, mod, site, col, chn, opcode_s, param_s, data_v, addr)
        events.append(ev)

    events.sort(key=lambda e: e.time)
    return {addr: events}


# ── 汇总报告 ─────────────────────────────────────────────────────────────────
def print_summary(all_violations_by_addr):
    total = sum(len(vs) for vs in all_violations_by_addr.values())
    if total == 0:
        print(f"\n{GREEN}✓ 未检测到一致性违规{RESET}")
        return

    print(f"\n{BOLD_RED}{'='*70}{RESET}")
    print(f"{BOLD_RED}一致性检测汇总: 共检测到 {total} 处违规{RESET}")
    print(f"{BOLD_RED}{'='*70}{RESET}")

    cat_counts = defaultdict(int)
    for addr, vs in all_violations_by_addr.items():
        for v in vs:
            cat_counts[v.category] += 1

    for cat, cnt in sorted(cat_counts.items()):
        label = Violation.CATEGORIES.get(cat, cat)
        print(f"  {YELLOW}{label}{RESET}: {cnt} 处")

    print(f"\n{YELLOW}涉及地址:{RESET}")
    for addr, vs in sorted(all_violations_by_addr.items()):
        cats = set(v.category for v in vs)
        cat_labels = ', '.join(Violation.CATEGORIES.get(c, c) for c in cats)
        print(f"  0x{addr:x}: {len(vs)} 处  [{cat_labels}]")


# ── 主函数 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="TileLink 一致性性质检测器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    # 两种互斥的输入源
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("fst_file", nargs="?", help="FST 波形文件路径")
    src_group.add_argument("--log",    metavar="LOG_FILE",
                           help="tllog_parser.py 输出的文本日志文件路径（代替 FST 输入）")

    parser.add_argument("--addr",    help="只分析特定地址（十六进制），FST 模式下可省略", default=None)
    parser.add_argument("--verbose", action="store_true",
                        help="输出所有事务行（含正常行），不仅是违规片段")
    parser.add_argument("--context", type=int, default=6,
                        help="违规片段前后显示的事件行数（默认 6）")
    args = parser.parse_args()

    filter_addr = int(args.addr, 16) if args.addr else None

    if args.log:
        # ── 日志模式 ──────────────────────────────────────────────────────────
        print(f"{CYAN}正在读取日志文件: {args.log}{RESET}")
        with open(args.log, "r", encoding="utf-8") as f:
            log_text = f.read()
        addr_events = parse_log_text(log_text, filter_addr)
        print(f"{CYAN}共解析 {sum(len(v) for v in addr_events.values())} 条事件，"
              f"涉及 {len(addr_events)} 个地址{RESET}\n")
    else:
        # ── FST 模式 ──────────────────────────────────────────────────────────
        print(f"{CYAN}正在读取 FST 文件: {args.fst_file}{RESET}")
        addr_events, t_start, t_end = extract_events(args.fst_file)
        print(f"{CYAN}时间范围: {t_start} ~ {t_end}，共解析 {len(addr_events)} 个地址{RESET}\n")

        if filter_addr is not None:
            if filter_addr not in addr_events:
                print(f"{RED}地址 0x{filter_addr:x} 无事务记录{RESET}")
                sys.exit(1)
            addr_events = {filter_addr: addr_events[filter_addr]}

    all_violations = {}

    for addr in sorted(addr_events.keys()):
        events = addr_events[addr]
        if not events:
            continue
        tracked = track_states(events)

        if args.verbose:
            print(f"\n{CYAN}── 地址 0x{addr:x} 全部事务 ──{RESET}")
            _print_header()
            for ev, before, after in tracked:
                _print_event_row(ev, after)

        violations = detect_violations(tracked, addr)
        if violations:
            all_violations[addr] = violations
            print_violation_context(addr, violations, tracked, context_window=args.context)

    print_summary(all_violations)


if __name__ == "__main__":
    main()
