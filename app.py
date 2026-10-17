import os, csv, io, time, json, sqlite3, uuid, hmac, secrets
from collections import Counter
from datetime import datetime, timezone
import requests, shapely
from shapely.geometry import shape
from shapely.strtree import STRtree
from flask import Flask, jsonify, request, send_from_directory, session, Response
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # alamat https yang benar di belakang proxy hosting
BASE = os.path.dirname(os.path.abspath(__file__))

# ---------- Konfigurasi, database, upload ----------
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config.update(MAX_CONTENT_LENGTH=6 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("PRODUKSI") == "1")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DB = os.path.join(BASE, "data", "app.db")
UPLOAD = os.path.join(BASE, "uploads")
os.makedirs(UPLOAD, exist_ok=True)

def q(sql, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()

q("""CREATE TABLE IF NOT EXISTS laporan(id INTEGER PRIMARY KEY AUTOINCREMENT, waktu TEXT, nama TEXT,
     deskripsi TEXT, lat REAL, lon REAL, foto TEXT, status TEXT DEFAULT 'menunggu')""")
q("CREATE TABLE IF NOT EXISTS hotspot_harian(tanggal TEXT, provinsi TEXT, jumlah INTEGER, PRIMARY KEY(tanggal, provinsi))")

def _simpan_harian(rows):
    cnt = Counter((r["tanggal"], r["provinsi"]) for r in rows)
    c = sqlite3.connect(DB)
    c.executemany("INSERT INTO hotspot_harian VALUES(?,?,?) ON CONFLICT(tanggal,provinsi) "
                  "DO UPDATE SET jumlah=MAX(jumlah, excluded.jumlah)", [(t, p, n) for (t, p), n in cnt.items()])
    c.commit()
    c.close()

FIRMS_KEY = os.environ.get("FIRMS_MAP_KEY", "")
BBOX = "95,-11,141,6"
SOURCE = "VIIRS_SNPP_NRT"
TTL = 1800
_cache = {}

# ---------- Batas provinsi (GeoJSON) ----------
def _nama(p):
    return (p.title().replace("Di. ", "").replace("Nusatenggara", "Nusa Tenggara")
            .replace("Dki ", "DKI ").replace("Daerah Istimewa Yogyakarta", "DI Yogyakarta"))

with open(os.path.join(BASE, "data", "provinsi.geojson"), encoding="utf-8") as f:
    _fitur = json.load(f)["features"]
_geoms = [shape(x["geometry"]) for x in _fitur]
_names = [_nama(x["properties"]["Propinsi"]) for x in _fitur]
_tree = STRtree(_geoms)

def tandai_provinsi(rows):
    if not rows:
        return
    pts = shapely.points([r["lon"] for r in rows], [r["lat"] for r in rows])
    prov = {}
    i_in, t_in = _tree.query(pts, predicate="within")
    for i, t in zip(i_in, t_in):
        prov.setdefault(int(i), _names[int(t)])
    sisa = [i for i in range(len(rows)) if i not in prov]
    if sisa:  # titik pesisir yang jatuh sedikit di luar poligon: ambil provinsi terdekat (<~11 km)
        i_n, t_n = _tree.query_nearest(pts[sisa], max_distance=0.1)
        for i, t in zip(i_n, t_n):
            prov.setdefault(sisa[int(i)], _names[int(t)])
    for i, r in enumerate(rows):
        r["provinsi"] = prov.get(i, "Di luar wilayah")

# ---------- Data FIRMS ----------
def fetch_hotspots(days, tanggal=None):
    key = (days, tanggal)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_KEY}/{SOURCE}/{BBOX}/{days}"
    if tanggal:
        url += f"/{tanggal}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if not r.text.startswith("latitude"):
        raise ValueError("Respon FIRMS tidak valid (cek FIRMS_MAP_KEY atau tanggal)")
    rows = []
    for d in csv.DictReader(io.StringIO(r.text)):
        t = d["acq_time"].zfill(4)
        rows.append({
            "lat": float(d["latitude"]), "lon": float(d["longitude"]),
            "tanggal": d["acq_date"], "jam_utc": f"{t[:2]}:{t[2:]}",
            "satelit": d.get("satellite", "-"), "confidence": d["confidence"].lower(),
            "frp": float(d["frp"]), "brightness": float(d["bright_ti4"]),
        })
    tandai_provinsi(rows)
    try:
        _simpan_harian(rows)
    except Exception:
        pass
    _cache[key] = (time.time(), rows)
    return rows

def _param():
    try:
        days = min(max(int(request.args.get("days", 1)), 1), 5)
    except ValueError:
        days = 1
    tanggal = request.args.get("tanggal") or None
    if tanggal:
        datetime.strptime(tanggal, "%Y-%m-%d")  # validasi format
    return days, tanggal

@app.errorhandler(ValueError)
def _bad(e):
    return jsonify({"error": str(e)}), 400

# ---------- SEO: robots.txt & sitemap.xml ----------
@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nDisallow: /uploads/\n"
                    f"Sitemap: {request.host_url}sitemap.xml\n", mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap():
    u = request.host_url.rstrip("/")
    isi = "".join(f"<url><loc>{u}{p}</loc></url>" for p in ["/", "/peta", "/statistik", "/lapor"])
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + isi + "</urlset>", mimetype="application/xml")

# ---------- Halaman ----------
@app.route("/")
def home():
    return send_from_directory("static", "index.html")

@app.route("/peta")
def peta():
    return send_from_directory("static", "peta.html")

@app.route("/statistik")
def statistik():
    return send_from_directory("static", "statistik.html")

# ---------- API ----------
@app.route("/api/provinsi")
def api_provinsi():
    return jsonify(sorted(set(_names)))

@app.route("/api/hotspots")
def api_hotspots():
    days, tanggal = _param()
    conf = set(request.args.get("confidence", "l,n,h").split(","))
    prov = request.args.get("provinsi") or None
    try:
        rows = [h for h in fetch_hotspots(days, tanggal)
                if h["confidence"] in conf and (not prov or h["provinsi"] == prov)]
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    terbaru = max((h["tanggal"] for h in rows), default=None)
    return jsonify({"days": days, "total": len(rows), "terbaru": terbaru, "data": rows})

@app.route("/api/statistik")
def api_statistik():
    days, tanggal = _param()
    try:
        rows = fetch_hotspots(days, tanggal)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    prov = Counter(h["provinsi"] for h in rows)
    hari = Counter(h["tanggal"] for h in rows)
    return jsonify({
        "total": len(rows),
        "provinsi": [{"nama": n, "jumlah": c} for n, c in prov.most_common()],
        "harian": [{"tanggal": t, "jumlah": hari[t]} for t in sorted(hari)],
    })

# ---------- Laporan warga & admin ----------
_rate = {}

def _ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()

def _batas(kunci, maks, detik):
    now = time.time()
    l = [t for t in _rate.get(kunci, []) if now - t < detik]
    if len(l) >= maks:
        return False
    l.append(now)
    _rate[kunci] = l
    return True

def _jenis_gambar(b):
    if b.startswith(b"\xff\xd8\xff"): return "jpg"
    if b.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    return None

@app.errorhandler(413)
def _besar(e):
    return jsonify({"error": "Ukuran file terlalu besar (maks 5 MB)"}), 413

@app.route("/lapor")
def lapor_page():
    return send_from_directory("static", "lapor.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/uploads/<path:nama>")
def uploads(nama):
    return send_from_directory(UPLOAD, nama)

@app.route("/api/laporan", methods=["POST"])
def kirim_laporan():
    if not _batas("lapor" + _ip(), 5, 3600):
        return jsonify({"error": "Terlalu banyak laporan, coba lagi nanti"}), 429
    f = request.form
    try:
        lat, lon = float(f.get("lat", "")), float(f.get("lon", ""))
    except ValueError:
        return jsonify({"error": "Lokasi tidak valid"}), 400
    if not (-11 <= lat <= 6 and 95 <= lon <= 141):
        return jsonify({"error": "Lokasi di luar Indonesia"}), 400
    desk = f.get("deskripsi", "").strip()[:1000]
    if len(desk) < 10:
        return jsonify({"error": "Deskripsi minimal 10 karakter"}), 400
    foto = None
    file = request.files.get("foto")
    if file and file.filename:
        data = file.read()
        jenis = _jenis_gambar(data)
        if not jenis:
            return jsonify({"error": "Foto harus JPG, PNG, atau WEBP"}), 400
        foto = f"{uuid.uuid4().hex}.{jenis}"
        with open(os.path.join(UPLOAD, foto), "wb") as o:
            o.write(data)
    q("INSERT INTO laporan(waktu,nama,deskripsi,lat,lon,foto) VALUES(?,?,?,?,?,?)",
      (datetime.now(timezone.utc).isoformat(timespec="seconds"), f.get("nama", "").strip()[:60], desk, lat, lon, foto))
    return jsonify({"ok": True})

@app.route("/api/laporan")
def daftar_laporan():
    rows = q("SELECT id,waktu,deskripsi,lat,lon,foto FROM laporan WHERE status='terverifikasi' ORDER BY id DESC LIMIT 200")
    return jsonify([dict(r) for r in rows])

def _admin():
    return session.get("admin") is True

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"error": "ADMIN_PASSWORD belum diatur di server"}), 503
    if not _batas("login" + _ip(), 5, 600):
        return jsonify({"error": "Terlalu banyak percobaan, tunggu 10 menit"}), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Password salah"}), 401

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/admin/laporan")
def admin_daftar():
    if not _admin():
        return jsonify({"error": "Belum login"}), 401
    st = request.args.get("status", "menunggu")
    return jsonify([dict(r) for r in q("SELECT * FROM laporan WHERE status=? ORDER BY id DESC LIMIT 200", (st,))])

@app.route("/api/admin/laporan/<int:i>", methods=["POST"])
def admin_ubah(i):
    if not _admin():
        return jsonify({"error": "Belum login"}), 401
    st = (request.get_json(silent=True) or {}).get("status")
    if st not in ("terverifikasi", "ditolak", "menunggu"):
        return jsonify({"error": "Status tidak valid"}), 400
    q("UPDATE laporan SET status=? WHERE id=?", (st, i))
    return jsonify({"ok": True})

@app.route("/api/statistik/bulanan")
def stat_bulanan():
    rows = q("SELECT substr(tanggal,1,7) AS bulan, SUM(jumlah) AS jumlah FROM hotspot_harian GROUP BY bulan ORDER BY bulan")
    return jsonify([dict(r) for r in rows])

# ---------- Cuaca & kualitas udara (Open-Meteo, tanpa API key) ----------
_cuaca_cache = {}

def _buat_grid():
    pts = [(float(la), float(lo)) for la in range(-9, 6, 2) for lo in range(96, 141, 2)]
    arr = shapely.points([p[1] for p in pts], [p[0] for p in pts])
    i_in, _ = _tree.query(arr, predicate="within")   # hanya titik di daratan Indonesia
    return [pts[k] for k in sorted(set(int(x) for x in i_in))]

GRID = _buat_grid()

def _open_meteo(url, current):
    hasil = []
    for s in range(0, len(GRID), 50):
        b = GRID[s:s + 50]
        r = requests.get(url, params={
            "latitude": ",".join(str(p[0]) for p in b),
            "longitude": ",".join(str(p[1]) for p in b),
            "current": current, "timezone": "UTC"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        hasil += j if isinstance(j, list) else [j]
    return hasil

def _cached(nama, fn):
    hit = _cuaca_cache.get(nama)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    v = fn()
    _cuaca_cache[nama] = (time.time(), v)
    return v

@app.route("/api/cuaca")
def api_cuaca():
    def ambil():
        out = []
        for d in _open_meteo("https://api.open-meteo.com/v1/forecast",
                             "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m"):
            c = d["current"]
            out.append({"lat": d["latitude"], "lon": d["longitude"], "suhu": c["temperature_2m"],
                        "kelembapan": c["relative_humidity_2m"], "angin": c["wind_speed_10m"],
                        "arah": c["wind_direction_10m"]})
        return out
    try:
        return jsonify(_cached("cuaca", ambil))
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/aqi")
def api_aqi():
    def ambil():
        out = []
        for d in _open_meteo("https://air-quality-api.open-meteo.com/v1/air-quality", "us_aqi,pm2_5"):
            c = d["current"]
            if c.get("us_aqi") is not None:
                out.append({"lat": d["latitude"], "lon": d["longitude"], "aqi": c["us_aqi"], "pm25": c.get("pm2_5")})
        return out
    try:
        return jsonify(_cached("aqi", ambil))
    except Exception as e:
        return jsonify({"error": str(e)}), 502

if __name__ == "__main__":
    app.run(debug=True, port=5000)