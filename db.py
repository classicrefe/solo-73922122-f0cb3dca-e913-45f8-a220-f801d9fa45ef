"""SQLite 连接管理与建表。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitles (
  material_id  TEXT NOT NULL,
  sentence_id  TEXT NOT NULL,
  start_ms     INTEGER NOT NULL,
  end_ms       INTEGER NOT NULL,
  text         TEXT NOT NULL,
  revision     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (material_id, sentence_id)
);

CREATE TABLE IF NOT EXISTS edits (
  program_id       TEXT NOT NULL,
  segment_id       TEXT NOT NULL,
  material_id      TEXT NOT NULL,
  cut_start_ms     INTEGER NOT NULL,
  cut_end_ms       INTEGER NOT NULL,
  program_start_ms INTEGER NOT NULL,
  PRIMARY KEY (program_id, segment_id)
);

-- 勘误单：出具后不可变，历史单据永久可查
CREATE TABLE IF NOT EXISTS errata (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id   TEXT NOT NULL,
  sentence_id   TEXT NOT NULL,
  base_revision INTEGER NOT NULL,          -- 基于哪一版原句出的单
  old_start_ms  INTEGER NOT NULL,
  old_end_ms    INTEGER NOT NULL,
  old_text      TEXT NOT NULL,
  new_start_ms  INTEGER NOT NULL,
  new_end_ms    INTEGER NOT NULL,
  new_text      TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS errata_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  errata_id    INTEGER NOT NULL REFERENCES errata(id),
  program_id   TEXT NOT NULL,
  segment_id   TEXT NOT NULL,
  action       TEXT NOT NULL CHECK (action IN ('delete', 'add', 'replace')),
  old_start_ms INTEGER,
  old_end_ms   INTEGER,
  new_start_ms INTEGER,
  new_end_ms   INTEGER,
  old_text     TEXT,
  new_text     TEXT
);

CREATE INDEX IF NOT EXISTS idx_edits_material ON edits(material_id);
CREATE INDEX IF NOT EXISTS idx_items_errata ON errata_items(errata_id);
CREATE INDEX IF NOT EXISTS idx_items_program ON errata_items(program_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()
