#!/usr/bin/env python3
"""
UltraClean Tracker - Flask RESTful API Backend
Deploy to Render: gunicorn app:app
"""

import os
import io
import csv
import shutil
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, g, send_file
from flask_cors import CORS

# ── App Config ──────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

APP_NAME = "UltraClean Tracker API"
APP_VERSION = "3.0"

# Database path – macOS local default, env override for Render
_DEFAULT_DB_DIR = os.path.join(str(Path.home()), "Library", "Application Support", "UltraCleanTracker")
DB_DIR = os.environ.get("ULTRACLEAN_DB_DIR", _DEFAULT_DB_DIR)
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "video_tracker.db")

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

STATUSES = [
    "素材歸檔", "已停止", "剪輯中", "MKT審核中", "MKT退回",
    "老闆審核中", "老闆退回", "審核通過",
    "文案撰寫中", "準備排程", "已排程", "已發布",
]

CATEGORIES = {
    "hour_clean": "Hour Clean",
    "pro_clean": "Pro Clean",
    "maint_clean": "Maint Clean",
    "aircon_care": "Aircon Care",
    "hygiene": "Hygiene",
    "pool_care": "Pool Care",
    "pest_care": "Pest Care",
    "home_care": "Home Care",
    "handy_care": "Handy Care",
    "academy": "Academy",
    "agency": "Agency",
    "garden_care": "Garden Care",
    "event": "Event",
    "ghc": "GHC",
    "daily_life_memes": "Daily Life Memes",
    "apss": "APSS",
}

ALL_CATEGORY_LABELS = list(CATEGORIES.values())

STATUS_COLORS = {
    "素材歸檔": "#6c757d",
    "已停止": "#e83e8c",
    "剪輯中": "#fd7e14",
    "MKT審核中": "#0d6efd",
    "MKT退回": "#dc3545",
    "老闆審核中": "#6f42c1",
    "老闆退回": "#b02a37",
    "審核通過": "#198754",
    "文案撰寫中": "#20c997",
    "準備排程": "#ffc107",
    "已排程": "#0dcaf0",
    "已發布": "#146c43",
}


# ── Database ─────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    current_ver = db.execute("PRAGMA user_version").fetchone()[0]

    if current_ver == 0:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'hour_clean',
                current_status TEXT NOT NULL DEFAULT '素材歸檔',
                note TEXT DEFAULT '',
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                edit_date TEXT DEFAULT NULL,
                review_date TEXT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS status_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                note TEXT DEFAULT '',
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_projects_category ON projects(category);
            CREATE INDEX IF NOT EXISTS idx_projects_current_status ON projects(current_status);
            CREATE INDEX IF NOT EXISTS idx_projects_deleted ON projects(deleted);
            CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at);
            CREATE INDEX IF NOT EXISTS idx_status_logs_project_id ON status_logs(project_id);
            CREATE INDEX IF NOT EXISTS idx_status_logs_changed_at ON status_logs(changed_at);
            CREATE INDEX IF NOT EXISTS idx_status_logs_new_status ON status_logs(new_status);
        """)
        db.execute("PRAGMA user_version = 1")

    db.commit()
    db.close()


# Init DB on startup
with app.app_context():
    init_db()


# ── Helpers ───────────────────────────────────────────────────
def row_to_dict(row):
    return dict(row) if row else None


def now_str():
    return datetime.now().strftime(DATETIME_FMT)


def today_str():
    return date.today().strftime(DATE_FMT)


# ── API: Config ───────────────────────────────────────────────
@app.route("/api/config")
def api_config():
    return jsonify({
        "statuses": STATUSES,
        "categories": CATEGORIES,
        "category_labels": ALL_CATEGORY_LABELS,
        "status_colors": STATUS_COLORS,
    })


# ── API: Projects CRUD ────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def api_projects_list():
    db = get_db()

    # Filters
    status_filter = request.args.get("status", "")
    cat_filter = request.args.get("category", "")
    search = request.args.get("search", "")
    deleted = int(request.args.get("deleted", "0"))

    # Sort
    sort_col = request.args.get("sort_col", "updated_at")
    sort_asc = request.args.get("sort_asc", "false").lower() == "true"
    allowed_sorts = {"id", "name", "category", "current_status", "note", "updated_at", "edit_date", "review_date"}
    if sort_col not in allowed_sorts:
        sort_col = "updated_at"

    # Pagination
    page = int(request.args.get("page", "0"))
    limit = int(request.args.get("limit", "200"))

    sql = f"SELECT * FROM projects WHERE deleted={deleted}"
    params = []

    if status_filter:
        sql += " AND current_status=?"
        params.append(status_filter)
    if cat_filter:
        cat_map = {v: k for k, v in CATEGORIES.items()}
        cat_key = cat_map.get(cat_filter)
        if cat_key:
            sql += " AND category=?"
            params.append(cat_key)
    if search:
        sql += " AND name LIKE ?"
        params.append(f"%{search}%")

    # Count
    count_sql = sql.replace("SELECT *", "SELECT COUNT(*)")
    total = db.execute(count_sql, params).fetchone()[0]

    order_dir = "ASC" if sort_asc else "DESC"
    sql += f" ORDER BY {sort_col} {order_dir}"
    sql += f" LIMIT {limit} OFFSET {page * limit}"

    rows = db.execute(sql, params).fetchall()

    return jsonify({
        "projects": [row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    })


@app.route("/api/projects", methods=["POST"])
def api_project_create():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    cat = data.get("category", "pest_care")
    if cat not in CATEGORIES:
        cat = "pest_care"
    note = data.get("note", "").strip()
    now = now_str()

    db = get_db()
    cur = db.execute(
        "INSERT INTO projects (name, category, current_status, note, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (name, cat, "素材歸檔", note, now, now)
    )
    pid = cur.lastrowid
    db.execute(
        "INSERT INTO status_logs (project_id, old_status, new_status, changed_at) VALUES (?,NULL,'素材歸檔',?)",
        (pid, now)
    )
    db.commit()

    return jsonify({"id": pid, "name": name}), 201


@app.route("/api/projects/<int:pid>", methods=["PUT"])
def api_project_update(pid):
    data = request.get_json()
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404

    name = data.get("name", proj["name"]).strip()
    note = data.get("note", proj["note"])
    now = now_str()
    db.execute(
        "UPDATE projects SET name=?, note=?, updated_at=? WHERE id=? AND deleted=0",
        (name, note, now, pid)
    )
    db.commit()
    return jsonify({"id": pid, "name": name})


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
def api_project_delete(pid):
    """Soft delete: move to trash"""
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404

    now = now_str()
    db.execute("UPDATE projects SET deleted=1, updated_at=? WHERE id=?", (now, pid))
    db.execute(
        "INSERT INTO status_logs (project_id, old_status, new_status, changed_at, note) VALUES (?,?,?,?,?)",
        (pid, proj["current_status"], "已刪除", now, "移至垃圾桶")
    )
    db.commit()
    return jsonify({"id": pid, "deleted": True})


@app.route("/api/projects/<int:pid>/status", methods=["PUT"])
def api_project_update_status(pid):
    data = request.get_json()
    new_status = data.get("status", "")
    if new_status not in STATUSES:
        return jsonify({"error": "invalid status"}), 400

    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404

    old_status = proj["current_status"]
    note = data.get("note", "").strip()
    now = now_str()

    db.execute("UPDATE projects SET current_status=?, updated_at=?, note=? WHERE id=? AND deleted=0",
               (new_status, now, note, pid))
    db.execute(
        "INSERT INTO status_logs (project_id, old_status, new_status, changed_at, note) VALUES (?,?,?,?,?)",
        (pid, old_status, new_status, now, note)
    )
    db.commit()
    return jsonify({"id": pid, "old_status": old_status, "new_status": new_status})


@app.route("/api/projects/<int:pid>", methods=["PATCH"])
def api_project_patch(pid):
    """Partial update: edit_date, review_date, updated_at"""
    data = request.get_json()
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404

    allowed = {"edit_date", "review_date", "updated_at"}
    for field, value in data.items():
        if field in allowed:
            db.execute(f"UPDATE projects SET {field}=? WHERE id=? AND deleted=0", (value, pid))
    db.commit()
    return jsonify({"id": pid, "updated": list(data.keys())})


@app.route("/api/projects/<int:pid>/logs", methods=["GET"])
def api_project_logs(pid):
    db = get_db()
    logs = db.execute(
        "SELECT * FROM status_logs WHERE project_id=? ORDER BY changed_at ASC",
        (pid,)
    ).fetchall()
    return jsonify({"logs": [row_to_dict(r) for r in logs]})


# ── Batch Operations ──────────────────────────────────────────
@app.route("/api/projects/batch/status", methods=["POST"])
def api_batch_update_status():
    data = request.get_json()
    ids = data.get("ids", [])
    new_status = data.get("status", "")
    if not ids or new_status not in STATUSES:
        return jsonify({"error": "ids and valid status required"}), 400

    db = get_db()
    now = now_str()
    updated = 0
    for pid in ids:
        proj = db.execute("SELECT current_status FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
        if not proj:
            continue
        old = proj["current_status"]
        db.execute("UPDATE projects SET current_status=?, updated_at=? WHERE id=? AND deleted=0",
                   (new_status, now, pid))
        db.execute("INSERT INTO status_logs (project_id, old_status, new_status, changed_at) VALUES (?,?,?,?)",
                   (pid, old, new_status, now))
        updated += 1
    db.commit()
    return jsonify({"updated": updated})


@app.route("/api/projects/batch/delete", methods=["POST"])
def api_batch_delete():
    data = request.get_json()
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400

    db = get_db()
    now = now_str()
    deleted = 0
    for pid in ids:
        proj = db.execute("SELECT current_status FROM projects WHERE id=? AND deleted=0", (pid,)).fetchone()
        if not proj:
            continue
        db.execute("UPDATE projects SET deleted=1, updated_at=? WHERE id=?", (now, pid))
        db.execute(
            "INSERT INTO status_logs (project_id, old_status, new_status, changed_at, note) VALUES (?,?,?,?,?)",
            (pid, proj["current_status"], "已刪除", now, "移至垃圾桶")
        )
        deleted += 1
    db.commit()
    return jsonify({"deleted": deleted})


@app.route("/api/projects/batch/date/<field>", methods=["POST"])
def api_batch_update_date(field):
    if field not in ("edit_date", "review_date", "updated_at"):
        return jsonify({"error": "invalid field"}), 400

    data = request.get_json()
    ids = data.get("ids", [])
    date_val = data.get("date", None)
    if not ids:
        return jsonify({"error": "ids required"}), 400

    db = get_db()
    now = now_str()
    updated = 0
    for pid in ids:
        if field == "updated_at":
            db.execute("UPDATE projects SET updated_at=? WHERE id=? AND deleted=0",
                       (date_val or now, pid))
        else:
            db.execute(f"UPDATE projects SET {field}=? WHERE id=? AND deleted=0",
                       (date_val or None, pid))
        updated += 1
    db.commit()
    return jsonify({"updated": updated})


# ── Trash ─────────────────────────────────────────────────────
@app.route("/api/trash", methods=["GET"])
def api_trash_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM projects WHERE deleted=1 ORDER BY updated_at DESC LIMIT 200"
    ).fetchall()
    return jsonify({"projects": [row_to_dict(r) for r in rows]})


@app.route("/api/trash/restore", methods=["POST"])
def api_trash_restore():
    data = request.get_json()
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400

    db = get_db()
    now = now_str()
    restored = 0
    for pid in ids:
        proj = db.execute("SELECT current_status FROM projects WHERE id=? AND deleted=1", (pid,)).fetchone()
        if not proj:
            continue
        db.execute("UPDATE projects SET deleted=0, updated_at=? WHERE id=?", (now, pid))
        db.execute(
            "INSERT INTO status_logs (project_id, old_status, new_status, changed_at, note) VALUES (?,?,?,?,?)",
            (pid, "已刪除", proj["current_status"], now, "從垃圾桶恢復")
        )
        restored += 1
    db.commit()
    return jsonify({"restored": restored})


@app.route("/api/trash/purge", methods=["POST"])
def api_trash_purge():
    data = request.get_json()
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400

    db = get_db()
    purged = 0
    for pid in ids:
        db.execute("DELETE FROM status_logs WHERE project_id=?", (pid,))
        db.execute("DELETE FROM projects WHERE id=? AND deleted=1", (pid,))
        purged += 1
    db.commit()
    return jsonify({"purged": purged})


@app.route("/api/trash/clear", methods=["POST"])
def api_trash_clear():
    db = get_db()
    deleted_ids = db.execute("SELECT id FROM projects WHERE deleted=1").fetchall()
    count = len(deleted_ids)
    for r in deleted_ids:
        db.execute("DELETE FROM status_logs WHERE project_id=?", (r["id"],))
    db.execute("DELETE FROM projects WHERE deleted=1")
    db.commit()
    return jsonify({"purged": count})


# ── Reports: Daily ────────────────────────────────────────────
@app.route("/api/reports/daily", methods=["GET"])
def api_daily_report():
    today = date.today()
    today_s = today.strftime(DATE_FMT)
    day_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    day_name = day_names[today.weekday()]

    start = today - timedelta(days=2) if today.weekday() == 0 else today
    end = today

    db = get_db()

    editing_today = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND edit_date=? ORDER BY updated_at DESC",
        (today_s,)
    ).fetchall()]

    review_today = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND review_date=? ORDER BY updated_at DESC",
        (today_s,)
    ).fetchall()]

    in_review = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status IN ('MKT審核中','老闆審核中') ORDER BY updated_at DESC"
    ).fetchall()]

    copywriting = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='文案撰寫中' ORDER BY updated_at DESC"
    ).fetchall()]

    ready = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='準備排程' ORDER BY updated_at DESC"
    ).fetchall()]

    scheduled = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='已排程' ORDER BY updated_at DESC"
    ).fetchall()]

    published = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='已發布' AND date(updated_at) BETWEEN ? AND ? ORDER BY updated_at DESC",
        (start.strftime(DATE_FMT), end.strftime(DATE_FMT))
    ).fetchall()]

    todo = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status IN ('素材歸檔','剪輯中','MKT退回','老闆退回') ORDER BY updated_at DESC"
    ).fetchall()]

    return jsonify({
        "date": today_s,
        "day_name": day_name,
        "range_start": start.strftime(DATE_FMT),
        "range_end": end.strftime(DATE_FMT),
        "editing_today": editing_today,
        "review_today": review_today,
        "in_review": in_review,
        "copywriting": copywriting,
        "ready": ready,
        "scheduled": scheduled,
        "published": published,
        "todo": todo,
    })


# ── Reports: Weekly ───────────────────────────────────────────
@app.route("/api/reports/weekly", methods=["GET"])
def api_weekly_report():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    monday_s = monday.strftime(DATE_FMT)
    sunday_s = sunday.strftime(DATE_FMT)

    db = get_db()

    editing_week = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND edit_date BETWEEN ? AND ? ORDER BY edit_date DESC",
        (monday_s, sunday_s)
    ).fetchall()]

    review_week = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND review_date BETWEEN ? AND ? ORDER BY review_date DESC",
        (monday_s, sunday_s)
    ).fetchall()]

    active = [row_to_dict(r) for r in db.execute(
        """SELECT DISTINCT p.* FROM projects p
           INNER JOIN status_logs sl ON sl.project_id = p.id
           WHERE p.deleted=0 AND date(sl.changed_at) BETWEEN ? AND ?
           AND sl.new_status NOT IN ('審核通過')
           ORDER BY p.updated_at DESC""",
        (monday_s, sunday_s)
    ).fetchall()]

    published = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='已發布' AND date(updated_at) BETWEEN ? AND ? ORDER BY updated_at DESC",
        (monday_s, sunday_s)
    ).fetchall()]

    in_review = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status IN ('MKT審核中','老闆審核中') ORDER BY updated_at DESC"
    ).fetchall()]

    pending = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status IN ('素材歸檔','剪輯中','MKT退回','老闆退回') ORDER BY updated_at DESC"
    ).fetchall()]

    return jsonify({
        "monday": monday_s,
        "sunday": sunday_s,
        "today": today.strftime(DATE_FMT),
        "editing_week": editing_week,
        "review_week": review_week,
        "active": active,
        "published": published,
        "in_review": in_review,
        "pending": pending,
    })


# ── Dashboard ─────────────────────────────────────────────────
@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    db = get_db()

    stats = []
    for st in STATUSES:
        cnt = db.execute("SELECT COUNT(*) FROM projects WHERE deleted=0 AND current_status=?", (st,)).fetchone()[0]
        stats.append({"status": st, "count": cnt, "color": STATUS_COLORS.get(st, "#fff")})

    total = db.execute("SELECT COUNT(*) FROM projects WHERE deleted=0").fetchone()[0]
    active_ct = db.execute(
        "SELECT COUNT(*) FROM projects WHERE deleted=0 AND current_status != '已發布'"
    ).fetchone()[0]
    trash_ct = db.execute("SELECT COUNT(*) FROM projects WHERE deleted=1").fetchone()[0]

    cat_stats = []
    for key, label in CATEGORIES.items():
        cnt = db.execute("SELECT COUNT(*) FROM projects WHERE deleted=0 AND category=?", (key,)).fetchone()[0]
        if cnt > 0:
            cat_stats.append({"key": key, "label": label, "count": cnt})

    return jsonify({
        "total": total,
        "active": active_ct,
        "trash": trash_ct,
        "category_count": len(cat_stats),
        "status_stats": stats,
        "category_stats": cat_stats,
    })


# ── Stats / Report Center ─────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def api_stats():
    period = request.args.get("period", "monthly")
    idx = int(request.args.get("idx", "0"))
    now = date.today()

    if period == "daily":
        d = now + timedelta(days=idx)
        label = f"{d.strftime('%Y/%m/%d')}（{['一','二','三','四','五','六','日'][d.weekday()]}）"
        start, end = d, d
    elif period == "weekly":
        monday = now - timedelta(days=now.weekday()) + timedelta(weeks=idx)
        sunday = monday + timedelta(days=6)
        label = f"{monday.strftime('%Y/%m/%d')} ~ {sunday.strftime('%Y/%m/%d')}"
        start, end = monday, sunday
    elif period == "monthly":
        y, m = now.year, now.month
        total_months = y * 12 + m - 1 + idx
        ny, nm = total_months // 12, total_months % 12 + 1
        first = date(ny, nm, 1)
        last = date(ny, nm + 1, 1) - timedelta(days=1) if nm < 12 else date(ny, 12, 31)
        label = f"{ny}年{nm}月"
        start, end = first, last
    elif period == "quarterly":
        current_q = (now.month - 1) // 3
        total_q = now.year * 4 + current_q + idx
        ny, nq = total_q // 4, total_q % 4
        first_m = nq * 3 + 1
        last_m = first_m + 2
        first = date(ny, first_m, 1)
        last = date(ny, last_m + 1, 1) - timedelta(days=1) if last_m < 12 else date(ny, 12, 31)
        label = f"{ny}年 Q{nq+1}（{first_m}-{last_m}月）"
        start, end = first, last
    elif period == "yearly":
        y = now.year + idx
        label = f"{y}年"
        start, end = date(y, 1, 1), date(y, 12, 31)
    else:
        return jsonify({"error": "invalid period"}), 400

    db = get_db()
    start_s = start.strftime(DATE_FMT)
    end_s = end.strftime(DATE_FMT)

    created = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND date(created_at) BETWEEN ? AND ? ORDER BY category",
        (start_s, end_s)
    ).fetchall()]

    active_rows = [row_to_dict(r) for r in db.execute(
        """SELECT DISTINCT p.* FROM projects p
           INNER JOIN status_logs sl ON p.id = sl.project_id
           WHERE p.deleted=0 AND date(sl.changed_at) BETWEEN ? AND ?
           ORDER BY p.category""",
        (start_s, end_s)
    ).fetchall()]

    published = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE deleted=0 AND current_status='已發布' AND date(updated_at) BETWEEN ? AND ?",
        (start_s, end_s)
    ).fetchall()]

    # Category breakdown
    cat_created = {}
    for r in created:
        cat_created[r["category"]] = cat_created.get(r["category"], 0) + 1
    cat_active = {}
    for r in active_rows:
        cat_active[r["category"]] = cat_active.get(r["category"], 0) + 1
    cat_pub = {}
    for r in published:
        cat_pub[r["category"]] = cat_pub.get(r["category"], 0) + 1

    # Status breakdown
    status_counts = {}
    for r in created:
        status_counts[r["current_status"]] = status_counts.get(r["current_status"], 0) + 1

    # Trend data (monthly)
    trend_rows = db.execute(
        "SELECT strftime('%Y-%m', created_at) as ym, COUNT(*) as created_cnt, "
        "SUM(CASE WHEN current_status='已發布' THEN 1 ELSE 0 END) as published_cnt "
        "FROM projects WHERE deleted=0 AND date(created_at) BETWEEN ? AND ? "
        "GROUP BY ym ORDER BY ym ASC",
        (start_s, end_s)
    ).fetchall()

    return jsonify({
        "period": period,
        "idx": idx,
        "label": label,
        "start": start_s,
        "end": end_s,
        "created": created,
        "active": active_rows,
        "published": [row_to_dict(r) for r in published],
        "cat_created": cat_created,
        "cat_active": cat_active,
        "cat_published": cat_pub,
        "status_counts": status_counts,
        "trend": {
            "months": [r["ym"] for r in trend_rows],
            "created": [r["created_cnt"] for r in trend_rows],
            "published": [r["published_cnt"] for r in trend_rows],
        },
    })


# ── Import / Export ────────────────────────────────────────────
@app.route("/api/import", methods=["POST"])
def api_import():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400

    f = request.files["file"]
    ext = Path(f.filename).suffix.lower() if f.filename else ""
    db = get_db()

    try:
        if ext in (".db", ".sqlite", ".sqlite3"):
            # Use separate connection to avoid ATTACH locking issues
            import tempfile
            tmp_path = os.path.join(tempfile.gettempdir(), f"_ultraclean_import_{int(datetime.now().timestamp())}.db")
            f.save(tmp_path)

            src_conn = sqlite3.connect(tmp_path)
            src_conn.row_factory = sqlite3.Row
            src_db = src_conn.cursor()

            try:
                src_cols = {r["name"] for r in src_db.execute("PRAGMA table_info(projects)").fetchall()}
            except Exception:
                src_conn.close()
                os.unlink(tmp_path)
                return jsonify({"error": "no projects table in source"}), 400

            target_cols = ["name", "category", "current_status", "note", "deleted",
                           "created_at", "updated_at", "edit_date", "review_date"]
            available = [c for c in target_cols if c in src_cols]

            existing_names = {r["name"] for r in db.execute("SELECT name FROM projects").fetchall()}
            src_projects = src_db.execute(f"SELECT {', '.join(available)} FROM projects").fetchall()

            imported = 0
            name_to_new_id = {}
            for row in src_projects:
                if row["name"] in existing_names:
                    continue
                values = [row[c] for c in available]
                placeholders = ", ".join(["?"] * len(available))
                db.execute(f"INSERT INTO projects ({', '.join(available)}) VALUES ({placeholders})", values)
                new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                name_to_new_id[row["name"]] = new_id
                imported += 1

            # Status logs
            if "project_id" in src_cols and imported > 0:
                try:
                    log_cols = {r["name"] for r in src_db.execute("PRAGMA table_info(status_logs)").fetchall()}
                except Exception:
                    log_cols = set()

                if log_cols:
                    log_target = ["project_id", "old_status", "new_status", "changed_at", "note"]
                    log_avail = [c for c in log_target if c in log_cols]
                    src_logs = src_db.execute(
                        f"SELECT sl.{', sl.'.join(log_avail)}, p.name FROM status_logs sl "
                        "JOIN projects p ON sl.project_id = p.id"
                    ).fetchall()
                    log_inserted = 0
                    for lr in src_logs:
                        name = lr["name"]
                        if name in name_to_new_id:
                            new_id = name_to_new_id[name]
                            values = [new_id if c == "project_id" else lr[c] for c in log_avail]
                            try:
                                db.execute(
                                    f"INSERT INTO status_logs ({', '.join(log_avail)}) VALUES ({', '.join(['?']*len(log_avail))})",
                                    values
                                )
                                log_inserted += 1
                            except Exception:
                                pass

            src_conn.close()
            os.unlink(tmp_path)

        elif ext == ".csv":
            content = f.stream.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            now = now_str()
            imported = 0
            skipped = 0
            cat_reverse = {v: k for k, v in CATEGORIES.items()}

            for row in reader:
                name = (row.get("name") or row.get("影片名稱") or "").strip()
                if not name:
                    continue
                existing = db.execute("SELECT id FROM projects WHERE name=? AND deleted=0", (name,)).fetchone()
                if existing:
                    skipped += 1
                    continue

                category = (row.get("category") or row.get("業務線") or "pest_care").strip()
                if category in cat_reverse:
                    category = cat_reverse[category]
                elif category not in CATEGORIES:
                    category = "pest_care"

                status = (row.get("current_status") or row.get("狀態") or "素材歸檔").strip()
                if status not in STATUSES:
                    status = "素材歸檔"

                note = (row.get("note") or row.get("備註") or "").strip()
                created = (row.get("created_at") or row.get("建立時間") or now).strip()
                updated = (row.get("updated_at") or row.get("更新時間") or now).strip()
                edit_date = (row.get("edit_date") or row.get("剪輯日期") or None)
                review_date = (row.get("review_date") or row.get("送審日期") or None)

                cur = db.execute(
                    "INSERT INTO projects (name, category, current_status, note, created_at, updated_at, edit_date, review_date) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (name, category, status, note, created, updated,
                     edit_date if edit_date and edit_date.strip() else None,
                     review_date if review_date and review_date.strip() else None)
                )
                pid = cur.lastrowid
                db.execute(
                    "INSERT INTO status_logs (project_id, old_status, new_status, changed_at) VALUES (?,NULL,?,?)",
                    (pid, status, created)
                )
                imported += 1
        else:
            return jsonify({"error": f"unsupported format: {ext}"}), 400

        db.commit()
        return jsonify({"imported": imported, "skipped": locals().get("skipped", 0)})

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/export", methods=["GET"])
def api_export():
    # Force WAL checkpoint so the file is consistent
    db = get_db()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()

    return send_file(
        DB_PATH,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=f"UltraCleanTracker_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    )


# ── Health Check ──────────────────────────────────────────────
@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "version": APP_VERSION, "db": DB_PATH})


# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
