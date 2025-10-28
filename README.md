# Shopify Checklink (Flask)

Trang web nhỏ để upload `.txt` hoặc dán danh sách URL (mỗi dòng 1 link) và kiểm tra tình trạng: `LIVE`, `PASSWORD`, `DEAD`, `BLOCKED`, `TIMEOUT`…

## Thành phần
- `web.py`: Flask app.
- `testlink.py`: logic kiểm tra HTTP/Shopify (từ file bạn cung cấp).
- `requirements.txt`: thư viện cần cài.
- `render.yaml`: cấu hình deploy lên Render.com (web service).

## Chạy local
```bash
python -m venv venv
venv\Scripts\activate    # Windows
# hoặc: source venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
set FLASK_ENV=development  # Windows (tùy chọn)
export FLASK_ENV=development  # macOS/Linux (tùy chọn)

# Tùy chọn proxy/tối đa link/timeout
set USE_PROXY=false
set PROXY_HOSTPORT=127.0.0.1:60000
set TIMEOUT=20

python web.py
# Truy cập http://localhost:8000
```

## Deploy trên Render.com
1. Push 4 file này lên GitHub (hoặc toàn bộ folder).
2. Tạo **Web Service** mới → chọn repo.
3. Runtime: **Python**. Build: `pip install -r requirements.txt`. Start: `gunicorn web:app`.
4. (Tùy chọn) Thêm **Environment Variables**: `USE_PROXY`, `PROXY_HOSTPORT`, `TIMEOUT`.
5. Deploy và truy cập domain do Render cấp.

## Ghi chú
- File `.txt` phải là UTF-8, mỗi dòng một URL; hệ thống tự `https://` nếu thiếu.
- Nếu có nhiều `BLOCKED/429`, hãy bật proxy và tăng `Timeout`.
- Nút **Tải CSV/JSON** để xuất kết quả.
- Giới hạn mặc định: 2000 link / lần.
## Hello World