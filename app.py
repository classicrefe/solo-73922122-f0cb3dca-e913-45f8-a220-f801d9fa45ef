"""字幕勘误网页：素材字幕更正后，逐剪辑段生成成片时码勘误单。

工作流程：
  1. 上传素材字幕 CSV 与等速剪辑表 CSV；
  2. 在“素材字幕”页对某一句按所见修订文字或起止毫秒；
  3. 系统把更正前后与各剪辑段的重叠部分换算为成片时码（左闭右开），
     出具勘误单，逐段列出要删除 / 补加 / 替换的字幕及新旧时码；
  4. 原句更正与勘误单在同一事务中生效；失败或修订过期不出单；
     已出单据不可变，后续更正不影响历史单据，单据可随时查询。
"""
import os
import sqlite3

from flask import (Flask, abort, flash, redirect, render_template, request,
                   url_for)

import db as database
from core import compute_errata_items, fmt_tc, segment_sort_key
from csv_import import CsvError, parse_edits_csv, parse_subtitles_csv

ACTION_LABELS = {"delete": "删除", "add": "补加", "replace": "替换"}


def create_app(config=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = os.environ.get(
        "DB_PATH", os.path.join(app.instance_path, "errata.db"))
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    if config:
        app.config.update(config)

    db_dir = os.path.dirname(app.config["DB_PATH"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    app.teardown_appcontext(database.close_db)
    database.init_db(app)

    app.add_template_filter(fmt_tc, "fmt_tc")
    app.add_template_filter(lambda a: ACTION_LABELS.get(a, a), "action_label")

    # ---------------- 首页 ----------------
    @app.route("/")
    def index():
        db = database.get_db()
        counts = {
            "subtitles": db.execute("SELECT COUNT(*) c FROM subtitles").fetchone()["c"],
            "edits": db.execute("SELECT COUNT(*) c FROM edits").fetchone()["c"],
            "errata": db.execute("SELECT COUNT(*) c FROM errata").fetchone()["c"],
        }
        recent = db.execute(
            "SELECT * FROM errata ORDER BY id DESC LIMIT 5").fetchall()
        return render_template("index.html", counts=counts, recent=recent)

    # ---------------- CSV 上传 ----------------
    @app.route("/upload/subtitles", methods=["POST"])
    def upload_subtitles():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("请选择要上传的素材字幕 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            rows = parse_subtitles_csv(file.read())
        except CsvError as exc:
            for e in exc.errors:
                flash(f"素材字幕导入失败：{e}", "error")
            return redirect(url_for("index"))

        db = database.get_db()
        try:
            db.executemany(
                """
                INSERT INTO subtitles (material_id, sentence_id, start_ms, end_ms, text)
                VALUES (:material_id, :sentence_id, :start_ms, :end_ms, :text)
                ON CONFLICT(material_id, sentence_id) DO UPDATE SET
                  start_ms = excluded.start_ms,
                  end_ms   = excluded.end_ms,
                  text     = excluded.text,
                  revision = CASE
                    WHEN subtitles.start_ms = excluded.start_ms
                     AND subtitles.end_ms   = excluded.end_ms
                     AND subtitles.text     = excluded.text
                    THEN subtitles.revision ELSE 0 END
                """,
                rows,
            )
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            flash(f"素材字幕导入失败：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"素材字幕导入成功，共 {len(rows)} 句", "success")
        return redirect(url_for("subtitles"))

    @app.route("/upload/edits", methods=["POST"])
    def upload_edits():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("请选择要上传的剪辑表 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            rows = parse_edits_csv(file.read())
        except CsvError as exc:
            for e in exc.errors:
                flash(f"剪辑表导入失败：{e}", "error")
            return redirect(url_for("index"))

        db = database.get_db()
        try:
            db.executemany(
                """
                INSERT OR REPLACE INTO edits
                  (program_id, segment_id, material_id,
                   cut_start_ms, cut_end_ms, program_start_ms)
                VALUES (:program_id, :segment_id, :material_id,
                        :cut_start_ms, :cut_end_ms, :program_start_ms)
                """,
                rows,
            )
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            flash(f"剪辑表导入失败：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"剪辑表导入成功，共 {len(rows)} 段", "success")
        return redirect(url_for("edits"))

    # ---------------- 浏览 ----------------
    @app.route("/subtitles")
    def subtitles():
        material = request.args.get("material_id", "").strip()
        db = database.get_db()
        if material:
            rows = db.execute(
                "SELECT * FROM subtitles WHERE material_id=? "
                "ORDER BY material_id, start_ms, sentence_id", (material,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM subtitles "
                "ORDER BY material_id, start_ms, sentence_id LIMIT 500").fetchall()
        return render_template("subtitles.html", rows=rows, material=material)

    @app.route("/edits")
    def edits():
        rows = database.get_db().execute("SELECT * FROM edits").fetchall()
        rows = sorted(rows, key=segment_sort_key)
        return render_template("edits.html", rows=rows)

    # ---------------- 修订与出单 ----------------
    @app.route("/revise/<material_id>/<sentence_id>")
    def revise_form(material_id, sentence_id):
        row = database.get_db().execute(
            "SELECT * FROM subtitles WHERE material_id=? AND sentence_id=?",
            (material_id, sentence_id)).fetchone()
        if row is None:
            abort(404)
        return render_template("revise.html", s=row)

    @app.route("/revise/<material_id>/<sentence_id>", methods=["POST"])
    def revise_submit(material_id, sentence_id):
        db = database.get_db()
        back = url_for("revise_form", material_id=material_id, sentence_id=sentence_id)
        row = db.execute(
            "SELECT * FROM subtitles WHERE material_id=? AND sentence_id=?",
            (material_id, sentence_id)).fetchone()
        if row is None:
            flash("未找到该句字幕，可能已被重新上传", "error")
            return redirect(url_for("subtitles"))

        # ---- 输入校验：失败则不出单 ----
        try:
            base_revision = int(request.form.get("base_revision", ""))
            new_start = int(request.form.get("start_ms", ""))
            new_end = int(request.form.get("end_ms", ""))
        except ValueError:
            flash("输入格式错误：版本号与起止毫秒必须为整数", "error")
            return redirect(back)
        new_text = request.form.get("text", "").strip()

        errors = []
        if not new_text:
            errors.append("原文不能为空")
        if new_start < 0 or new_end < 0:
            errors.append("起止毫秒不能为负")
        if new_start >= new_end:
            errors.append("开始毫秒必须小于结束毫秒（区间左闭右开）")
        if base_revision != row["revision"]:
            errors.append(
                f"修订过期：你基于第 {base_revision} 版，"
                f"当前已是第 {row['revision']} 版，请刷新页面后重新修订")
        if not errors and (new_start, new_end, new_text) == \
                (row["start_ms"], row["end_ms"], row["text"]):
            errors.append("未做任何修改，不出具勘误单")
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(back)

        # ---- 计算各剪辑段勘误条目 ----
        segments = db.execute(
            "SELECT * FROM edits WHERE material_id=?", (material_id,)).fetchall()
        segments = sorted(segments, key=segment_sort_key)
        items = compute_errata_items(
            segments, dict(row),
            {"start_ms": new_start, "end_ms": new_end, "text": new_text})

        # ---- 原句更正与勘误单在同一事务生效 ----
        try:
            cur = db.execute(
                "UPDATE subtitles SET start_ms=?, end_ms=?, text=?, revision=revision+1 "
                "WHERE material_id=? AND sentence_id=? AND revision=?",
                (new_start, new_end, new_text, material_id, sentence_id, base_revision))
            if cur.rowcount != 1:
                db.rollback()
                flash("修订过期：该句刚被他人更新，请刷新后重试", "error")
                return redirect(back)
            cur = db.execute(
                "INSERT INTO errata (material_id, sentence_id, base_revision, "
                " old_start_ms, old_end_ms, old_text, new_start_ms, new_end_ms, new_text) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (material_id, sentence_id, base_revision,
                 row["start_ms"], row["end_ms"], row["text"],
                 new_start, new_end, new_text))
            errata_id = cur.lastrowid
            db.executemany(
                "INSERT INTO errata_items (errata_id, program_id, segment_id, action, "
                " old_start_ms, old_end_ms, new_start_ms, new_end_ms, old_text, new_text) "
                "VALUES (:eid, :program_id, :segment_id, :action, "
                " :old_start_ms, :old_end_ms, :new_start_ms, :new_end_ms, :old_text, :new_text)",
                [dict(it, eid=errata_id) for it in items])
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            flash(f"出具勘误单失败，原句未改动：{exc}", "error")
            return redirect(back)

        flash(f"勘误单 #{errata_id} 已出具，原句已同步更正（第 {base_revision + 1} 版）", "success")
        return redirect(url_for("errata_detail", errata_id=errata_id))

    # ---------------- 勘误单查询 ----------------
    @app.route("/errata")
    def errata_list():
        f_material = request.args.get("material_id", "").strip()
        f_sentence = request.args.get("sentence_id", "").strip()
        f_program = request.args.get("program_id", "").strip()
        sql = (
            "SELECT e.*, "
            " (SELECT COUNT(*) FROM errata_items i WHERE i.errata_id=e.id) AS item_count "
            "FROM errata e "
            "WHERE (?='' OR e.material_id=?) "
            "  AND (?='' OR e.sentence_id=?) "
            "  AND (?='' OR EXISTS (SELECT 1 FROM errata_items i "
            "                       WHERE i.errata_id=e.id AND i.program_id=?)) "
            "ORDER BY e.id DESC")
        rows = database.get_db().execute(
            sql, (f_material, f_material, f_sentence, f_sentence,
                  f_program, f_program)).fetchall()
        return render_template("errata_list.html", rows=rows,
                               f_material=f_material, f_sentence=f_sentence,
                               f_program=f_program)

    @app.route("/errata/<int:errata_id>")
    def errata_detail(errata_id):
        db = database.get_db()
        err = db.execute("SELECT * FROM errata WHERE id=?", (errata_id,)).fetchone()
        if err is None:
            abort(404)
        items = db.execute(
            "SELECT * FROM errata_items WHERE errata_id=?", (errata_id,)).fetchall()
        items = sorted(items, key=segment_sort_key)
        return render_template("errata_detail.html", e=err, items=items)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
