import logging
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

if sys.version_info < (3, 11):
    raise RuntimeError("inhHAB requires Python 3.11 or newer. Run ./run_mac.sh to recreate the virtual environment.")

import yt_dlp
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("inhHAB")

BASE=Path(__file__).resolve().parent; DATA=BASE/"data"; MEDIA=DATA/"media"; THUMBS=DATA/"thumbs"; TMP=DATA/"tmp"; DB=DATA/"inhhab.db"; SECRET=DATA/".session_secret"; SETUP=DATA/".setup_code"
for p in (DATA,MEDIA,THUMBS,TMP): p.mkdir(parents=True,exist_ok=True)

def now(): return datetime.now(timezone.utc)
def iso(dt): return dt.astimezone(timezone.utc).isoformat() if dt else None
def connect():
    c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c

def secret_value():
    v=os.getenv("SESSION_SECRET")
    if v:return v
    if SECRET.exists():return SECRET.read_text(encoding="utf-8").strip()
    v=secrets.token_hex(32); SECRET.write_text(v,encoding="utf-8")
    try: SECRET.chmod(0o600)
    except OSError: pass
    return v

def setup_code():
    v=os.getenv("ADMIN_SETUP_CODE")
    if v:return v
    if SETUP.exists():return SETUP.read_text(encoding="utf-8").strip()
    v=secrets.token_urlsafe(20); SETUP.write_text(v,encoding="utf-8")
    try: SETUP.chmod(0o600)
    except OSError: pass
    log.warning("First-run setup code: %s",v)
    return v

# Generate/show the setup code at startup, not after the user has already submitted the form.
# An explicit ADMIN_SETUP_CODE always takes precedence.
if not os.getenv("ADMIN_SETUP_CODE"):
    setup_code()

app=Flask(__name__); app.secret_key=secret_value()
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE","0")=="1",MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_BYTES",str(20*1024**3))))
with connect() as c:
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY,title TEXT NOT NULL,source_url TEXT NOT NULL,source TEXT NOT NULL,filename TEXT NOT NULL,mime_type TEXT NOT NULL,filesize INTEGER NOT NULL DEFAULT 0,duration REAL,width INTEGER,height INTEGER,uploader TEXT,thumbnail TEXT,created_at TEXT NOT NULL,expires_at TEXT);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,url TEXT NOT NULL,quality TEXT NOT NULL,container TEXT NOT NULL,ttl_hours INTEGER,status TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,title TEXT,error TEXT,video_id TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_videos_expires ON videos(expires_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at);
    CREATE TABLE IF NOT EXISTS bot_users(platform TEXT NOT NULL,user_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(platform,user_id));
    CREATE TABLE IF NOT EXISTS bot_requests(job_id TEXT PRIMARY KEY,platform TEXT NOT NULL,user_id TEXT NOT NULL);
    """)

def setting(k):
    with connect() as c:r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
    return r["value"] if r else None
def configured():return bool(setting("admin_password_hash"))
def set_setting(k,v):
    with connect() as c:c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(k,str(v)))
def bot_access_key():
    v=setting("bot_access_key")
    if v:return v
    v=secrets.token_urlsafe(18);set_setting("bot_access_key",v);return v

def bot_enabled(platform):return setting(platform+"_enabled")=="1"

def http_json(url,payload=None,timeout=35):
    data=json.dumps(payload or {}).encode("utf-8")
    req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json","User-Agent":"inhHAB/1.0"})
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode("utf-8"))

def bot_user_allowed(platform,user_id):
    with connect() as c:r=c.execute("SELECT 1 FROM bot_users WHERE platform=? AND user_id=?",(platform,str(user_id))).fetchone()
    return bool(r)

def bot_authorize(platform,user_id,key):
    if not hmac.compare_digest(str(key or ""),bot_access_key()):return False
    with connect() as c:c.execute("INSERT OR IGNORE INTO bot_users(platform,user_id,created_at) VALUES(?,?,?)",(platform,str(user_id),iso(now())))
    return True

def bot_register_request(job_id,platform,user_id):
    with connect() as c:c.execute("INSERT OR REPLACE INTO bot_requests(job_id,platform,user_id) VALUES(?,?,?)",(job_id,platform,str(user_id)))

def telegram_call(token,method,payload):return http_json("https://api.telegram.org/bot%s/%s"%(token,method),payload)
def telegram_send(token,chat_id,text):return telegram_call(token,"sendMessage",{"chat_id":chat_id,"text":text})

def vk_api(token,method,payload):
    p=dict(payload or {});p.update({"access_token":token,"v":"5.199"})
    data=urllib.parse.urlencode(p).encode("utf-8")
    req=urllib.request.Request("https://api.vk.com/method/"+method,data=data,headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"inhHAB/1.0"})
    with urllib.request.urlopen(req,timeout=35) as r:return json.loads(r.read().decode("utf-8"))
def vk_send(token,peer_id,text):return vk_api(token,"messages.send",{"peer_id":peer_id,"random_id":0,"message":text})

def notify_bot_request(job_id,text):
    with connect() as c:r=c.execute("SELECT platform,user_id FROM bot_requests WHERE job_id=?",(job_id,)).fetchone()
    if not r:return
    try:
        if r["platform"]=="telegram" and setting("telegram_token"):telegram_send(setting("telegram_token"),r["user_id"],text)
        elif r["platform"]=="vk" and setting("vk_token"):vk_send(setting("vk_token"),r["user_id"],text)
    except Exception as e:log.warning("bot notification failed: %s",e)

def handle_bot_text(platform,chat_id,user_id,text,reply):
    text=(text or "").strip();parts=text.split()
    if not parts:return
    cmd=parts[0].split("@",1)[0].lower()
    if cmd in {"/start","/help","help"}:
        reply("inhHAB bot. Доступ: /access KEY\nСкачать: /download URL [quality] [mp4|webm] [ttl]\nСтатус: /status");return
    if cmd in {"/access","/key"}:
        if len(parts)<2:reply("Использование: /access KEY");return
        reply("Доступ выдан." if bot_authorize(platform,user_id,parts[1]) else "Неверный ключ доступа.");return
    if not bot_user_allowed(platform,user_id):reply("Доступ закрыт. Сначала введи /access KEY.");return
    if cmd=="/status":
        with connect() as c:rows=c.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status").fetchall()
        reply("Статус: "+"; ".join("%s=%s"%(r["status"],r["n"]) for r in rows) or "задач нет");return
    if cmd=="/download":
        if len(parts)<2:reply("Использование: /download URL [quality] [mp4|webm] [ttl]");return
        url=parts[1];q=parts[2] if len(parts)>2 else "best";container=parts[3].lower() if len(parts)>3 else "mp4";ttl=parts[4] if len(parts)>4 else "12"
        try:
            validate_url(url)
            if container not in {"mp4","webm"}:raise ValueError("контейнер должен быть mp4 или webm")
            if q!="best" and not (1<=int(q)<=4320):raise ValueError("некорректное разрешение")
            ttlh=ttl_value(ttl);jid=uuid.uuid4().hex
            with connect() as c:c.execute("INSERT INTO jobs(id,url,quality,container,ttl_hours,status,created_at) VALUES(?,?,?,?,?,'queued',?)",(jid,url,q,container,ttlh,iso(now())))
            bot_register_request(jid,platform,user_id);reply("Задача добавлена: "+jid+"\nСтатус: /status")
        except Exception as e:reply("Ошибка: "+str(e))
        return
    reply("Неизвестная команда. /help")

def telegram_loop():
    offset=None;ready_token=None
    while True:
        token=setting("telegram_token")
        if not token or not bot_enabled("telegram"):ready_token=None;time.sleep(3);continue
        try:
            if token!=ready_token:
                telegram_call(token,"deleteWebhook",{})
                ready_token=token
            payload={"timeout":25,"limit":50};payload.update({"offset":offset} if offset is not None else {})
            data=telegram_call(token,"getUpdates",payload)
            if not data.get("ok"):raise RuntimeError(data.get("description","Telegram API error"))
            for upd in data.get("result",[]):
                offset=upd["update_id"]+1;msg=upd.get("message") or upd.get("edited_message")
                if msg:handle_bot_text("telegram",str(msg["chat"]["id"]),str(msg.get("from",{}).get("id","")),msg.get("text",""),lambda t:telegram_send(token,msg["chat"]["id"],t))
        except Exception as e:log.warning("Telegram bot loop: %s",e);time.sleep(5)

def vk_loop():
    ts=server=key=None
    while True:
        token=setting("vk_token")
        if not token or not bot_enabled("vk"):time.sleep(3);continue
        try:
            if not server:
                g=vk_api(token,"groups.getById",{})
                if "error" in g:raise RuntimeError(str(g["error"]))
                groups=(g.get("response") or {}).get("groups") or []
                if not groups:raise RuntimeError("VK token не привязан к сообществу.")
                group_id=str(groups[0].get("id") or "")
                if not group_id:raise RuntimeError("VK API не вернул ID сообщества.")
                d=vk_api(token,"groups.getLongPollServer",{"group_id":group_id})
                if "error" in d:raise RuntimeError(str(d["error"]))
                server=d["response"]["server"];key=d["response"]["key"];ts=d["response"]["ts"]
            u=server+"?act=a_check&key="+urllib.parse.quote(key)+"&wait=25&ts="+urllib.parse.quote(ts)
            with urllib.request.urlopen(u,timeout=35) as r:data=json.loads(r.read().decode("utf-8"))
            if data.get("failed"):server=None;continue
            ts=data.get("ts",ts)
            for upd in data.get("updates",[]):
                if upd.get("type")!="message_new":continue
                o=upd.get("object") or {};text=o.get("text","");peer=str(o.get("peer_id") or o.get("from_id") or "");uid=str(o.get("from_id") or peer)
                handle_bot_text("vk",peer,uid,text,lambda t:vk_send(token,peer,t))
        except Exception as e:log.warning("VK bot loop: %s",e);server=None;time.sleep(5)
def is_admin():return session.get("is_admin") is True

def admin_only(view):
    @wraps(view)
    def wrapped(*a,**kw):
        if not configured():return redirect(url_for("setup"))
        if not is_admin():return redirect(url_for("login",next=request.full_path))
        return view(*a,**kw)
    return wrapped

@app.before_request
def auth_gate():
    if request.endpoint!="static":
        log.info("%s %s from %s",request.method,request.path,request.remote_addr)
    wants_json=request.path.startswith("/api/")
    if configured():
        if not is_admin() and request.endpoint not in {"login","setup","static"}:
            if wants_json:
                return jsonify(ok=False,error="Требуется вход администратора."),401
            return redirect(url_for("login",next=request.full_path))
    elif request.endpoint not in {"setup","static"}:
        if wants_json:
            return jsonify(ok=False,error="Сначала завершите первичную настройку."),403
        return redirect(url_for("setup"))

def ttl_value(raw):
    if raw in (None,"","never"):return None
    hours=int(raw)
    if hours<=0 or hours>24*365*100:raise ValueError("TTL должен быть от 1 часа до 100 лет.")
    return hours
def expires(ttl):return iso(now()+timedelta(hours=ttl)) if ttl else None
def source_name(url):
    host=(urllib.parse.urlparse(url).hostname or "").lower()
    if host=="youtube.com" or host.endswith(".youtube.com") or host=="youtu.be" or host.endswith(".youtu.be"):return "YouTube"
    if host=="pornhub.com" or host.endswith(".pornhub.com") or host=="pornhub.org" or host.endswith(".pornhub.org"):return "PornHub"
    return None

def canonical_url(url):
    p=urllib.parse.urlparse(url)
    host=(p.hostname or "").lower()
    # PornHub's rt.pornhub.org links point at the same view_video endpoint but
    # are not matched by the PornHub extractor as reliably as the canonical .com host.
    if host=="rt.pornhub.org" or host.endswith(".pornhub.org"):
        return urllib.parse.urlunparse(("https","www.pornhub.com",p.path or "/",p.params,p.query,p.fragment))
    return url

def validate_url(url):
    p=urllib.parse.urlparse(url)
    if p.scheme not in {"http","https"} or not p.netloc:
        raise ValueError("Нужен полноценный http(s) URL.")
    s=source_name(url)
    if not s:
        raise ValueError("Поддерживаются только YouTube и PornHub.")
    if s=="PornHub" and not urllib.parse.parse_qs(p.query).get("viewkey") and "/view_video.php" in p.path:
        raise ValueError("У PornHub-ссылки не найден параметр viewkey.")
    return s

def youtube_fallback_options():
    return {"extractor_args":{"youtube":{"player_client":["default","web_embedded"]}}}

def ydl_base():
    o={"quiet":True,"no_warnings":True,"noplaylist":True,"socket_timeout":int(os.getenv("YTDLP_SOCKET_TIMEOUT","30"))}
    if os.getenv("YTDLP_COOKIEFILE"):o["cookiefile"]=os.getenv("YTDLP_COOKIEFILE")
    if os.getenv("YTDLP_USER_AGENT"):o["http_headers"]={"User-Agent":os.getenv("YTDLP_USER_AGENT")}
    # If Deno is installed, current yt-dlp enables it by default. If only Node
    # is available, explicitly enable Node for the EJS JavaScript challenges.
    if shutil.which("deno"):
        log.info("yt-dlp JS runtime: deno")
    elif shutil.which("node"):
        o["js_runtimes"]={"node":{}}
        log.info("yt-dlp JS runtime: node")
    else:
        log.warning("No supported yt-dlp JS runtime found (install Deno or Node for full YouTube support).")
    return o

def info_for(url):
    normalized=canonical_url(url)
    o=ydl_base();o["skip_download"]=True
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(normalized,download=False)
    except yt_dlp.utils.DownloadError as e:
        if source_name(url)=="YouTube" and "page needs to be reloaded" in str(e).lower():
            log.warning("YouTube extractor retry with player_client=default,web_embedded: %s",normalized)
            o=ydl_base();o["skip_download"]=True;o.update(youtube_fallback_options())
            with yt_dlp.YoutubeDL(o) as ydl:
                return ydl.extract_info(normalized,download=False)
        raise
def qualities(info):
    hs=sorted({f.get("height") for f in (info.get("formats") or []) if isinstance(f.get("height"),int) and 0<f.get("height")<=4320})
    return sorted(set([x for x in (360,480,720,1080,1440,2160) if x in hs]+[x for x in hs if x not in (360,480,720,1080,1440,2160)]))
def fmt_for(q,container):
    h=None if q in ("best","",None) else int(q)
    if container=="mp4":v=("bestvideo[ext=mp4][height<=%s]"%h) if h else "bestvideo[ext=mp4]";a="bestaudio[ext=m4a]/bestaudio";f=("best[ext=mp4][height<=%s]/best[height<=%s]"%(h,h)) if h else "best[ext=mp4]/best"
    else:v=("bestvideo[ext=webm][height<=%s]"%h) if h else "bestvideo[ext=webm]";a="bestaudio[ext=webm]/bestaudio";f=("best[ext=webm][height<=%s]/best[height<=%s]"%(h,h)) if h else "best[ext=webm]/best"
    return v+"+"+a+"/"+f
def find_result(folder):
    files=[p for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in {".part",".ytdl",".json"}]
    if not files:raise FileNotFoundError("yt-dlp не создал итоговый файл.")
    return max(files,key=lambda p:p.stat().st_mtime)

def run_job(job_id):
    with connect() as c:
        job=c.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job:return
        c.execute("UPDATE jobs SET status='downloading',started_at=? WHERE id=?",(iso(now()),job_id))
    log.info("job %s started: %s",job_id,job["url"]);folder=TMP/job_id;folder.mkdir(parents=True,exist_ok=True)
    def hook(d):
        pct=0.0
        if d.get("status")=="downloading":
            total=d.get("total_bytes") or d.get("total_bytes_estimate");got=d.get("downloaded_bytes")
            if total and got:pct=min(99.0,got*100/total)
        elif d.get("status")=="finished":pct=99.0
        with connect() as c:c.execute("UPDATE jobs SET progress=? WHERE id=?",(pct,job_id))
    try:
        target_url=canonical_url(job["url"])
        o=ydl_base();o.update({"format":fmt_for(job["quality"],job["container"]),"outtmpl":str(folder/"%(title).180s [%(id)s].%(ext)s"),"progress_hooks":[hook],"merge_output_format":job["container"]})
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                info=ydl.extract_info(target_url,download=True)
        except yt_dlp.utils.DownloadError as e:
            if source_name(job["url"])=="YouTube" and "page needs to be reloaded" in str(e).lower():
                log.warning("YouTube download retry with player_client=default,web_embedded: %s",target_url)
                o=ydl_base();o.update(youtube_fallback_options());o.update({"format":fmt_for(job["quality"],job["container"]),"outtmpl":str(folder/"%(title).180s [%(id)s].%(ext)s"),"progress_hooks":[hook],"merge_output_format":job["container"]})
                with yt_dlp.YoutubeDL(o) as ydl:
                    info=ydl.extract_info(target_url,download=True)
            else:
                raise
        src=find_result(folder);vid=uuid.uuid4().hex;final=MEDIA/(vid+src.suffix.lower());shutil.move(str(src),str(final));thumb=None
        if info.get("thumbnail"):
            tp=THUMBS/(vid+".jpg")
            try:urllib.request.urlretrieve(info["thumbnail"],tp);thumb=tp.name
            except Exception:pass
        with connect() as c:
            c.execute("INSERT INTO videos(id,title,source_url,source,filename,mime_type,filesize,duration,width,height,uploader,thumbnail,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(vid,(info.get("title") or final.stem).strip(),job["url"],source_name(job["url"]),final.name,"video/"+final.suffix.lstrip("."),final.stat().st_size,info.get("duration"),info.get("width"),info.get("height"),info.get("uploader") or info.get("channel"),thumb,iso(now()),expires(job["ttl_hours"])))
            c.execute("UPDATE jobs SET status='done',progress=100,title=?,video_id=?,finished_at=? WHERE id=?",(info.get("title") or final.stem,vid,iso(now()),job_id))
        notify_bot_request(job_id,"Готово: "+(info.get("title") or final.stem)+"\nВидео: /video/"+vid)
        log.info("job %s completed: %s",job_id,info.get("title") or final.stem)
    except Exception as e:
        msg=str(e)[-1600:];notify_bot_request(job_id,"Ошибка загрузки: "+msg[-1000:]);log.exception("job %s failed",job_id)
        with connect() as c:c.execute("UPDATE jobs SET status='error',error=?,finished_at=? WHERE id=?",(msg,iso(now()),job_id))
    finally:shutil.rmtree(folder,ignore_errors=True)

def worker():
    while True:
        with connect() as c:r=c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if r:run_job(r["id"])
        else:time.sleep(1)
def cleaner():
    while True:
        with connect() as c:
            rows=c.execute("SELECT id,filename,thumbnail FROM videos WHERE expires_at IS NOT NULL AND expires_at<=?",(iso(now()),)).fetchall()
            for r in rows:
                (MEDIA/r["filename"]).unlink(missing_ok=True)
                if r["thumbnail"]:(THUMBS/r["thumbnail"]).unlink(missing_ok=True)
                c.execute("DELETE FROM videos WHERE id=?",(r["id"],))
                log.info("expired video removed: %s",r["id"])
        time.sleep(60)
threading.Thread(target=worker,daemon=True).start();threading.Thread(target=cleaner,daemon=True).start();threading.Thread(target=telegram_loop,daemon=True).start();threading.Thread(target=vk_loop,daemon=True).start()

@app.route("/setup",methods=["GET","POST"])
def setup():
    if configured():return redirect(url_for("index"))
    error=None
    if request.method=="POST":
        code=request.form.get("setup_code","");p1=request.form.get("password","");p2=request.form.get("password2","")
        if not hmac.compare_digest(code,setup_code()):error="Неверный код первичной настройки."
        elif len(p1)<8:error="Пароль должен быть минимум 8 символов."
        elif p1!=p2:error="Пароли не совпадают."
        else:
            method="scrypt" if hasattr(hashlib,"scrypt") else "pbkdf2:sha256:600000"
            with connect() as c:c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('admin_password_hash',?)",(generate_password_hash(p1,method=method),))
            if not os.getenv("ADMIN_SETUP_CODE"):SETUP.unlink(missing_ok=True)
            session.clear();session["is_admin"]=True;log.info("admin setup completed");return redirect(url_for("index"))
    return render_template_string(PAGE,page="setup",error=error)

@app.route("/login",methods=["GET","POST"])
def login():
    if not configured():return redirect(url_for("setup"))
    error=None;nxt=request.form.get("next") or request.args.get("next") or "/"
    if request.method=="POST":
        if check_password_hash(setting("admin_password_hash"),request.form.get("password","")):
            session.clear();session["is_admin"]=True;log.info("admin login successful");return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        log.warning("admin login failed");error="Неверный пароль."
    return render_template_string(PAGE,page="login",error=error,next_url=nxt)
@app.route("/logout")
def logout():log.info("admin logout");session.clear();return redirect(url_for("login"))
@app.route("/")
def index():
    bot_settings={"telegram_enabled":setting("telegram_enabled")=="1","telegram_token":setting("telegram_token") or "","vk_enabled":setting("vk_enabled")=="1","vk_token":setting("vk_token") or "","vk_group_id":setting("vk_group_id") or "","bot_access_key":bot_access_key()}
    with connect() as c:videos=c.execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall();jobs=c.execute("SELECT * FROM jobs WHERE status IN ('queued','downloading','error') ORDER BY created_at DESC LIMIT 30").fetchall()
    return render_template_string(PAGE,page="index",videos=videos,jobs=jobs,bot_settings=bot_settings)
@app.route("/api/bots",methods=["GET","POST"])
@admin_only
def api_bots():
    if request.method=="GET":
        return jsonify(ok=True,telegram_enabled=setting("telegram_enabled")=="1",telegram_token_set=bool(setting("telegram_token")),vk_enabled=setting("vk_enabled")=="1",vk_token_set=bool(setting("vk_token")),vk_group_id=setting("vk_group_id") or "",bot_access_key=bot_access_key())
    data=request.get_json(silent=True) or request.form
    if "telegram_enabled" in data:set_setting("telegram_enabled","1" if str(data.get("telegram_enabled")).lower() in {"1","true","on","yes"} else "0")
    if "telegram_token" in data and str(data.get("telegram_token","")).strip():set_setting("telegram_token",str(data.get("telegram_token")).strip())
    if "vk_enabled" in data:set_setting("vk_enabled","1" if str(data.get("vk_enabled")).lower() in {"1","true","on","yes"} else "0")
    if "vk_token" in data and str(data.get("vk_token","")).strip():set_setting("vk_token",str(data.get("vk_token")).strip())
    if "vk_group_id" in data:set_setting("vk_group_id",str(data.get("vk_group_id","")).strip())
    if "bot_access_key" in data and str(data.get("bot_access_key","")).strip():set_setting("bot_access_key",str(data.get("bot_access_key")).strip())
    if str(data.get("rotate_access_key","")).lower() in {"1","true","yes"}:set_setting("bot_access_key",secrets.token_urlsafe(18))
    log.info("bot settings updated")
    return jsonify(ok=True,bot_access_key=bot_access_key())
@app.route("/api/formats")
@admin_only
def api_formats():
    try:
        url=request.args.get("url","").strip()
        src=validate_url(url)
        info=info_for(url)
        hs=qualities(info)
        qlist=[{"value":str(h),"label":"до %sp"%h} for h in hs]
        qlist.append({"value":"best","label":"Максимум доступного"})
        metadata={
            "title":info.get("title") or "Без названия",
            "thumbnail":info.get("thumbnail"),
            "uploader":info.get("uploader") or info.get("channel"),
            "duration":info.get("duration"),
            "width":info.get("width"),
            "height":info.get("height"),
            "webpage_url":info.get("webpage_url") or canonical_url(url),
            "formats_count":len(info.get("formats") or []),
        }
        log.info("metadata resolved: source=%s title=%r url=%s",src,metadata["title"],url)
        return jsonify(ok=True,source=src,qualities=qlist,metadata=metadata)
    except Exception as e:
        log.warning("metadata lookup failed: %s",e)
        return jsonify(ok=False,error=str(e)),400
@app.route("/api/download",methods=["POST"])
@admin_only
def api_download():
    data=request.get_json(silent=True) or request.form
    url=str(data.get("url","")).strip()
    q=str(data.get("quality","best"))
    container=str(data.get("container","mp4")).lower()
    metadata_url=str(data.get("metadata_url","")).strip()
    try:
        validate_url(url)
        if metadata_url != url:
            raise ValueError("Сначала загрузите метаданные для этого URL.")
        ttl=ttl_value(str(data.get("ttl_hours","12")))
        if container not in {"mp4","webm"}:
            raise ValueError("Неподдерживаемый контейнер.")
        if q!="best" and not (1<=int(q)<=4320):
            raise ValueError("Некорректное разрешение.")
        jid=uuid.uuid4().hex
        with connect() as c:
            c.execute(
                "INSERT INTO jobs(id,url,quality,container,ttl_hours,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                (jid,url,q,container,ttl,iso(now()))
            )
        log.info("queued job %s: %s quality=%s container=%s ttl=%s",jid,url,q,container,ttl or "never")
        return jsonify(ok=True,job_id=jid)
    except Exception as e:
        log.warning("download request rejected: %s",e)
        return jsonify(ok=False,error=str(e)),400
@app.route("/api/jobs")
def api_jobs():
    with connect() as c:rows=c.execute("SELECT id,url,quality,container,status,progress,title,error,video_id,created_at,finished_at FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return jsonify(ok=True,jobs=[dict(r) for r in rows])
@app.route("/api/videos/<vid>",methods=["DELETE"])
@admin_only
def api_delete(vid):
    with connect() as c:
        r=c.execute("SELECT filename,thumbnail FROM videos WHERE id=?",(vid,)).fetchone()
        if not r:return jsonify(ok=False,error="Видео не найдено."),404
        c.execute("DELETE FROM videos WHERE id=?",(vid,))
    log.info("deleted video %s",vid);(MEDIA/r["filename"]).unlink(missing_ok=True)
    if r["thumbnail"]:(THUMBS/r["thumbnail"]).unlink(missing_ok=True)
    return jsonify(ok=True)
@app.route("/api/upload",methods=["POST"])
@admin_only
def api_upload():
    f=request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False,error="Файл не выбран."),400
    safe_name=secure_filename(f.filename)
    suffix=Path(safe_name).suffix.lower()
    if suffix not in {".mp4",".webm",".mkv",".mov",".avi"}:
        return jsonify(ok=False,error="Разрешены только видеофайлы: MP4, WebM, MKV, MOV, AVI."),400
    try:
        ttl=ttl_value(request.form.get("ttl_hours","12"))
        vid=uuid.uuid4().hex
        name=vid+suffix
        path=MEDIA/name
        f.save(path)
        size=path.stat().st_size
        with connect() as c:
            c.execute(
                "INSERT INTO videos(id,title,source_url,source,filename,mime_type,filesize,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (vid,Path(f.filename).stem,"local-upload","Local",name,"video/"+suffix.lstrip("."),size,iso(now()),expires(ttl))
            )
        log.info("uploaded local video %s (%s bytes)",f.filename,size)
        return jsonify(ok=True,video_id=vid)
    except Exception as e:
        if 'path' in locals():
            path.unlink(missing_ok=True)
        log.exception("local upload failed")
        return jsonify(ok=False,error=str(e)),400
@app.route("/video/<vid>")
def video_page(vid):
    with connect() as c:v=c.execute("SELECT * FROM videos WHERE id=?",(vid,)).fetchone()
    if not v:abort(404)
    return render_template_string(PAGE,page="video",video=v)
@app.route("/media/<vid>")
def media(vid):
    with connect() as c:v=c.execute("SELECT filename FROM videos WHERE id=?",(vid,)).fetchone()
    if not v:abort(404)
    p=MEDIA/v["filename"]
    if not p.is_file():abort(404)
    return send_file(p,conditional=True)
@app.route("/download/<vid>")
def download(vid):
    with connect() as c:v=c.execute("SELECT filename,title FROM videos WHERE id=?",(vid,)).fetchone()
    if not v:abort(404)
    p=MEDIA/v["filename"]
    if not p.is_file():abort(404)
    return send_file(p,as_attachment=True,download_name=secure_filename(v["title"])+p.suffix)
@app.route("/thumb/<vid>")
def thumb(vid):
    with connect() as c:v=c.execute("SELECT thumbnail FROM videos WHERE id=?",(vid,)).fetchone()
    if not v or not v["thumbnail"]:abort(404)
    p=THUMBS/v["thumbnail"]
    if not p.is_file():abort(404)
    return send_file(p,mimetype="image/jpeg",conditional=True)

PAGE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>inhHAB</title><style>
body{margin:0;background:#0b0d10;color:#eef2f5;font:15px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1200px;margin:auto;padding:28px}
input,select,button{padding:10px;border-radius:9px;background:#11161b;color:#fff;border:1px solid #29323b}
button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.panel,.card{background:#12161b;border:1px solid #29323b;border-radius:15px;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#080a0c;border-radius:10px}
.meta-thumb{width:220px;max-width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:#080a0c}
.muted{color:#97a1ab;font-size:13px}.row{display:flex;justify-content:space-between;gap:10px;align-items:center}
.controls{display:grid;grid-template-columns:minmax(250px,2fr) 1fr 1fr 1fr auto auto;gap:10px}
.primary{background:#79e3b0;color:#08100c;font-weight:800}.secondary{background:#20262d;color:#fff;font-weight:700}
.error{color:#ff9898}.success{color:#9de6b8}
.progress{height:7px;background:#20262d;border-radius:8px;overflow:hidden}.progress i{display:block;height:100%;background:#79e3b0}
a{color:inherit;text-decoration:none}.mt{margin-top:18px}
.metadata{display:grid;grid-template-columns:220px 1fr;gap:16px;align-items:start}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 12px}
@media(max-width:1000px){.controls{grid-template-columns:1fr 1fr 1fr}.controls input:first-child{grid-column:1/-1}}
@media(max-width:650px){.metadata{grid-template-columns:1fr}.controls{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><div class="row"><b>inhHAB</b><span class="muted">127.0.0.1:1616</span></div>
{% if page=="setup" %}<div class="panel" style="max-width:520px;margin:80px auto"><h1>Первичная настройка</h1><p class="muted">Код можно задать через ADMIN_SETUP_CODE при запуске.</p>{% if error %}<p class="error">{{error}}</p>{% endif %}<form method="post"><input name="setup_code" placeholder="Код настройки" required><input name="password" type="password" placeholder="Пароль" minlength="8" required><input name="password2" type="password" placeholder="Повтор" minlength="8" required><button class="primary">Создать администратора</button></form></div>
{% elif page=="login" %}<div class="panel" style="max-width:420px;margin:80px auto"><h1>Вход</h1>{% if error %}<p class="error">{{error}}</p>{% endif %}<form method="post"><input type="hidden" name="next" value="{{next_url}}"><input name="password" type="password" placeholder="Пароль" required><button class="primary">Войти</button></form></div>
{% elif page=="video" %}<a href="/">← назад</a><div class="panel mt"><h1>{{video["title"]}}</h1><video controls style="width:100%;max-height:75vh" src="/media/{{video["id"]}}"></video><div class="row mt"><span class="muted">{{video["source"]}} · {{video["height"] or "?"}}p</span><a href="/download/{{video["id"]}}">скачать</a></div></div>
{% else %}<div class="panel mt"><h1>Скачать видео</h1>
<div class="controls">
<input id="url" placeholder="https://...">
<select id="quality" disabled><option value="best">Сначала получите метаданные</option></select>
<select id="container"><option value="mp4">MP4</option><option value="webm">WebM</option></select>
<select id="ttl"><option value="12">12 часов</option><option value="24">24 часа</option><option value="72">3 дня</option><option value="168">7 дней</option><option value="720">30 дней</option><option value="never">Бессрочно</option></select>
<button id="metaBtn" class="secondary" type="button" onclick="loadMetadata()">метаданные</button>
<button id="downloadBtn" class="primary" type="button" onclick="startDownload()" disabled>скачать</button>
</div>
<div id="preview" class="muted mt">Вставь URL и отдельно нажми «метаданные», затем выбери качество и скачай видео.</div>
</div>
<div class="panel mt"><div class="row"><h2>Очередь</h2><a href="/logout">выйти</a></div><div id="jobs"></div></div>
<div class="row mt"><h2>Видео на сервере</h2></div>
<div class="grid">{% for v in videos %}<a class="card" href="/video/{{v["id"]}}">{% if v["thumbnail"] %}<img class="thumb" src="/thumb/{{v["id"]}}">{% else %}<div class="thumb"></div>{% endif %}<b>{{v["title"]}}</b><div class="muted">{{v["source"]}} · {{v["height"] or "?"}}p</div><button onclick="delv(event,'{{v["id"]}}')">удалить</button></a>{% else %}<div class="card muted">Видео пока нет.</div>{% endfor %}</div>
<div class="panel mt"><h2>Боты Telegram / VK</h2><p class="muted">Пользователь сначала отправляет боту <b>/access КЛЮЧ</b>, после чего получает доступ к /download и /status.</p><div class="grid"><div><h3>Telegram</h3><label><input id="tgEnabled" type="checkbox" {% if bot_settings.telegram_enabled %}checked{% endif %}> включён</label><input id="tgToken" type="password" placeholder="{% if bot_settings.telegram_token %}токен сохранён, введите новый для замены{% else %}BotFather token{% endif %}" autocomplete="off"></div><div><h3>VK</h3><label><input id="vkEnabled" type="checkbox" {% if bot_settings.vk_enabled %}checked{% endif %}> включён</label><input id="vkToken" type="password" placeholder="{% if bot_settings.vk_token %}токен сохранён, введите новый для замены{% else %}токен сообщества{% endif %}"></div></div><div class="row mt"><div><b>Ключ доступа ботов</b><div class="muted">Его вводят пользователи командой /access КЛЮЧ.</div></div><input id="botKey" value="{{bot_settings.bot_access_key}}" style="flex:1" autocomplete="off"><button type="button" onclick="rotateBotKey()">новый ключ</button><button type="button" class="primary" onclick="saveBots()">сохранить</button></div><div id="botStatus" class="muted mt"></div></div><div class="panel mt"><h2>Ручная загрузка</h2><form id="upload"><input name="file" type="file" accept="video/*" required><select name="ttl_hours"><option value="12">12 часов</option><option value="24">24 часа</option><option value="168">7 дней</option><option value="never">Бессрочно</option></select><button>загрузить</button></form></div>
<script>
const q=s=>document.querySelector(s);
const urlEl=q("#url"),qualityEl=q("#quality"),containerEl=q("#container"),ttlEl=q("#ttl"),metaBtn=q("#metaBtn"),downloadBtn=q("#downloadBtn"),preview=q("#preview"),jobsEl=q("#jobs");
let metadataUrl="";

function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]))}
function durationText(sec){if(!Number.isFinite(Number(sec)))return "—";let n=Math.max(0,Math.round(Number(sec))),h=Math.floor(n/3600),m=Math.floor((n%3600)/60),s=n%60;return h?String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(s).padStart(2,"0"):String(m).padStart(2,"0")+":"+String(s).padStart(2,"0")}
function setMetaState(ready){downloadBtn.disabled=!ready;qualityEl.disabled=!ready}
function invalidateMetadata(){metadataUrl="";setMetaState(false);qualityEl.innerHTML='<option value="best">Сначала получите метаданные</option>';preview.className="muted mt";preview.textContent="URL изменён. Снова получите метаданные перед скачиванием."}
function sameMetadataUrl(){return metadataUrl===urlEl.value.trim()&&metadataUrl!==""}

urlEl.addEventListener("input",()=>{if(metadataUrl!==urlEl.value.trim())invalidateMetadata()});

async function apiFetch(url,options){
    const r=await fetch(url,options);
    const text=await r.text();
    let d={};
    try{d=JSON.parse(text)}catch(e){throw Error("Сервер вернул неожиданный ответ ("+r.status+").")}
    if(!r.ok||d.ok===false)throw Error(d.error||("HTTP "+r.status));
    return d;
}

async function loadMetadata(){
    const u=urlEl.value.trim();
    if(!u){invalidateMetadata();preview.className="error mt";preview.textContent="Сначала вставь URL.";return}
    metaBtn.disabled=true;downloadBtn.disabled=true;qualityEl.disabled=true;
    preview.className="muted mt";preview.textContent="получаю метаданные…";
    try{
        const d=await apiFetch("/api/formats?url="+encodeURIComponent(u));
        qualityEl.innerHTML=d.qualities.map(x=>'<option value="'+esc(x.value)+'">'+esc(x.label)+'</option>').join("");
        const m=d.metadata||{};
        const thumb=m.thumbnail?'<img class="meta-thumb" src="'+esc(m.thumbnail)+'" alt="thumbnail">':'<div class="meta-thumb"></div>';
        preview.className="mt";
        preview.innerHTML='<div class="metadata">'+thumb+'<div><h3 style="margin-top:0">'+esc(m.title||u)+'</h3><div class="kv"><span class="muted">Источник</span><span>'+esc(d.source)+'</span><span class="muted">Автор</span><span>'+esc(m.uploader||"—")+'</span><span class="muted">Длительность</span><span>'+durationText(m.duration)+'</span><span class="muted">Разрешение</span><span>'+esc((m.width&&m.height)?m.width+"×"+m.height:"—")+'</span><span class="muted">Форматов</span><span>'+esc(m.formats_count||0)+'</span></div></div></div>';
        metadataUrl=u;setMetaState(true);
    }catch(e){
        metadataUrl="";setMetaState(false);preview.className="error mt";preview.textContent=e.message;
    }finally{metaBtn.disabled=false}
}

async function startDownload(){
    const u=urlEl.value.trim();
    if(!u||!sameMetadataUrl()){preview.className="error mt";preview.textContent="Сначала получите метаданные именно для текущего URL.";return}
    downloadBtn.disabled=true;
    try{
        const d=await apiFetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:u,metadata_url:metadataUrl,quality:qualityEl.value,container:containerEl.value,ttl_hours:ttlEl.value})});
        preview.className="success mt";preview.textContent="Добавлено в очередь. Скачивание выполняется в фоне.";
        poll();
    }catch(e){
        preview.className="error mt";preview.textContent=e.message;
    }finally{downloadBtn.disabled=false}
}

function jobStatus(s){return ({queued:"в очереди",downloading:"скачивается",done:"готово",error:"ошибка"})[s]||s}
async function poll(){
    try{
        const d=await apiFetch("/api/jobs");
        jobsEl.innerHTML=d.jobs.map(j=>{
            const err=j.error?'<div class="error">'+esc(j.error)+'</div>':"";
            const link=j.video_id?'<div class="mt"><a href="/video/'+encodeURIComponent(j.video_id)+'">открыть видео</a></div>':"";
            return '<div class="card mt"><div class="row"><b>'+esc(j.title||j.url)+'</b><span>'+esc(jobStatus(j.status))+'</span></div><div class="muted">'+esc(j.quality)+" · "+esc(j.container)+" · "+esc(j.progress)+"%"+(j.error?" · ошибка":"")+'</div><div class="progress"><i style="width:'+Math.max(0,Math.min(100,Number(j.progress)||0))+'%"></i></div>'+err+link+'</div>';
        }).join("")||'<span class="muted">очередь пуста</span>';
    }catch(e){jobsEl.innerHTML='<div class="error">'+esc(e.message)+'</div>'}
}

async function saveBots(){
 const d=await apiFetch("/api/bots",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({telegram_enabled:q("#tgEnabled").checked,telegram_token:q("#tgToken").value,vk_enabled:q("#vkEnabled").checked,vk_token:q("#vkToken").value,bot_access_key:q("#botKey").value})});
 q("#botKey").value=d.bot_access_key;q("#tgToken").value="";q("#vkToken").value="";q("#botStatus").textContent="Настройки сохранены.";
}
async function rotateBotKey(){
 const d=await apiFetch("/api/bots",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rotate_access_key:true})});
 q("#botKey").value=d.bot_access_key;q("#botStatus").textContent="Ключ заменён.";
}async function delv(e,id){
    e.preventDefault();e.stopPropagation();
    if(!confirm("Удалить видео с сервера?"))return;
    try{await apiFetch("/api/videos/"+encodeURIComponent(id),{method:"DELETE"});location.reload()}catch(err){alert(err.message)}
}

q("#upload").addEventListener("submit",async e=>{
    e.preventDefault();
    const btn=e.target.querySelector("button");btn.disabled=true;
    try{await apiFetch("/api/upload",{method:"POST",body:new FormData(e.target)});location.reload()}
    catch(err){alert(err.message);btn.disabled=false}
});

poll();setInterval(poll,2000);
</script>{% endif %}</div></body></html>'''


if __name__=="__main__":
    host=os.getenv("HOST","127.0.0.1");port=int(os.getenv("PORT","1616"));threads=int(os.getenv("WAITRESS_THREADS","8"))
    log.info("inhHAB listening on http://%s:%s",host,port)
    log.info("Python: %s | yt-dlp: %s",sys.version.split()[0],getattr(yt_dlp,"version","unknown"))
    log.info("ADMIN_SETUP_CODE: %s", "configured" if os.getenv("ADMIN_SETUP_CODE") else "auto-generated on first setup")
    serve(app,host=host,port=port,threads=threads)
