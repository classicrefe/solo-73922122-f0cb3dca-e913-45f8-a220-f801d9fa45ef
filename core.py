"""核心换算逻辑：素材时间 -> 成片时码，勘误条目生成。

约定：所有时间区间均为左闭右开 [start_ms, end_ms)，单位毫秒。
剪辑为等速剪辑，素材时间到成片时间为 1:1 平移映射：
    成片时码 = 成片起点 + (素材时间 - 截取起点)
"""


def overlap(a_start, a_end, b_start, b_end):
    """左闭右开区间 [a_start, a_end) 与 [b_start, b_end) 的重叠部分。

    返回 (start, end)；无重叠（含端点刚好相接）时返回 None。
    """
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return (s, e) if s < e else None


def to_program_time(seg, m_start, m_end):
    """素材区间 [m_start, m_end) 在剪辑段 seg 中对应的成片区间（等速 1:1）。"""
    offset = seg["program_start_ms"] - seg["cut_start_ms"]
    return (m_start + offset, m_end + offset)


def compute_errata_items(segments, old, new):
    """逐剪辑段计算一句字幕更正（old -> new）产生的勘误条目。

    segments: 剪辑表行（含 program_id/segment_id/cut_start_ms/cut_end_ms/program_start_ms）
    old/new:  dict，含 start_ms、end_ms、text

    每条目 action 为 delete / add / replace 之一：
      - 仅旧区间与该段有重叠 -> delete（删去旧字幕）
      - 仅新区间与该段有重叠 -> add（补加新字幕）
      - 新旧均与该段有重叠   -> replace（旧时码处替换为新时码/新文字）
      - 新旧重叠换算后完全相同且文字未变 -> 该段无可见变化，不产生条目
    """
    items = []
    for seg in segments:
        old_ov = overlap(old["start_ms"], old["end_ms"],
                         seg["cut_start_ms"], seg["cut_end_ms"])
        new_ov = overlap(new["start_ms"], new["end_ms"],
                         seg["cut_start_ms"], seg["cut_end_ms"])
        old_prog = to_program_time(seg, *old_ov) if old_ov else None
        new_prog = to_program_time(seg, *new_ov) if new_ov else None

        if old_prog and new_prog:
            if old_prog == new_prog and old["text"] == new["text"]:
                continue  # 该段内字幕无可见变化
            action = "replace"
        elif old_prog:
            action = "delete"
        elif new_prog:
            action = "add"
        else:
            continue

        items.append({
            "program_id": seg["program_id"],
            "segment_id": seg["segment_id"],
            "action": action,
            "old_start_ms": old_prog[0] if old_prog else None,
            "old_end_ms": old_prog[1] if old_prog else None,
            "new_start_ms": new_prog[0] if new_prog else None,
            "new_end_ms": new_prog[1] if new_prog else None,
            "old_text": old["text"] if old_prog else None,
            "new_text": new["text"] if new_prog else None,
        })
    return items


def segment_sort_key(seg):
    """剪辑段排序：先成片号，段号为纯数字时按数值排序。"""
    sid = str(seg["segment_id"])
    return (seg["program_id"], 0, int(sid), "") if sid.isdigit() \
        else (seg["program_id"], 1, 0, sid)


def fmt_tc(ms):
    """毫秒 -> HH:MM:SS.mmm 时码字符串（支持负值）。"""
    if ms is None:
        return ""
    ms = int(ms)
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
