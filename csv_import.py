"""素材字幕 / 剪辑表 CSV 的解析与校验。

任一数据行出错则整个文件不入库（全部或全无），错误按行号汇总返回。
表头支持中文标准列名及若干英文别名，编码按 UTF-8（兼容 BOM）。
"""
import csv
import io

SUBTITLE_FIELDS = {
    "material_id": ["素材号", "material_id", "material"],
    "sentence_id": ["句号", "sentence_id", "sentence"],
    "start_ms": ["开始毫秒", "起点毫秒", "起始毫秒", "start_ms"],
    "end_ms": ["结束毫秒", "终点毫秒", "终止毫秒", "end_ms"],
    "text": ["原文", "字幕原文", "内容", "text"],
}

EDIT_FIELDS = {
    "program_id": ["成片号", "program_id", "program"],
    "segment_id": ["剪辑段号", "segment_id", "segment"],
    "material_id": ["素材号", "material_id", "material"],
    "cut_start_ms": ["截取开始毫秒", "截取起点毫秒", "cut_start_ms"],
    "cut_end_ms": ["截取结束毫秒", "截取终点毫秒", "cut_end_ms"],
    "program_start_ms": ["成片起点毫秒", "成片起点", "program_start_ms"],
}

MAX_ERRORS = 20


class CsvError(Exception):
    """CSV 解析/校验失败，errors 为逐条中文错误信息。"""

    def __init__(self, errors):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _map_headers(header, fields):
    mapping = {}
    cells = [(h or "").strip() for h in header]
    for key, aliases in fields.items():
        for alias in aliases:
            if alias in cells:
                mapping[key] = cells.index(alias)
                break
    return mapping


def _to_int(rec, key, label, errors):
    try:
        rec[key] = int(rec[key])
    except (ValueError, TypeError):
        errors.append(f"{label}必须为整数毫秒（当前值: {rec[key]!r}）")


def _require_text(rec, key, label, errors):
    if not rec.get(key):
        errors.append(f"{label}不能为空")


def _validate_common(rec, int_fields, text_fields):
    errors = []
    for key, label in text_fields:
        _require_text(rec, key, label, errors)
    for key, label in int_fields:
        _to_int(rec, key, label, errors)
    return errors


def _parse(data, fields, text_fields, int_fields, check_range):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError(["文件不是有效的 UTF-8 编码"])
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise CsvError(["CSV 文件为空"])
    mapping = _map_headers(rows[0], fields)
    missing = [k for k in fields if k not in mapping]
    if missing:
        names = "、".join(fields[k][0] for k in missing)
        raise CsvError([f"缺少必需列: {names}"])

    errors, out = [], []
    for lineno, row in enumerate(rows[1:], start=2):
        rec = {}
        for key, idx in mapping.items():
            rec[key] = row[idx].strip() if idx < len(row) else ""
        row_errors = _validate_common(rec, int_fields, text_fields)
        if not row_errors:
            row_errors = check_range(rec)
        if row_errors:
            errors.extend(f"第{lineno}行: {e}" for e in row_errors)
        else:
            out.append(rec)
        if len(errors) >= MAX_ERRORS:
            errors.append("……错误过多，仅列出前部分")
            break
    if errors:
        raise CsvError(errors)
    return out


def parse_subtitles_csv(data):
    """解析素材字幕 CSV，返回 dict 列表（start_ms/end_ms 已为 int）。"""
    def check_range(rec):
        errs = []
        if rec["start_ms"] < 0 or rec["end_ms"] < 0:
            errs.append("起止毫秒不能为负")
        if rec["start_ms"] >= rec["end_ms"]:
            errs.append("开始毫秒必须小于结束毫秒（区间左闭右开）")
        return errs

    return _parse(
        data, SUBTITLE_FIELDS,
        text_fields=[("material_id", "素材号"), ("sentence_id", "句号"), ("text", "原文")],
        int_fields=[("start_ms", "开始毫秒"), ("end_ms", "结束毫秒")],
        check_range=check_range,
    )


def parse_edits_csv(data):
    """解析等速剪辑表 CSV，返回 dict 列表（各毫秒字段已为 int）。"""
    def check_range(rec):
        errs = []
        if rec["cut_start_ms"] < 0 or rec["cut_end_ms"] < 0 or rec["program_start_ms"] < 0:
            errs.append("毫秒字段不能为负")
        if rec["cut_start_ms"] >= rec["cut_end_ms"]:
            errs.append("截取开始毫秒必须小于截取结束毫秒（区间左闭右开）")
        return errs

    return _parse(
        data, EDIT_FIELDS,
        text_fields=[("program_id", "成片号"), ("segment_id", "剪辑段号"), ("material_id", "素材号")],
        int_fields=[("cut_start_ms", "截取开始毫秒"), ("cut_end_ms", "截取结束毫秒"),
                    ("program_start_ms", "成片起点毫秒")],
        check_range=check_range,
    )
