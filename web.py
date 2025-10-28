
import os
import io
import csv
import uuid
from datetime import datetime, timedelta
from collections import Counter, defaultdict

from flask import Flask, request, redirect, url_for, send_file, render_template_string, flash

# Import functions from the provided testlink.py
from testlink import check_links, normalize_url

# ---------------------------- App setup ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB uploads

# In-memory export cache (auto-purged)
EXPORT_CACHE = {}
EXPORT_TTL = timedelta(hours=2)

def _purge_cache():
    now = datetime.utcnow()
    to_del = [k for k, v in EXPORT_CACHE.items() if now - v["ts"] > EXPORT_TTL]
    for k in to_del:
        del EXPORT_CACHE[k]

def _parse_bool(s, default=False):
    if s is None: return default
    s = str(s).strip().lower()
    return s in ("1","true","t","yes","y","on")

def _linewise(text):
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            out.append(ln)
    return out

def _dedup_preserve(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def classify_group(c):
    if c in ("LIVE", "PASSWORD"):
        return "ok"
    if c in ("DEAD", "BLOCKED", "BLOCKED(401)", "BLOCKED_OR_DNS"):
        return "bad"
    if c in ("TIMEOUT", "SSL_ERROR", "PROXY_FAIL", "RETRY", "UNREACHABLE"):
        return "unstable"
    return "other"

# ---------------------------- Routes ----------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Shopify Checklink</title>
  <style>
    :root { --bg:#0b0f18; --card:#121826; --muted:#a3b3c2; --ok:#34d399; --bad:#ef4444; --unstable:#f59e0b; --other:#60a5fa; }
    body{ margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:var(--bg); color:#e5e7eb; }
    .wrap{ max-width:1100px; margin:24px auto; padding:0 16px; }
    .card{ background:var(--card); border-radius:16px; padding:20px; box-shadow:0 10px 30px rgba(0,0,0,.25); }
    h1{ margin:0 0 12px; font-size:28px; }
    p.muted{ color:var(--muted); margin:0 0 16px; }
    form .row{ display:flex; gap:12px; flex-wrap:wrap; }
    textarea{ width:100%; min-height:180px; background:#0f1524; color:#e5e7eb; border:1px solid #1f2937; border-radius:12px; padding:12px; }
    .controls{ display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin:12px 0; }
    .control{ background:#0f1524; border:1px solid #1f2937; border-radius:12px; padding:10px 12px; }
    input[type="text"], input[type="number"] { background:transparent; border:none; color:#e5e7eb; outline:none; width:220px; }
    input[type="file"] { color:#e5e7eb; }
    .btn{ background:#2563eb; color:white; border:none; border-radius:12px; padding:12px 16px; cursor:pointer; font-weight:600; }
    .btn.secondary{ background:#374151; }
    .grid{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:12px; }
    .stat{ background:#0f1524; border:1px solid #1f2937; border-radius:12px; padding:12px; }
    .stat h3{ margin:0 0 6px; font-size:14px; color:var(--muted); }
    .stat .v{ font-size:20px; font-weight:700; }
    .ok{ color:var(--ok); } .bad{ color:var(--bad); } .unstable{ color:var(--unstable); } .other{ color:var(--other); }
    table{ width:100%; border-collapse: collapse; margin-top:16px; }
    th, td{ border-bottom:1px solid #1f2937; padding:8px 6px; font-size:14px; text-align:left; vertical-align:top; }
    th{ color:#9ca3af; font-weight:600; }
    td.code{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space:nowrap; max-width:280px; overflow:hidden; text-overflow:ellipsis; }
    .pill{ display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }
    .pill.ok{ background:rgba(52,211,153,.15); color:var(--ok); }
    .pill.bad{ background:rgba(239,68,68,.15); color:var(--bad); }
    .pill.unstable{ background:rgba(245,158,11,.15); color:var(--unstable); }
    .pill.other{ background:rgba(96,165,250,.15); color:var(--other); }
    .footer{ margin-top:24px; color:#9ca3af; font-size:12px; }
    .flash{ background:#1f2937; padding:10px 12px; border-radius:10px; margin-bottom:12px; color:#fca5a5; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Shopify Checklink</h1>
      <p class="muted">Nhập danh sách link (mỗi dòng 1 URL) hoặc tải tệp <b>.txt</b>. Hệ thống sẽ phân loại: LIVE, PASSWORD, DEAD, BLOCKED, TIMEOUT… và cho phép xuất CSV/JSON.</p>

      {% with messages = get_flashed_messages() %}
        {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
      {% endwith %}

      <form method="POST" action="{{ url_for('check') }}" enctype="multipart/form-data">
        <div class="row">
          <textarea name="links_text" placeholder="https://examplestore.myshopify.com
https://custom-domain.com
store1.com"></textarea>
        </div>
        <div class="controls">
          <label class="control">File .txt: <input type="file" name="txtfile" accept=".txt"></label>
          <label class="control">Timeout (s): <input type="number" step="1" name="timeout" value="{{ default_timeout }}"></label>
          <label class="control">Proxy SOCKS5 (host:port): <input type="text" name="proxy_hostport" value="{{ default_proxy }}"></label>
          <label class="control"><input type="checkbox" name="use_proxy" {% if default_use_proxy %}checked{% endif %}> Dùng proxy</label>
          <button class="btn" type="submit">Kiểm tra</button>
        </div>
      </form>

      {% if results %}
        <div class="grid">
          <div class="stat"><h3>Tổng URL</h3><div class="v">{{ total }}</div></div>
          <div class="stat"><h3>OK (LIVE + PASSWORD)</h3><div class="v ok">{{ ok }}</div></div>
          <div class="stat"><h3>BAD (DEAD/BLOCKED)</h3><div class="v bad">{{ bad }}</div></div>
          <div class="stat"><h3>Unstable/Other</h3><div class="v unstable">{{ unstable }}</div></div>
        </div>

        <div class="controls" style="margin-top: 16px;">
          <a class="btn secondary" href="{{ url_for('export_results', export_id=export_id, fmt='csv') }}">Tải CSV</a>
          <a class="btn secondary" href="{{ url_for('export_results', export_id=export_id, fmt='json') }}">Tải JSON</a>
        </div>

        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Input</th>
              <th>Final URL</th>
              <th>HTTP</th>
              <th>Trạng thái</th>
              <th>Lỗi</th>
            </tr>
          </thead>
          <tbody>
            {% for i, r in enumerate(results, start=1) %}
              {% set g = groups[i-1] %}
              <tr>
                <td>{{ i }}</td>
                <td class="code" title="{{ r.input_url }}">{{ r.input_url }}</td>
                <td class="code" title="{{ r.final_url or '' }}">{{ r.final_url or '' }}</td>
                <td>{{ r.http_status or '' }}</td>
                <td><span class="pill {{ g }}">{{ r.classification }}</span></td>
                <td class="code" title="{{ r.error or '' }}">{{ r.error or '' }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% endif %}

      <div class="footer">
        <div>Gợi ý: Bạn có thể bỏ trống proxy nếu không cần. Nếu nhiều BLOCKED/429 hãy bật proxy và tăng timeout.</div>
      </div>
    </div>
  </div>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    default_use_proxy = os.environ.get("USE_PROXY", "false").lower() in ("1","true","yes","y")
    default_proxy = os.environ.get("PROXY_HOSTPORT", "127.0.0.1:60000")
    default_timeout = int(os.environ.get("TIMEOUT", "20"))
    return render_template_string(INDEX_HTML, results=None, total=0, ok=0, bad=0, unstable=0,
                                  export_id=None, groups=[], default_use_proxy=default_use_proxy,
                                  default_proxy=default_proxy, default_timeout=default_timeout)

@app.route("/check", methods=["POST"])
def check():
    _purge_cache()

    # Read inputs
    links_text = request.form.get("links_text", "").strip()
    file = request.files.get("txtfile")
    timeout = request.form.get("timeout", "").strip()
    proxy_hostport = request.form.get("proxy_hostport", "").strip()
    use_proxy = _parse_bool(request.form.get("use_proxy"), default=False)

    # Gather lines
    collected = []
    if links_text:
        collected.extend(_linewise(links_text))
    if file and file.filename:
        try:
            content = file.read().decode("utf-8", errors="ignore")
            collected.extend(_linewise(content))
        except Exception:
            flash("Không đọc được file .txt (hãy dùng UTF-8).")
            return redirect(url_for('index'))

    collected = [normalize_url(x) for x in collected]
    collected = _dedup_preserve(collected)
    if not collected:
        flash("Chưa có link nào được nhập.")
        return redirect(url_for('index'))

    # Limit
    MAX_LINKS = int(os.environ.get("MAX_LINKS", "2000"))
    if len(collected) > MAX_LINKS:
        collected = collected[:MAX_LINKS]
        flash(f"Danh sách quá dài, chỉ kiểm tra {MAX_LINKS} link đầu.")

    # Parse timeout
    try:
        timeout_val = float(timeout) if timeout else None
    except ValueError:
        timeout_val = None

    # Run check
    results = check_links(collected, use_proxy=use_proxy, proxy_hostport=proxy_hostport or None, timeout=timeout_val)

    # Build stats
    groups = [classify_group(r.get("classification") or "") for r in results]
    counts = Counter([g for g in groups])
    total = len(results)
    ok = counts.get("ok", 0)
    bad = counts.get("bad", 0)
    unstable = counts.get("unstable", 0) + counts.get("other", 0)

    # Cache for export
    export_id = uuid.uuid4().hex[:12]
    EXPORT_CACHE[export_id] = {
        "ts": datetime.utcnow(),
        "results": results,
        "params": {
            "use_proxy": use_proxy,
            "proxy_hostport": proxy_hostport,
            "timeout": timeout_val,
        }
    }

    # Defaults for form repopulation
    default_use_proxy = use_proxy
    default_proxy = proxy_hostport or os.environ.get("PROXY_HOSTPORT", "127.0.0.1:60000")
    default_timeout = int(timeout_val or os.environ.get("TIMEOUT", "20"))

    return render_template_string(INDEX_HTML, results=results, total=total, ok=ok, bad=bad, unstable=unstable,
                                  export_id=export_id, groups=groups,
                                  default_use_proxy=default_use_proxy, default_proxy=default_proxy,
                                  default_timeout=default_timeout)

@app.route("/export/<export_id>")
def export_results(export_id):
    _purge_cache()
    fmt = request.args.get("fmt", "csv").lower()
    item = EXPORT_CACHE.get(export_id)
    if not item:
        flash("Phiên xuất dữ liệu đã hết hạn. Hãy kiểm tra lại link và xuất mới.")
        return redirect(url_for('index'))

    results = item["results"]

    if fmt == "json":
        buf = io.BytesIO()
        buf.write((
            '{"exported_at": "%s", "count": %d, "results": %s}'
            % (datetime.utcnow().isoformat()+'Z', len(results), json.dumps(results, ensure_ascii=False))
        ).encode("utf-8"))
        buf.seek(0)
        return send_file(buf, mimetype="application/json", as_attachment=True, download_name=f"shopify-checker-{export_id}.json")

    # default CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["input_url", "normalized_url", "final_url", "http_status", "classification", "error"])
    for r in results:
        writer.writerow([
            r.get("input_url",""),
            r.get("normalized_url",""),
            r.get("final_url",""),
            r.get("http_status",""),
            r.get("classification",""),
            (r.get("error","") or "").replace("\n"," ").replace("\r"," "),
        ])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"shopify-checker-{export_id}.csv")

if __name__ == "__main__":
    # Local dev
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")), debug=True)
