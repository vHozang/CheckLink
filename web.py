
import math
import os
import io
import csv
import sqlite3
from datetime import datetime, timezone

from flask import Flask, request, redirect, url_for, send_file, render_template_string, flash

from testlink import check_links, normalize_url

APP_TITLE = "CheckLink – Shopify Monitor"

DB_PATH = os.environ.get("DB_PATH", "data.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS checks (
        url TEXT PRIMARY KEY,
        classification TEXT,
        http_status INTEGER,
        final_url TEXT,
        error TEXT,
        updated_at TEXT
    );""")
    cur.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT,
        prev_classification TEXT,
        new_classification TEXT,
        changed_at TEXT
    );""")
    conn.commit()
    conn.close()

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def parse_iso8601(value):
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    value = value.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

def _linewise(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]

def _dedup_preserve(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

OK_SET = {"LIVE", "PASSWORD"}
BAD_SET = {"DEAD", "BLOCKED", "BLOCKED(401)", "BLOCKED_OR_DNS"}
UNPAID_SET = {"UNPAID", "UNPAID_PLAN"}

def group_of(c):
    c = (c or "").upper()
    if c in OK_SET: return "ok"
    if c in BAD_SET: return "bad"
    if c in UNPAID_SET: return "unpaid"
    return "other"

def clamp_timeout(v, default=20.0):
    try:
        t = float(v) if v not in (None, "") else float(os.environ.get("TIMEOUT", default))
    except Exception:
        t = float(os.environ.get("TIMEOUT", default))
    return max(1.0, min(t, 60.0))

def clamp_interval(v, default=10.0):
    try:
        t = float(v) if v not in (None, "") else float(os.environ.get("CHECK_INTERVAL_DEFAULT", default))
    except Exception:
        t = float(os.environ.get("CHECK_INTERVAL_DEFAULT", default))
    return max(1.0, min(t, 20.0))

def get_last_check_time():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT updated_at FROM checks ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return parse_iso8601(row["updated_at"])

# ---------------------------- Flask App ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

init_db()

# ---------------------------- HTML Template ----------------------------
TEMPLATE = r"""<!doctype html>
<html lang="vi" data-theme="{{ theme }}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600&display=swap" rel="stylesheet" />
  <title>{{ title }}</title>
  <style>
    :root{
      --bg:#0b0f18; --card:#121826; --muted:#a3b3c2; --text:#e5e7eb;
      --ok:#10b981; --bad:#ef4444; --unpaid:#f59e0b; --other:#60a5fa;
      --border:#1f2937; --accent:#2563eb; --success:#22c55e;
    }
    [data-theme="light"]{
      --bg:#f6f7fb; --card:#ffffff; --muted:#4b5563; --text:#0b1220;
      --border:#e5e7eb; --accent:#2563eb;
    }
    *{ box-sizing: border-box }
    body{ margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:var(--bg); color:var(--text); }
    a{ color: var(--accent); text-decoration:none }
    .wrap{ max-width:1200px; margin:24px auto; padding:0 16px; }
    .topbar{ display:flex; gap:12px; align-items:center; justify-content:space-between; margin-bottom:12px; }
    .title{ font-size:22px; font-weight:800; }
    .toggle{ display:flex; align-items:center; gap:8px; }
    .btn{ background:var(--accent); color:#fff; border:none; border-radius:12px; padding:10px 14px; font-weight:700; cursor:pointer; }
    .btn.gray{ background:#374151; color:#fff; }
    .row{ display:flex; gap:12px; flex-wrap:wrap; }
    .card{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.12); }
    .muted{ color:var(--muted) }
    .kpi{ display:grid; grid-template-columns: repeat(4,1fr); gap:12px; }
    .kpi .item{ padding:14px; border-radius:14px; background:var(--card); border:1px solid var(--border); }
    .kpi .label{ font-size:12px; color:var(--muted) }
    .kpi .val{ font-size:26px; font-weight:900; margin-top:4px; }
    .kpi .pct{ font-size:12px; margin-top:2px; }
    .ok{ color:var(--ok) } .bad{ color:var(--bad) } .unpaid{ color:var(--unpaid) } .other{ color:var(--other) }
    textarea{ width:100%; min-height:160px; border-radius:12px; border:1px solid var(--border); padding:12px; background:transparent; color:var(--text); font-family:'Fira Code', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    input[type="number"]{ border:1px solid var(--border); background:transparent; color:var(--text); border-radius:12px; padding:10px 12px; width:120px; }
    table{ width:100%; border-collapse:collapse; }
    th,td{ border-bottom:1px solid var(--border); padding:8px 8px; text-align:left; font-size:14px; }
    th{ color:var(--muted) }
    td.code{ font-family:'Fira Code', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space:nowrap; max-width:380px; overflow:hidden; text-overflow:ellipsis; }
    .pill{ display:inline-flex; align-items:center; gap:6px; padding:4px 8px; border-radius:999px; font-weight:700; font-size:12px; }
    .pill.ok{ background:rgba(16,185,129,.15); color:var(--ok) }
    .pill.bad{ background:rgba(239,68,68,.15); color:var(--bad) }
    .pill.unpaid{ background:rgba(245,158,11,.15); color:var(--unpaid) }
        .pill.other{ background:rgba(96,165,250,.15); color:var(--other) }
    .error-cell{ color:var(--muted); font-size:13px; max-width:260px; white-space:normal; word-break:break-word; }
    .flash{ background:rgba(239,68,68,.15); border:1px solid rgba(239,68,68,.35); color:var(--bad); padding:10px; border-radius:10px; margin:10px 0; }
    .toolbar{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    select{ border:1px solid var(--border); background:transparent; color:var(--text); border-radius:12px; padding:10px 12px; }
    .success{ background:rgba(34,197,94,.15); border:1px solid rgba(34,197,94,.35); color:var(--success); padding:10px; border-radius:10px; margin:10px 0; }
  </style>
  <script>
    function toggleTheme(){
      const html = document.documentElement;
      const cur = html.getAttribute('data-theme') || 'dark';
      const next = (cur === 'dark') ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      try{ localStorage.setItem('theme', next); }catch(e){}
    }
    (function(){
      try{
        const t = localStorage.getItem('theme');
        if(t){ document.documentElement.setAttribute('data-theme', t); }
      }catch(e){}
    })();
  </script>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">{{ title }}</div>
      <div class="toggle">
        <button class="btn gray" onclick="toggleTheme()">Dark/Light</button>
        <form method="POST" action="{{ url_for('export_csv') }}">
          <button class="btn" title="Xuất toàn bộ DB ra CSV">Xuất CSV</button>
        </form>
      </div>
    </div>

    {% with messages = get_flashed_messages() %}
      {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
    {% endwith %}

    <div class="row">
      <div class="card" style="flex:1 1 340px; min-width:320px;">
        <h3>Nhập Danh Sách Kiểm Tra</h3>
        <form method="POST" action="{{ url_for('check') }}" enctype="multipart/form-data" class="row">
          <textarea name="links_text" placeholder="https://examplestore.myshopify.com
https://custom-domain.com
store1.com"></textarea>
          <div class="toolbar">
            <label>File .txt: <input type="file" name="txtfile" accept=".txt"></label>
            <label>Chu kỳ chạy (1–20s): <input type="number" name="interval" min="1" max="20" step="1" value="{{ default_interval }}"></label>
            <button class="btn" type="submit">Chạy Kiểm Tra</button>
          </div>
        </form>
        {% if last_run %}
          <div class="success">Đã chạy: {{ last_run }}</div>
        {% endif %}
      </div>

      <div class="card" style="flex:2 1 540px; min-width:380px;">
        <h3>Tổng Quan</h3>
        <div class="kpi">
          <div class="item">
            <div class="label">Tổng Stores</div>
            <div class="val">{{ metrics.total }}</div>
          </div>
          <div class="item">
            <div class="label">LIVE Stores</div>
            <div class="val ok">{{ metrics.live }}</div>
            <div class="pct ok">{{ metrics.live_pct }}%</div>
          </div>
          <div class="item">
            <div class="label">DEAD Stores</div>
            <div class="val bad">{{ metrics.dead }}</div>
            <div class="pct bad">{{ metrics.dead_pct }}%</div>
          </div>
          <div class="item">
            <div class="label">UNPAID Stores</div>
            <div class="val unpaid">{{ metrics.unpaid }}</div>
            <div class="pct unpaid">{{ metrics.unpaid_pct }}%</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h3>Trạng Thái Hiện Tại</h3>
      <p class="muted">Hiển thị {{ statuses|length }} link gần nhất (giới hạn {{ status_limit }}).</p>
      <table>
        <thead>
          <tr>
            <th>URL Store</th>
            <th>Final URL</th>
            <th>HTTP</th>
            <th>Phân Loại</th>
            <th>Cập Nhật</th>
            <th>Lỗi</th>
          </tr>
        </thead>
        <tbody>
          {% for row in statuses %}
            <tr>
              <td class="code"><a href="{{ row.url }}" target="_blank" rel="noopener">{{ row.url }}</a></td>
              <td class="code">{% if row.final_url %}<a href="{{ row.final_url }}" target="_blank" rel="noopener">{{ row.final_url }}</a>{% else %}-{% endif %}</td>
              <td>{{ row.http_status or 'N/A' }}</td>
              <td><span class="pill {{ row.group }}">{{ row.classification or 'UNKNOWN' }}</span></td>
              <td>{{ row.updated_at or '-' }}</td>
              <td class="error-cell">{{ row.error or '' }}</td>
            </tr>
          {% else %}
            <tr><td colspan="6" class="muted">Chưa có dữ liệu. Hãy chạy kiểm tra để bắt đầu.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def compute_metrics():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM checks")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM checks WHERE upper(classification) IN (%s)" % ",".join(["?"]*len(OK_SET)), tuple(OK_SET))
    live = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM checks WHERE upper(classification) IN (%s)" % ",".join(["?"]*len(BAD_SET)), tuple(BAD_SET))
    dead = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM checks WHERE upper(classification) IN (%s)" % ",".join(["?"]*len(UNPAID_SET)), tuple(UNPAID_SET))
    unpaid = cur.fetchone()["c"]
    conn.close()
    def pct(n, d): return round((n*100.0/d), 1) if d else 0.0
    return {
        "total": total,
        "live": live, "dead": dead, "unpaid": unpaid,
        "live_pct": pct(live, max(total, 1)),
        "dead_pct": pct(dead, max(total, 1)),
        "unpaid_pct": pct(unpaid, max(total, 1)),
    }



@app.route("/", methods=["GET"])
def index():
    status_limit = int(os.environ.get("STATUS_LIMIT", "500"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT url, final_url, http_status, classification, error, updated_at
        FROM checks
        ORDER BY datetime(updated_at) DESC
        LIMIT ?
    """, (status_limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    statuses = [{
        "url": r["url"],
        "final_url": r["final_url"],
        "http_status": r["http_status"],
        "classification": (r["classification"] or "").upper(),
        "group": group_of(r["classification"]),
        "error": r["error"],
        "updated_at": r["updated_at"],
    } for r in rows]

    metrics = compute_metrics()
    last_run = rows[0]["updated_at"] if rows else None

    theme = request.cookies.get("theme") or "dark"
    interval_cookie = request.cookies.get("interval")
    default_interval = int(clamp_interval(interval_cookie, default=10.0))

    return render_template_string(TEMPLATE, title=APP_TITLE, theme=theme, metrics=metrics,
                                  last_run=last_run, default_interval=default_interval,
                                  statuses=statuses, status_limit=status_limit)

@app.route("/check", methods=["POST"])
def check():
    links_text = request.form.get("links_text", "").strip()
    file = request.files.get("txtfile")
    timeout = clamp_timeout(None, default=20.0)
    interval = clamp_interval(request.form.get("interval"), default=10.0)
    interval_value = int(max(1, min(20, round(interval))))
    cooldown_seconds = float(interval_value)
    last_check_at = get_last_check_time()
    now_dt = datetime.now(timezone.utc)
    if last_check_at:
        elapsed = (now_dt - last_check_at).total_seconds()
        if elapsed < cooldown_seconds:
            wait = math.ceil(cooldown_seconds - elapsed)
            flash(f"Vui lòng đợi thêm {wait}s trước khi chạy lại.")
            resp = redirect(url_for('index'))
            resp.set_cookie("interval", str(interval_value), max_age=30 * 24 * 60 * 60)
            return resp

    items = []
    if links_text:
        items += _linewise(links_text)
    if file and file.filename:
        try:
            content = file.read().decode("utf-8", errors="ignore")
            items += _linewise(content)
        except Exception:
            flash("Không đọc được file .txt (hãy dùng UTF-8).")
            return redirect(url_for('index'))

    items = [normalize_url(x) for x in items]
    items = _dedup_preserve(items)

    if not items:
        flash("Chưa có link nào được nhập.")
        return redirect(url_for('index'))

    # Limit batch
    MAX_LINKS = int(os.environ.get("MAX_LINKS", "2500"))
    if len(items) > MAX_LINKS:
        items = items[:MAX_LINKS]
        flash(f"Danh sách quá dài, chỉ kiểm tra {MAX_LINKS} link đầu.")

    # Run check
    results = check_links(items, use_proxy=False, proxy_hostport=None, timeout=timeout)

    # Persist & detect changes
    conn = get_db()
    cur = conn.cursor()
    now = now_iso()
    for r in results:
        url = r.get("normalized_url") or r.get("input_url")
        newc = (r.get("classification") or "").upper()
        http_status = r.get("http_status")
        final_url = r.get("final_url")
        error = r.get("error")

        cur.execute("SELECT classification FROM checks WHERE url = ?", (url,))
        row = cur.fetchone()
        prevc = (row["classification"] if row else None)

        if row is None:
            cur.execute("INSERT INTO checks (url, classification, http_status, final_url, error, updated_at) VALUES (?,?,?,?,?,?)",
                        (url, newc, http_status, final_url, error, now))
            if newc:
                cur.execute("INSERT INTO events (url, prev_classification, new_classification, changed_at) VALUES (?,?,?,?)",
                            (url, None, newc, now))
        else:
            if (prevc or "").upper() != newc:
                cur.execute("INSERT INTO events (url, prev_classification, new_classification, changed_at) VALUES (?,?,?,?)",
                            (url, prevc, newc, now))
            cur.execute("UPDATE checks SET classification=?, http_status=?, final_url=?, error=?, updated_at=? WHERE url=?",
                        (newc, http_status, final_url, error, now, url))
    conn.commit()
    conn.close()

    flash(f"Đã kiểm tra {len(results)} link. Timeout={int(timeout)}s.")
    return redirect(url_for('index'))

@app.route("/export.csv", methods=["POST"])
def export_csv():
    # Export all rows of 'checks'
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT url, final_url, http_status, classification, error, updated_at FROM checks ORDER BY url")
    rows = cur.fetchall()
    conn.close()

    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(["url", "final_url", "http_status", "classification", "error", "updated_at"])
    for r in rows:
        writer.writerow([r["url"], r["final_url"], r["http_status"], r["classification"], (r["error"] or "").replace('\\n',' '), r["updated_at"]])
    bio = io.BytesIO(sio.getvalue().encode("utf-8-sig"))
    bio.seek(0)
    return send_file(bio, mimetype="text/csv", as_attachment=True, download_name="shopify-checks.csv")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")), debug=True)
