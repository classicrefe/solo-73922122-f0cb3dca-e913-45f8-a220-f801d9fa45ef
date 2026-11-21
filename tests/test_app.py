# -*- coding: utf-8 -*-
"""关键功能自测：重叠换算、勘误出单、失败/过期不出单、单据不可变。"""
import io
import sqlite3

import pytest

from app import create_app
from core import compute_errata_items, fmt_tc, overlap

SUBTITLES_CSV = """素材号,句号,开始毫秒,结束毫秒,原文
M1,S1,1000,5000,你好世界
M1,S2,12000,14000,再见
"""

EDITS_CSV = """成片号,剪辑段号,素材号,截取开始毫秒,截取结束毫秒,成片起点毫秒
P1,1,M1,0,4000,60000
P1,2,M1,4000,9000,70000
P2,1,M1,2000,6000,10000
P3,1,M1,8000,12000,0
P4,1,M1,7000,9000,300000
P5,1,M1,14000,16000,0
"""


@pytest.fixture
def client(tmp_path):
    app = create_app({
        "DB_PATH": str(tmp_path / "test.db"),
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    with app.test_client() as c:
        yield c
    return app


def db_rows(app, sql, args=()):
    con = sqlite3.connect(app.config["DB_PATH"])
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def upload(client, url, csv_text):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), "data.csv")}
    return client.post(url, data=data,
                       content_type="multipart/form-data",
                       follow_redirects=True)


@pytest.fixture
def loaded(client):
    r1 = upload(client, "/upload/subtitles", SUBTITLES_CSV)
    assert "导入成功" in r1.get_data(as_text=True)
    r2 = upload(client, "/upload/edits", EDITS_CSV)
    assert "导入成功" in r2.get_data(as_text=True)
    return client


def revise(client, material, sentence, base, start, end, text):
    return client.post(
        f"/revise/{material}/{sentence}",
        data={"base_revision": str(base), "start_ms": str(start),
              "end_ms": str(end), "text": text},
        follow_redirects=True)


# ---------------- 单元：区间与时码 ----------------

def test_overlap_left_closed_right_open():
    assert overlap(0, 1000, 1000, 2000) is None      # 端点相接不算重叠
    assert overlap(0, 1000, 999, 2000) == (999, 1000)
    assert overlap(1000, 5000, 2000, 3000) == (2000, 3000)  # 包含
    assert overlap(2000, 3000, 0, 9000) == (2000, 3000)     # 被包含
    assert overlap(0, 1000, 2000, 3000) is None
    assert overlap(1000, 5000, 1000, 5000) == (1000, 5000)


def test_fmt_tc():
    assert fmt_tc(0) == "00:00:00.000"
    assert fmt_tc(61000) == "00:01:01.000"
    assert fmt_tc(70500) == "00:01:10.500"
    assert fmt_tc(3600000 + 2000 + 3) == "01:00:02.003"
    assert fmt_tc(None) == ""


def test_compute_items_no_visible_change_skipped():
    # 区间换算后完全相同且文字未变 -> 该段不产生条目
    segs = [{"program_id": "P", "segment_id": "1",
             "cut_start_ms": 0, "cut_end_ms": 5000, "program_start_ms": 100}]
    old = {"start_ms": 1000, "end_ms": 9000, "text": "x"}   # 旧重叠 [1000,5000)
    new = {"start_ms": 1000, "end_ms": 12000, "text": "x"}  # 新重叠仍是 [1000,5000)
    assert compute_errata_items(segs, old, new) == []


# ---------------- 上传校验 ----------------

def test_upload_bad_csv_rejected_atomically(client):
    bad = "素材号,句号,开始毫秒,结束毫秒,原文\nM1,S1,5000,1000,起止颠倒\n"
    r = upload(client, "/upload/subtitles", bad)
    body = r.get_data(as_text=True)
    assert "导入失败" in body and "开始毫秒必须小于结束毫秒" in body
    assert db_rows(client.application, "SELECT * FROM subtitles") == []


def test_upload_missing_column_rejected(client):
    bad = "素材号,句号,开始毫秒,原文\nM1,S1,1000,你好\n"
    r = upload(client, "/upload/subtitles", bad)
    assert "缺少必需列" in r.get_data(as_text=True)


def test_reupload_resets_revision_only_when_changed(loaded):
    app = loaded.application
    revise(loaded, "M1", "S1", 0, 4500, 9500, "你好，世界")
    # 原样重传：内容未变的句目版本号保留（S1 已是更正后的值，与 CSV 不同 -> 归零；
    # S2 与 CSV 一致 -> 保持 0）
    upload(loaded, "/upload/subtitles", SUBTITLES_CSV)
    s1 = db_rows(app, "SELECT * FROM subtitles WHERE sentence_id='S1'")[0]
    assert s1["revision"] == 0 and s1["start_ms"] == 1000  # 被 CSV 覆盖且版本归零
    # 再次原样重传：内容一致，版本号不再变动
    upload(loaded, "/upload/subtitles", SUBTITLES_CSV)
    assert db_rows(app, "SELECT revision FROM subtitles WHERE sentence_id='S1'")[0][0] == 0


# ---------------- 出单：删除 / 替换 / 补加 ----------------

def test_revise_generates_delete_replace_add(loaded):
    app = loaded.application
    r = revise(loaded, "M1", "S1", base=0, start=4500, end=9500, text="你好，世界")
    body = r.get_data(as_text=True)
    assert "勘误单 #1 已出具" in body

    items = db_rows(app, "SELECT * FROM errata_items ORDER BY program_id, segment_id")
    assert len(items) == 5

    def item(prog, seg):
        return next(i for i in items
                    if i["program_id"] == prog and i["segment_id"] == seg)

    # P1/1 截取 [0,4000)：旧 [1000,4000)->[61000,64000)，新 [4500,9500) 无重叠 -> 删除
    it = item("P1", "1")
    assert it["action"] == "delete"
    assert (it["old_start_ms"], it["old_end_ms"]) == (61000, 64000)
    assert it["new_start_ms"] is None and it["old_text"] == "你好世界"

    # P1/2 截取 [4000,9000) 起点 70000：旧 [4000,5000)->[70000,71000)，
    # 新 [4500,9000)->[70500,75000) -> 替换
    it = item("P1", "2")
    assert it["action"] == "replace"
    assert (it["old_start_ms"], it["old_end_ms"]) == (70000, 71000)
    assert (it["new_start_ms"], it["new_end_ms"]) == (70500, 75000)
    assert it["old_text"] == "你好世界" and it["new_text"] == "你好，世界"

    # P2/1 截取 [2000,6000) 起点 10000：旧 [2000,5000)->[10000,13000)，
    # 新 [4500,6000)->[12500,14000) -> 替换
    it = item("P2", "1")
    assert it["action"] == "replace"
    assert (it["old_start_ms"], it["old_end_ms"]) == (10000, 13000)
    assert (it["new_start_ms"], it["new_end_ms"]) == (12500, 14000)

    # P3/1 截取 [8000,12000) 起点 0：旧无重叠，新 [8000,9500)->[0,1500) -> 补加
    it = item("P3", "1")
    assert it["action"] == "add"
    assert it["old_start_ms"] is None
    assert (it["new_start_ms"], it["new_end_ms"]) == (0, 1500)
    assert it["new_text"] == "你好，世界"

    # P4/1 截取 [7000,9000) 起点 300000：旧无重叠，新 [7000,9000)->[300000,302000) -> 补加
    it = item("P4", "1")
    assert it["action"] == "add"
    assert it["old_start_ms"] is None
    assert (it["new_start_ms"], it["new_end_ms"]) == (300000, 302000)

    # 原句已同步更正（与单据一并生效）
    s = db_rows(app, "SELECT * FROM subtitles WHERE material_id='M1' AND sentence_id='S1'")[0]
    assert (s["start_ms"], s["end_ms"], s["text"], s["revision"]) == (4500, 9500, "你好，世界", 1)

    # 详情页展示成片时码与操作
    detail = loaded.get("/errata/1").get_data(as_text=True)
    for tc in ("00:01:01.000", "00:01:04.000", "00:01:10.500", "00:01:15.000",
               "00:00:12.500", "00:00:14.000", "00:00:00.000", "00:00:01.500"):
        assert tc in detail
    for label in ("删除", "替换", "补加"):
        assert label in detail


def test_text_only_change_replace_same_timecode(loaded):
    app = loaded.application
    r = revise(loaded, "M1", "S1", base=0, start=1000, end=5000, text="你好，世界")
    assert "勘误单 #1 已出具" in r.get_data(as_text=True)
    items = db_rows(app, "SELECT * FROM errata_items")
    # P1/1、P1/2、P2/1 三段有重叠；P3/1、P4/1 与 [1000,5000) 无重叠
    assert len(items) == 3
    for it in items:
        assert it["action"] == "replace"
        assert it["old_start_ms"] == it["new_start_ms"]
        assert it["old_end_ms"] == it["new_end_ms"]


def test_boundary_touching_segments_produce_no_items(loaded):
    # S2 [12000,14000)：与 P3/1 截取 [8000,12000) 在 12000 处端点相接、
    # 与 P5/1 [14000,16000) 在 14000 处端点相接；左闭右开下均无重叠 -> 出单但无条目
    app = loaded.application
    r = revise(loaded, "M1", "S2", base=0, start=12000, end=14000, text="再会")
    assert "勘误单 #1 已出具" in r.get_data(as_text=True)
    assert db_rows(app, "SELECT * FROM errata_items") == []
    assert "不涉及任何已成片剪辑段" in loaded.get("/errata/1").get_data(as_text=True)


# ---------------- 失败 / 修订过期：不出单 ----------------

def test_stale_revision_no_new_errata(loaded):
    app = loaded.application
    assert "勘误单 #1" in revise(loaded, "M1", "S1", 0, 4500, 9500, "你好，世界").get_data(as_text=True)
    # 再用旧版本号提交 -> 修订过期
    r = revise(loaded, "M1", "S1", 0, 5000, 9600, "又一次")
    body = r.get_data(as_text=True)
    assert "修订过期" in body
    assert db_rows(app, "SELECT * FROM errata") .__len__() == 1
    s = db_rows(app, "SELECT * FROM subtitles WHERE sentence_id='S1'")[0]
    assert (s["start_ms"], s["end_ms"], s["text"], s["revision"]) == (4500, 9500, "你好，世界", 1)


def test_invalid_input_no_new_errata(loaded):
    app = loaded.application
    r = revise(loaded, "M1", "S1", 0, 5000, 5000, "起止相等")
    assert "开始毫秒必须小于结束毫秒" in r.get_data(as_text=True)
    r = revise(loaded, "M1", "S1", 0, 1000, 5000, "   ")
    assert "原文不能为空" in r.get_data(as_text=True)
    r = revise(loaded, "M1", "S1", 0, 1000, 5000, "你好世界")  # 未做任何修改
    assert "未做任何修改" in r.get_data(as_text=True)
    assert db_rows(app, "SELECT * FROM errata") == []
    s = db_rows(app, "SELECT * FROM subtitles WHERE sentence_id='S1'")[0]
    assert s["revision"] == 0


def test_missing_sentence_no_new_errata(loaded):
    app = loaded.application
    r = loaded.post("/revise/M1/NOPE",
                    data={"base_revision": "0", "start_ms": "1",
                          "end_ms": "2", "text": "x"},
                    follow_redirects=True)
    assert "未找到该句字幕" in r.get_data(as_text=True)
    assert db_rows(app, "SELECT * FROM errata") == []


# ---------------- 单据不可变、历史可查 ----------------

def test_issued_errata_immutable_after_later_revisions(loaded):
    app = loaded.application
    revise(loaded, "M1", "S1", 0, 4500, 9500, "你好，世界")
    before = [dict(r) for r in db_rows(app, "SELECT * FROM errata_items WHERE errata_id=1")]
    # 第二次更正（基于新版）出具新单
    r = revise(loaded, "M1", "S1", 1, 4600, 9600, "你好啊世界")
    assert "勘误单 #2 已出具" in r.get_data(as_text=True)
    after = [dict(r) for r in db_rows(app, "SELECT * FROM errata_items WHERE errata_id=1")]
    assert before == after  # 旧单条目不变
    # 两单均可查询
    assert "勘误单 #1" in loaded.get("/errata/1").get_data(as_text=True)
    assert "勘误单 #2" in loaded.get("/errata/2").get_data(as_text=True)
    listing = loaded.get("/errata?program_id=P3").get_data(as_text=True)
    assert "<td>#1</td>" in listing  # P3 只在第一单中出现
    e2 = db_rows(app, "SELECT * FROM errata WHERE id=2")[0]
    assert e2["base_revision"] == 1 and e2["old_start_ms"] == 4500


def test_errata_list_filters(loaded):
    revise(loaded, "M1", "S1", 0, 4500, 9500, "你好，世界")
    body = loaded.get("/errata?material_id=M1&sentence_id=S1").get_data(as_text=True)
    assert "<td>#1</td>" in body
    body = loaded.get("/errata?program_id=P1").get_data(as_text=True)
    assert "<td>#1</td>" in body
    body = loaded.get("/errata?program_id=NOPE").get_data(as_text=True)
    assert "<td>#1</td>" not in body
