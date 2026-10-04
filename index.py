
import atexit
import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MEDIA = DATA / "media"
THUMBS = DATA / "thumbs"
TMP = DATA / "tmp"
DB = DATA / "inhhab.db"
SECRET = DATA / ".session_secret"
SETUP = DATA / ".setup_code"
for p in (DATA, MEDIA, THUMBS, TMP): p.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None

def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def secret_value():
    v = os.getenv("SESSION_SECRET")
    if v: return v
    if SECRET.exists(): return SECRET.read_text(encoding="utf-8").strip()
    v = secrets.token_hex(32)
    SECRET.write_text(v, encoding="utf-8")
    try: SECRET.chmod(0o600)
    except OSError: pass
    return v

def setup_code():
    v = os.getenv("ADMIN_SETUP_CODE")
    if v: return v
    if SETUP.exists(): return SETUP.read_text(encoding="utf-8").strip()
    v = secrets.token_urlsafe(20)
    SETUP.write_text(v, encoding="utf-8")
    try: SETUP.chmod(0o600)
    except OSError: pass
    print("\n[SECURITY] First-run setup code: " + v + "\n")
    return v

app = Flask(__name__)
app.secret_key = secret_value()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024**3))),
)

with connect() as c:
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS videos(
        id TEXT PRIMARY KEY,title TEXT NOT NULL,source_url TEXT NOT NULL,source TEXT NOT NULL,
        filename TEXT NOT NULL,mime_type TEXT NOT NULL,filesize INTEGER NOT NULL DEFAULT 0,
        duration REAL,width INTEGER,height INTEGER,uploader TEXT,thumbnail TEXT,
        created_at TEXT NOT NULL,expires_at TEXT
    );
    CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY,url TEXT NOT NULL,quality TEXT NOT NULL,container TEXT NOT NULL,
        ttl_hours INTEGER,status TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,
        title TEXT,error TEXT,video_id TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_videos_expires ON videos(expires_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at);
    """)

def setting(k):
    with connect() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else None

def configured():
    return bool(setting("admin_password_hash"))

def is_admin():
    return session.get("is_admin") is True

def admin_only(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not configured(): return redirect(url_for("setup"))
        if not is_admin(): return redirect(url_for("login", next=request.full_path))
        return view(*a, **kw)
    return wrapped

@app.before_request
def auth_gate():
    if configured():
        if not is_admin() and request.endpoint not in {"login","setup","static"}:
            return redirect(url_for("login", next=request.full_path))
    elif request.endpoint not in {"setup","static"}:
        return redirect(url_for("setup"))

def ttl_value(raw):
    if raw in (None, "", "never"): return None
    hours = int(raw)
    if hours <= 0 or hours > 24 * 365 * 100: raise ValueError("TTL должен быть от 1 часа до 100 лет.")
    return hours

def expires(ttl):
    return iso(now() + timedelta(hours=ttl)) if ttl else None

def source_name(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if "youtube.com" in host or "youtu.be" in host: return "YouTube"
    if "pornhub.com" in host: return "PornHub"
    return None

def validate_url(url):
    import urllib.parse
    p = urllib.parse.urlparse(url)
    if p.scheme not in {"http","https"} or not p.netloc: raise ValueError("Нужен полноценный http(s) URL.")
    source = source_name(url)
    if not source: raise ValueError("Поддерживаются только YouTube и PornHub.")
    return source

def ydl_base():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": int(os.getenv("YTDLP_SOCKET_TIMEOUT","30"))}
    cookie = os.getenv("YTDLP_COOKIEFILE")
    if cookie: o["cookiefile"] = cookie
    ua = os.getenv("YTDLP_USER_AGENT")
    if ua: o["http_headers"] = {"User-Agent": ua}
    return o

def info_for(url):
    o = ydl_base(); o["skip_download"] = True
    with yt_dlp.YoutubeDL(o) as ydl: return ydl.extract_info(url, download=False)

def qualities(info):
    hs = sorted({f.get("height") for f in (info.get("formats") or []) if isinstance(f.get("height"), int) and f.get("height") > 0 and f.get("height") <= 4320})
    preferred = [x for x in (360,480,720,1080,1440,2160) if x in hs]
    return sorted(set(preferred + [x for x in hs if x not in preferred]))

def fmt_for(q, container):
    h = None if q in ("best","",None) else int(q)
    if container == "mp4":
        v = ("bestvideo[ext=mp4][height<=%s]" % h) if h else "bestvideo[ext=mp4]"
        a = "bestaudio[ext=m4a]/bestaudio"
        f = ("best[ext=mp4][height<=%s]/best[height<=%s]" % (h,h)) if h else "best[ext=mp4]/best"
    else:
        v = ("bestvideo[ext=webm][height<=%s]" % h) if h else "bestvideo[ext=webm]"
        a = "bestaudio[ext=webm]/bestaudio"
        f = ("best[ext=webm][height<=%s]/best[height<=%s]" % (h,h)) if h else "best[ext=webm]/best"
    return v + "+" + a + "/" + f

def find_result(folder):
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in {".part",".ytdl",".json"}]
    if not files: raise FileNotFoundError("yt-dlp не создал итоговый файл.")
    return max(files, key=lambda p:p.stat().st_mtime)

def run_job(job_id):
    with connect() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job: return
        c.execute("UPDATE jobs SET status='downloading',started_at=? WHERE id=?", (iso(now()),job_id))
    folder = TMP / job_id; folder.mkdir(parents=True, exist_ok=True)
    def hook(d):
        pct = 0.0
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes")
            if total and got: pct = min(99.0, got * 100 / total)
        elif d.get("status") == "finished": pct = 99.0
        with connect() as c: c.execute("UPDATE jobs SET progress=? WHERE id=?", (pct,job_id))
    try:
        o = ydl_base()
        o.update({"format":fmt_for(job["quality"],job["container"]),
                  "outtmpl":str(folder / "%(title).180s [%(id)s].%(ext)s"),
                  "progress_hooks":[hook],"merge_output_format":job["container"]})
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(job["url"], download=True)
        src = find_result(folder)
        vid = uuid.uuid4().hex
        final = MEDIA / (vid + src.suffix.lower())
        shutil.move(str(src), str(final))
        thumb = None
        if info.get("thumbnail"):
            tp = THUMBS / (vid + ".jpg")
            try:
                urllib.request.urlretrieve(info["thumbnail"], tp); thumb = tp.name
            except Exception: pass
        with connect() as c:
            c.execute("""INSERT INTO videos(
                id,title,source_url,source,filename,mime_type,filesize,duration,width,height,uploader,thumbnail,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (vid,(info.get("title") or final.stem).strip(),job["url"],source_name(job["url"]),
                 final.name,"video/" + final.suffix.lstrip("."),final.stat().st_size,info.get("duration"),
                 info.get("width"),info.get("height"),info.get("uploader") or info.get("channel"),thumb,
                 iso(now()),expires(job["ttl_hours"])))
            c.execute("UPDATE jobs SET status='done',progress=100,title=?,video_id=?,finished_at=? WHERE id=?",
                      (info.get("title") or final.stem,vid,iso(now()),job_id))
    except Exception as e:
        msg = str(e)[-1600:]
        with connect() as c: c.execute("UPDATE jobs SET status='error',error=?,finished_at=? WHERE id=?",(msg,iso(now()),job_id))
    finally:
        shutil.rmtree(folder, ignore_errors=True)

def worker():
    while True:
        with connect() as c:
            r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if r: run_job(r["id"])
        else: time.sleep(1)

def cleaner():
    while True:
        with connect() as c:
            rows = c.execute("SELECT id,filename,thumbnail FROM videos WHERE expires_at IS NOT NULL AND expires_at <= ?", (iso(now()),)).fetchall()
            for r in rows:
                (MEDIA / r["filename"]).unlink(missing_ok=True)
                if r["thumbnail"]: (THUMBS / r["thumbnail"]).unlink(missing_ok=True)
                c.execute("DELETE FROM videos WHERE id=?", (r["id"],))
        time.sleep(60)

threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=cleaner, daemon=True).start()

@app.route("/setup", methods=["GET","POST"])
def setup():
    if configured(): return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        code = request.form.get("setup_code",""); p1 = request.form.get("password",""); p2 = request.form.get("password2","")
        if not hmac.compare_digest(code, setup_code()): error = "Неверный код первичной настройки."
        elif len(p1) < 8: error = "Пароль должен быть минимум 8 символов."
        elif p1 != p2: error = "Пароли не совпадают."
        else:
            method = "scrypt" if hasattr(hashlib,"scrypt") else "pbkdf2:sha256:600000"
            with connect() as c: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('admin_password_hash',?)",(generate_password_hash(p1,method=method),))
            if not os.getenv("ADMIN_SETUP_CODE"): SETUP.unlink(missing_ok=True)
            session.clear(); session["is_admin"]=True
            return redirect(url_for("index"))
    return render_template_string(PAGE, page="setup", error=error)

@app.route("/login", methods=["GET","POST"])
def login():
    if not configured(): return redirect(url_for("setup"))
    error=None; nxt=request.form.get("next") or request.args.get("next") or "/"
    if request.method=="POST":
        if check_password_hash(setting("admin_password_hash"), request.form.get("password","")):
            session.clear(); session["is_admin"]=True
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        error="Неверный пароль."
    return render_template_string(PAGE, page="login", error=error, next_url=nxt)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/")
def index():
    with connect() as c:
        videos=c.execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall()
        jobs=c.execute("SELECT * FROM jobs WHERE status IN ('queued','downloading','error') ORDER BY created_at DESC LIMIT 30").fetchall()
    return render_template_string(PAGE, page="index", videos=videos, jobs=jobs)

@app.route("/api/formats")
def api_formats():
    try:
        url=request.args.get("url","").strip(); src=validate_url(url); info=info_for(url)
        hs=qualities(info)
        return jsonify(ok=True,source=src,title=info.get("title"),thumbnail=info.get("thumbnail"),
                       qualities=[{"value":str(h),"label":"до %sp" % h} for h in hs]+[{"value":"best","label":"максимум"}])
    except Exception as e:
        return jsonify(ok=False,error=str(e)),400

@app.route("/api/download", methods=["POST"])
def api_download():
    data=request.get_json(silent=True) or request.form
    url=str(data.get("url","")).strip(); q=str(data.get("quality","best")); container=str(data.get("container","mp4")).lower()
    try:
        validate_url(url); ttl=ttl_value(str(data.get("ttl_hours","12")))
        if q!="best" and not (1 <= int(q) <= 4320): raise ValueError("Некорректное разрешение.")
        jid=uuid.uuid4().hex
        with connect() as c:
            c.execute("INSERT INTO jobs(id,url,quality,container,ttl_hours,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                      (jid,url,q,container,ttl,iso(now())))
        return jsonify(ok=True,job_id=jid)
    except Exception as e:
        return jsonify(ok=False,error=str(e)),400

@app.route("/api/jobs")
def api_jobs():
    with connect() as c: rows=c.execute("SELECT id,url,quality,container,status,progress,title,error,video_id,created_at,finished_at FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return jsonify(ok=True,jobs=[dict(r) for r in rows])

@app.route("/api/videos/<vid>", methods=["DELETE"])
@admin_only
def api_delete(vid):
    with connect() as c:
        r=c.execute("SELECT filename,thumbnail FROM videos WHERE id=?",(vid,)).fetchone()
        if not r: return jsonify(ok=False,error="Видео не найдено."),404
        c.execute("DELETE FROM videos WHERE id=?",(vid,))
    (MEDIA/r["filename"]).unlink(missing_ok=True)
    if r["thumbnail"]: (THUMBS/r["thumbnail"]).unlink(missing_ok=True)
    return jsonify(ok=True)

@app.route("/api/upload", methods=["POST"])
@admin_only
def api_upload():
    f=request.files.get("file")
    if not f or not f.filename: return jsonify(ok=False,error="Файл не выбран."),400
    if Path(secure_filename(f.filename)).suffix.lower() not in {".mp4",".webm",".mkv",".mov",".avi"}:
        return jsonify(ok=False,error="Разрешены только видеофайлы."),400
    ttl=ttl_value(request.form.get("ttl_hours","12")); vid=uuid.uuid4().hex
    suffix=Path(secure_filename(f.filename)).suffix.lower(); name=vid+suffix; path=MEDIA/name; f.save(path)
    with connect() as c:
        c.execute("""INSERT INTO videos(id,title,source_url,source,filename,mime_type,filesize,created_at,expires_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (vid,Path(f.filename).stem,"local-upload","Local",name,"video/"+suffix.lstrip("."),path.stat().st_size,iso(now()),expires(ttl)))
    return jsonify(ok=True,video_id=vid)

@app.route("/video/<vid>")
def video_page(vid):
    with connect() as c: v=c.execute("SELECT * FROM videos WHERE id=?",(vid,)).fetchone()
    if not v: abort(404)
    return render_template_string(PAGE,page="video",video=v)

@app.route("/media/<vid>")
def media(vid):
    with connect() as c: v=c.execute("SELECT filename FROM videos WHERE id=?",(vid,)).fetchone()
    if not v: abort(404)
    p=MEDIA/v["filename"]
    if not p.is_file(): abort(404)
    return send_file(p,conditional=True)

@app.route("/download/<vid>")
def download(vid):
    with connect() as c: v=c.execute("SELECT filename,title FROM videos WHERE id=?",(vid,)).fetchone()
    if not v: abort(404)
    p=MEDIA/v["filename"]
    if not p.is_file(): abort(404)
    return send_file(p,as_attachment=True,download_name=secure_filename(v["title"])+p.suffix)

@app.route("/thumb/<vid>")
def thumb(vid):
    with connect() as c: v=c.execute("SELECT thumbnail FROM videos WHERE id=?",(vid,)).fetchone()
    if not v or not v["thumbnail"]: abort(404)
    p=THUMBS/v["thumbnail"]
    if not p.is_file(): abort(404)
    return send_file(p,mimetype="image/jpeg",conditional=True)

PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>inhHAB</title><style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#12161b;--line:#29323b;--text:#eef2f5;--muted:#97a1ab;--accent:#79e3b0;--danger:#ff7d7d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#172019 0,#0b0d10 45%);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:inherit;text-decoration:none}.wrap{max-width:1300px;margin:auto;padding:28px 20px 60px}nav,.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.brand{font-weight:900;letter-spacing:.08em}.pill{padding:7px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.panel,.card{background:rgba(18,22,27,.92);border:1px solid var(--line);border-radius:16px;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#080a0c;border-radius:11px;display:block}.title{font-weight:800;margin:10px 0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.muted{color:var(--muted);font-size:13px}
.controls{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:10px}input,select,button{font:inherit;border-radius:10px;border:1px solid var(--line);background:#0c1014;color:var(--text);padding:11px 12px}button{cursor:pointer}button.primary{background:var(--accent);color:#08100c;border-color:var(--accent);font-weight:900}button.danger{background:transparent;color:var(--danger);border-color:#56343a}.notice{padding:10px 12px;border-radius:10px;background:#171d23;color:var(--muted)}.error{color:#ff9898}.status{font-size:12px;text-transform:uppercase;letter-spacing:.08em}.progress{height:7px;background:#20262d;border-radius:9px;overflow:hidden}.progress i{display:block;height:100%;background:var(--accent)}video{width:100%;max-height:75vh;background:#000;border-radius:14px}.mt{margin-top:20px}
@media(max-width:850px){.controls{grid-template-columns:1fr 1fr}.controls input:first-child{grid-column:1/-1}}
</style></head><body><div class="wrap">
<nav><div class="brand">inhHAB</div><div class="row"><span class="pill">127.0.0.1:1616</span>{% if page not in ['login','setup'] %}<a class="pill" href="/logout">выйти</a>{% endif %}</div></nav>
{% if page=='setup' %}
<div class="panel" style="max-width:560px;margin:80px auto"><h1>Первичная настройка</h1><p class="muted">Код печатается в терминале.</p>{% if error %}<p class="error">{{error}}</p>{% endif %}<form method="post">
<input name="setup_code" placeholder="Код из терминала" required><input name="password" type="password" placeholder="Пароль" minlength="8" required><input name="password2" type="password" placeholder="Повтор" minlength="8" required><button class="primary">Создать администратора</button></form></div>
{% elif page=='login' %}
<div class="panel" style="max-width:460px;margin:80px auto"><h1>Вход администратора</h1>{% if error %}<p class="error">{{error}}</p>{% endif %}<form method="post"><input type="hidden" name="next" value="{{next_url}}"><input name="password" type="password" placeholder="Пароль" autofocus required><button class="primary">Войти</button></form></div>
{% elif page=='video' %}
<a class="pill" href="/">← назад</a><div class="panel mt"><h1>{{video['title']}}</h1><video controls preload="metadata" src="/media/{{video['id']}}"></video><div class="row mt"><span class="muted">{{video['source']}} · {{video['height'] or '?'}}p · {{(video['filesize']/1024/1024)|round(1)}} MB</span><a class="pill" href="/download/{{video['id']}}">скачать</a></div></div>
{% else %}
<div class="panel"><h1>Скачать видео</h1><p class="muted">YouTube и PornHub. URL → доступные качества → очередь → локальное хранение с TTL.</p>
<div class="controls"><input id="url" placeholder="https://www.youtube.com/watch?v=..."><select id="quality"><option value="best">Максимум</option></select><select id="container"><option value="mp4">MP4</option><option value="webm">WebM</option></select><select id="ttl"><option value="12">12 часов</option><option value="24">24 часа</option><option value="72">3 дня</option><option value="168">7 дней</option><option value="720">30 дней</option><option value="never">Бессрочно</option></select><button class="primary" onclick="startDownload()">скачать</button></div>
<div id="preview" class="notice mt">Вставь URL и уйди по своим человеческим делам: список форматов подтянется сам.</div></div>
<div class="panel mt"><div class="row"><h2>Очередь</h2><span class="muted">автообновление</span></div><div id="jobs"></div></div>
<div class="row mt"><h2>Видео на сервере</h2><span class="muted">{{videos|length}} шт.</span></div>
<div class="grid">{% for v in videos %}<a class="card" href="/video/{{v['id']}}">{% if v['thumbnail'] %}<img class="thumb" src="/thumb/{{v['id']}}">{% else %}<div class="thumb"></div>{% endif %}<div class="title">{{v['title']}}</div><div class="muted">{{v['source']}} · {{v['height'] or '?'}}p · {{(v['filesize']/1024/1024)|round(1)}} MB</div><div class="muted">истекает: {% if v['expires_at'] %}{{v['expires_at']}}{% else %}никогда{% endif %}</div><button class="danger" style="margin-top:10px;width:100%" onclick="deleteVideo(event,'{{v['id']}}')">удалить</button></a>{% else %}<div class="panel"><span class="muted">Видео пока нет.</span></div>{% endfor %}</div>
<div class="panel mt"><h2>Ручная загрузка</h2><form id="uploadForm"><input name="file" type="file" accept="video/*" required><select name="ttl_hours"><option value="12">12 часов</option><option value="24">24 часа</option><option value="168">7 дней</option><option value="never">Бессрочно</option></select><button>загрузить</button></form></div>
<script>
const q=s=>document.querySelector(s); let last='';
q('#url').addEventListener('blur',formats); q('#url').addEventListener('change',formats);
async function formats(){const u=q('#url').value.trim();if(!u||u===last)return;last=u;q('#preview').textContent='получаю форматы…';try{const r=await fetch('/api/formats?url='+encodeURIComponent(u)),d=await r.json();if(!d.ok)throw Error(d.error);q('#quality').innerHTML=d.qualities.map(function(x){return '<option value="'+x.value+'">'+x.label+'</option>'}).join('');q('#preview').textContent=d.title||u}catch(e){q('#preview').textContent=e.message}}
async function startDownload(){try{const r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:q('#url').value.trim(),quality:q('#quality').value,container:q('#container').value,ttl_hours:q('#ttl').value})}),d=await r.json();if(!d.ok)throw Error(d.error);q('#preview').textContent='добавлено в очередь';poll()}catch(e){q('#preview').textContent=e.message}}
async function poll(){const r=await fetch('/api/jobs'),d=await r.json(),box=q('#jobs');if(!d.jobs.length){box.innerHTML='<p class="muted">очередь пуста</p>';return}box.innerHTML=d.jobs.map(function(j){return '<div class="card" style="margin-top:10px"><div class="row"><b>'+esc(j.title||j.url)+'</b><span class="status">'+j.status+'</span></div><div class="muted">'+esc(j.quality)+' · '+esc(j.container)+(j.error?' · '+esc(j.error):'')+'</div><div class="progress" style="margin-top:8px"><i style="width:'+j.progress+'%"></i></div></div>'}).join('')}
async function deleteVideo(e,id){e.preventDefault();e.stopPropagation();if(!confirm('Удалить видео с сервера?'))return;const r=await fetch('/api/videos/'+id,{method:'DELETE'});if(r.ok)location.reload()}
q('#uploadForm').addEventListener('submit',async function(e){e.preventDefault();const r=await fetch('/api/upload',{method:'POST',body:new FormData(e.target)}),d=await r.json();if(!d.ok)alert(d.error);else location.reload()});
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]})}
poll();setInterval(poll,2000);
</script>
{% endif %}</div></body></html>'''

if __name__ == "__main__":
    host=os.getenv("HOST","127.0.0.1"); port=int(os.getenv("PORT","1616")); threads=int(os.getenv("WAITRESS_THREADS","8"))
    print("inhHAB listening on http://%s:%s" % (host,port))
    serve(app,host=host,port=port,threads=threads)
