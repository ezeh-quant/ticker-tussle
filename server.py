from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import json, math, os, re, secrets, sqlite3, threading, time

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ARENA_DB_PATH", ROOT / "ticker-tussle.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PRICE_SOURCES = (
    ("Coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/ticker", "coinbase"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true", "coingecko"),
)
DURATIONS = {30_000, 60_000, 120_000}
MAX_PLAYERS = 12
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DB_LOCK = threading.RLock()
PRICE_LOCK = threading.Lock()
PRICE_CACHE = {"until": 0.0, "body": None, "error_until": 0.0}

@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def initialize_database():
    with DB_LOCK, database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY,host_token TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('lobby','running','finished')),round_number INTEGER NOT NULL DEFAULT 0,duration_ms INTEGER NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS players(token TEXT PRIMARY KEY,room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,name TEXT NOT NULL,score REAL NOT NULL DEFAULT 0,joined_at REAL NOT NULL,last_seen REAL NOT NULL,is_host INTEGER NOT NULL DEFAULT 0,UNIQUE(room_code,name COLLATE NOCASE));
        CREATE TABLE IF NOT EXISTS rounds(room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,number INTEGER NOT NULL,start_price REAL NOT NULL,started_at REAL NOT NULL,deadline REAL NOT NULL,actual REAL,resolved_at REAL,intermission_until REAL,PRIMARY KEY(room_code,number));
        CREATE TABLE IF NOT EXISTS forecasts(room_code TEXT NOT NULL,round_number INTEGER NOT NULL,player_token TEXT NOT NULL REFERENCES players(token),prediction REAL NOT NULL,error_pct REAL,points REAL,submitted_at REAL NOT NULL,PRIMARY KEY(room_code,round_number,player_token),FOREIGN KEY(room_code,round_number) REFERENCES rounds(room_code,number));
        CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,player_token TEXT NOT NULL REFERENCES players(token),name TEXT NOT NULL,body TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_players_room ON players(room_code);
        CREATE INDEX IF NOT EXISTS idx_chat_room_id ON chat_messages(room_code,id);
        """)
        try:
            db.execute("ALTER TABLE rounds ADD COLUMN intermission_until REAL")
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error).lower():
                raise

def market_price(force=False):
    now = time.monotonic()
    with PRICE_LOCK:
        if not force and PRICE_CACHE["body"] is not None and now < PRICE_CACHE["until"]:
            return PRICE_CACHE["body"]
        if not force and now < PRICE_CACHE["error_until"]:
            raise RuntimeError("BTC/USD price is temporarily unavailable")
        last_error = None
        for source, upstream, provider in PRICE_SOURCES:
            try:
                def fetch_json(url):
                    request = Request(url, headers={"Accept":"application/json","User-Agent":"TickerTussle/1.0"})
                    with urlopen(request, timeout=4) as response:
                        return json.loads(response.read().decode("utf-8"))
                payload = fetch_json(upstream)
                if provider == "coinbase":
                    stats = fetch_json("https://api.exchange.coinbase.com/products/BTC-USD/stats")
                    price = float(payload["price"])
                    open_24h = float(stats["open"])
                    if not math.isfinite(open_24h) or open_24h <= 0:
                        raise ValueError("Invalid BTC/USD 24-hour open")
                    change_24h_pct = (price / open_24h - 1.0) * 100.0
                else:
                    price = float(payload["bitcoin"]["usd"])
                    raw_change = payload["bitcoin"].get("usd_24h_change")
                    change_24h_pct = float(raw_change) if raw_change is not None else None
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("Invalid BTC/USD quote")
                if change_24h_pct is not None and not math.isfinite(change_24h_pct):
                    change_24h_pct = None
                body = {"price":price,"source":source,"change_24h_pct":change_24h_pct,"as_of":time.time()}
                PRICE_CACHE.update(body=body,until=now+15,error_until=0)
                return body
            except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError,UnicodeDecodeError) as error:
                last_error = error
                print(f"BTC/USD provider unavailable: {source} ({type(error).__name__})")
        PRICE_CACHE.update(error_until=now+5)
        raise RuntimeError("BTC/USD price is temporarily unavailable") from last_error

def resolve_if_due(code):
    now = time.time()
    with DB_LOCK, database() as db:
        room = db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone()
        if room is None or room["status"] != "running": return
        current = db.execute("SELECT * FROM rounds WHERE room_code=? AND number=?",(code,room["round_number"])).fetchone()
        if current is None: return
        if current["actual"] is not None:
            number = int(current["number"])
            if number >= 20: return
            intermission_until = current["intermission_until"]
            if intermission_until is None:
                db.execute("UPDATE rounds SET intermission_until=? WHERE room_code=? AND number=?",(now+20,code,number))
                return
            if now < intermission_until: return
            next_number = number+1
            actual = float(current["actual"])
            db.execute("UPDATE rooms SET round_number=? WHERE code=?",(next_number,code))
            db.execute("INSERT INTO rounds(room_code,number,start_price,started_at,deadline) VALUES(?,?,?,?,?)",(code,next_number,actual,now,now+room["duration_ms"]/1000.0))
            return
        if now < current["deadline"]: return
    try: actual = market_price(force=True)["price"]
    except RuntimeError: return
    now = time.time()
    with DB_LOCK, database() as db:
        room = db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone()
        if room is None or room["status"] != "running": return
        number = int(room["round_number"])
        current = db.execute("SELECT * FROM rounds WHERE room_code=? AND number=?",(code,number)).fetchone()
        if current is None or current["actual"] is not None or now < current["deadline"]: return
        intermission_until = now+20 if number < 20 else None
        db.execute("UPDATE rounds SET actual=?,resolved_at=?,intermission_until=? WHERE room_code=? AND number=?",(actual,now,intermission_until,code,number))
        forecasts = db.execute("SELECT player_token,prediction FROM forecasts WHERE room_code=? AND round_number=?",(code,number)).fetchall()
        for item in forecasts:
            error_pct = abs(float(item["prediction"])-actual)/actual*100
            points = max(0.0,100.0-20.0*error_pct)
            db.execute("UPDATE forecasts SET error_pct=?,points=? WHERE room_code=? AND round_number=? AND player_token=?",(error_pct,points,code,number,item["player_token"]))
            db.execute("UPDATE players SET score=score+? WHERE token=?",(points,item["player_token"]))
        if number >= 20:
            db.execute("UPDATE rooms SET status='finished' WHERE code=?",(code,))

def clean_name(value):
    name = " ".join(str(value or "").split()).strip()
    if not name or len(name)>20 or any(ord(char)<32 for char in name): raise ValueError("Choose a player name from 1 to 20 characters.")
    return name

def normalize_code(value):
    code = re.sub(r"[^A-Za-z0-9]","",str(value or "")).upper()
    if len(code)!=6: raise ValueError("Room codes must be 6 characters.")
    return code

def create_room(name,duration):
    name=clean_name(name)
    try: duration=int(duration)
    except (ValueError,TypeError) as error: raise ValueError("Choose a valid round length.") from error
    if duration not in DURATIONS: raise ValueError("Choose 30 seconds, 1 minute, or 2 minutes per round.")
    now=time.time(); token=secrets.token_urlsafe(32)
    with DB_LOCK,database() as db:
        for _ in range(20):
            code="".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
            try:
                db.execute("INSERT INTO rooms(code,host_token,status,duration_ms,created_at) VALUES(?,?,'lobby',?,?)",(code,token,duration,now)); break
            except sqlite3.IntegrityError: continue
        else: raise RuntimeError("Could not create a room. Please try again.")
        db.execute("INSERT INTO players(token,room_code,name,joined_at,last_seen,is_host) VALUES(?,?,?,?,?,1)",(token,code,name,now,now))
    return {"code":code,"token":token}

def join_room(code,name):
    code=normalize_code(code); name=clean_name(name); token=secrets.token_urlsafe(32); now=time.time()
    with DB_LOCK,database() as db:
        room=db.execute("SELECT status FROM rooms WHERE code=?",(code,)).fetchone()
        if room is None: raise LookupError("Room not found. Check the code.")
        if room["status"]!="lobby": raise PermissionError("This game has started; ask the host for another room.")
        count=db.execute("SELECT COUNT(*) AS n FROM players WHERE room_code=?",(code,)).fetchone()["n"]
        if count>=MAX_PLAYERS: raise OverflowError("This room is full (12 players maximum).")
        try: db.execute("INSERT INTO players(token,room_code,name,joined_at,last_seen) VALUES(?,?,?,?,?)",(token,code,name,now,now))
        except sqlite3.IntegrityError as error: raise ValueError("That name is already being used in this room.") from error
    return {"code":code,"token":token}

def authenticated_player(handler,code):
    auth=handler.headers.get("Authorization",""); token=auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token: raise PermissionError("Your player session is missing. Rejoin the room.")
    with DB_LOCK,database() as db:
        player=db.execute("SELECT * FROM players WHERE room_code=? AND token=?",(code,token)).fetchone()
        if player is None: raise PermissionError("That player session is not a member of this room.")
        db.execute("UPDATE players SET last_seen=? WHERE token=?",(time.time(),token))
        return dict(player)

def room_snapshot(code,token):
    resolve_if_due(code); now=time.time()
    with DB_LOCK,database() as db:
        room=db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone()
        if room is None: raise LookupError("Room not found.")
        player=db.execute("SELECT * FROM players WHERE room_code=? AND token=?",(code,token)).fetchone()
        if player is None: raise PermissionError("That player session is not a member of this room.")
        db.execute("UPDATE players SET last_seen=? WHERE token=?",(now,token))
        players=db.execute("SELECT name,score,is_host,last_seen FROM players WHERE room_code=? ORDER BY joined_at",(code,)).fetchall()
        online=sum(1 for p in players if now-p["last_seen"]<12)
        leaders=db.execute("SELECT name,score,token FROM players WHERE room_code=? ORDER BY score DESC,name COLLATE NOCASE",(code,)).fetchall()
        current=None; locked=False; submitted=0
        if room["status"]=="running":
            current=db.execute("SELECT * FROM rounds WHERE room_code=? AND number=?",(code,room["round_number"])).fetchone()
            locked=db.execute("SELECT 1 FROM forecasts WHERE room_code=? AND round_number=? AND player_token=?",(code,room["round_number"],token)).fetchone() is not None
            submitted=db.execute("SELECT COUNT(*) AS n FROM forecasts WHERE room_code=? AND round_number=?",(code,room["round_number"])).fetchone()["n"]
        completed=db.execute("SELECT number,actual FROM rounds WHERE room_code=? AND actual IS NOT NULL ORDER BY number",(code,)).fetchall(); reveal=None
        if completed:
            latest=completed[-1]
            rows=db.execute("SELECT p.name,f.prediction,f.points FROM forecasts f JOIN players p ON p.token=f.player_token WHERE f.room_code=? AND f.round_number=? ORDER BY f.points DESC,p.name COLLATE NOCASE",(code,latest["number"])).fetchall()
            reveal={"number":latest["number"],"actual":latest["actual"],"forecasts":[dict(name=r["name"],prediction=r["prediction"],points=r["points"]) for r in rows]}
        round_data=None
        if current:
            intermission=room["status"]=="running" and current["actual"] is not None and current["number"]<20
            round_data={"number":current["number"],"phase":"intermission" if intermission else "forecast","start_price":current["start_price"],"actual":current["actual"],"seconds_left":0 if intermission else max(0,math.ceil(current["deadline"]-now)),"intermission_seconds_left":max(0,math.ceil((current["intermission_until"] or now)-now)) if intermission else 0,"submitted":submitted,"locked":locked}
        return {"code":code,"status":room["status"],"round_number":room["round_number"],"duration_ms":room["duration_ms"],"is_host":bool(player["is_host"]),"player_name":player["name"],"players":[dict(name=p["name"],host=bool(p["is_host"]),online=now-p["last_seen"]<12) for p in players],"connected_count":online,"leaderboard":[dict(name=r["name"],score=round(r["score"],1),you=r["token"]==token) for r in leaders],"round":round_data,"rounds_completed":len(completed),"reveal":reveal,"can_start":room["status"]=="lobby" and bool(player["is_host"]) and online>=2}

class Handler(SimpleHTTPRequestHandler):
    server_version="TickerTussle/2.0"
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT),**kwargs)
    def end_headers(self):
        self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","strict-origin-when-cross-origin"); self.send_header("X-Frame-Options","DENY"); super().end_headers()
    def log_message(self,fmt,*args):
        path=urlsplit(str(args[0])).path if args else ""
        print("%s - %s %s"%(self.client_address[0],self.command,path))
    def json_response(self,status,payload):
        raw=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def read_json(self):
        try: length=int(self.headers.get("Content-Length","0"))
        except ValueError as error: raise ValueError("Invalid request size.") from error
        if length<1 or length>8192: raise ValueError("Request is empty or too large.")
        try: data=json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError,json.JSONDecodeError) as error: raise ValueError("Request must contain valid JSON.") from error
        if not isinstance(data,dict): raise ValueError("Request must be a JSON object.")
        return data
    def do_GET(self):
        path=urlsplit(self.path).path
        try:
            if path in ("/","/index.html","/btc-usd-live.html"):
                # Serve the app shell for both the root page and invite URLs.
                # The browser keeps its original query string for JavaScript to read.
                self.path="/index.html"
                return super().do_GET()
            if path=="/api/health": return self.json_response(200,{"ok":True,"service":"ticker-tussle"})
            if path=="/api/price":
                try:
                    quote=market_price(); return self.json_response(200,{"bitcoin":{"usd":quote["price"]},"source":quote["source"],"change_24h_pct":quote["change_24h_pct"],"as_of":quote["as_of"]})
                except RuntimeError as error: return self.json_response(502,{"error":str(error)})
            if path=="/api/room":
                query=parse_qs(urlsplit(self.path).query); code=normalize_code(query.get("code",[""])[0]); player=authenticated_player(self,code)
                return self.json_response(200,room_snapshot(code,player["token"]))
            if path=="/api/chat":
                query=parse_qs(urlsplit(self.path).query); code=normalize_code(query.get("code",[""])[0]); player=authenticated_player(self,code); since=max(0,int(query.get("since",["0"])[0]))
                with DB_LOCK,database() as db: rows=db.execute("SELECT id,name,body,created_at FROM chat_messages WHERE room_code=? AND id>? ORDER BY id LIMIT 100",(code,since)).fetchall()
                messages=[dict(id=r["id"],name=r["name"],text=r["body"],at=datetime.fromtimestamp(r["created_at"],timezone.utc).isoformat()) for r in rows]
                return self.json_response(200,{"messages":messages})
            return super().do_GET()
        except ValueError as error: return self.json_response(400,{"error":str(error)})
        except LookupError as error: return self.json_response(404,{"error":str(error)})
        except PermissionError as error: return self.json_response(403,{"error":str(error)})
        except (sqlite3.Error,OSError) as error:
            print("Request failed:",type(error).__name__); return self.json_response(500,{"error":"The server could not complete that request."})
    def do_POST(self):
        path=urlsplit(self.path).path
        try:
            data=self.read_json()
            if path=="/api/rooms": return self.json_response(201,create_room(data.get("name"),data.get("duration_ms",60_000)))
            if path=="/api/join": return self.json_response(201,join_room(data.get("code"),data.get("name")))
            code=normalize_code(data.get("code")); player=authenticated_player(self,code)
            if path=="/api/start":
                if not player["is_host"]: return self.json_response(403,{"error":"Only the room host can start the game."})
                with DB_LOCK,database() as db:
                    room=db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone(); active=db.execute("SELECT COUNT(*) AS n FROM players WHERE room_code=? AND last_seen>?",(code,time.time()-12)).fetchone()["n"]
                    if room is None: return self.json_response(404,{"error":"Room not found."})
                    if room["status"]!="lobby": return self.json_response(409,{"error":"This game has already started."})
                    if active<2: return self.json_response(409,{"error":"Invite at least one other player before starting."})
                try: quote=market_price(force=True)
                except RuntimeError as error: return self.json_response(503,{"error":str(error)})
                now=time.time()
                with DB_LOCK,database() as db:
                    room=db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone()
                    if room is None or room["status"]!="lobby": return self.json_response(409,{"error":"This room is no longer waiting to start."})
                    db.execute("UPDATE rooms SET status='running',round_number=1 WHERE code=?",(code,)); db.execute("INSERT INTO rounds(room_code,number,start_price,started_at,deadline) VALUES(?,?,?,?,?)",(code,1,quote["price"],now,now+room["duration_ms"]/1000.0))
                return self.json_response(200,{"ok":True})
            if path=="/api/forecast":
                prediction=float(data.get("prediction"))
                if not math.isfinite(prediction) or prediction<=0 or prediction>1_000_000_000: raise ValueError("Enter a finite BTC/USD forecast greater than zero.")
                with DB_LOCK,database() as db:
                    room=db.execute("SELECT * FROM rooms WHERE code=?",(code,)).fetchone()
                    if room is None or room["status"]!="running": return self.json_response(409,{"error":"This room is not accepting forecasts."})
                    current=db.execute("SELECT * FROM rounds WHERE room_code=? AND number=?",(code,room["round_number"])).fetchone()
                    if current is None or current["actual"] is not None or time.time()>=current["deadline"]: return self.json_response(409,{"error":"This round is closed. Wait for the next round."})
                    try: db.execute("INSERT INTO forecasts(room_code,round_number,player_token,prediction,submitted_at) VALUES(?,?,?,?,?)",(code,room["round_number"],player["token"],prediction,time.time()))
                    except sqlite3.IntegrityError: return self.json_response(409,{"error":"Your forecast is already locked for this round."})
                return self.json_response(201,{"ok":True})
            if path=="/api/chat":
                text=str(data.get("text","")).strip()
                if not text or len(text)>500 or any(ord(char)<32 and char not in "\t\n\r" for char in text): raise ValueError("Chat messages must be 1 to 500 characters.")
                now=time.time()
                with DB_LOCK,database() as db:
                    previous=db.execute("SELECT created_at FROM chat_messages WHERE player_token=? ORDER BY id DESC LIMIT 1",(player["token"],)).fetchone()
                    if previous and now-float(previous["created_at"])<0.8: return self.json_response(429,{"error":"Please wait a moment before sending another message."})
                    db.execute("INSERT INTO chat_messages(room_code,player_token,name,body,created_at) VALUES(?,?,?,?,?)",(code,player["token"],player["name"],text,now))
                return self.json_response(201,{"ok":True})
            return self.json_response(404,{"error":"Not found."})
        except (TypeError,ValueError) as error: return self.json_response(400,{"error":str(error) or "Invalid request."})
        except LookupError as error: return self.json_response(404,{"error":str(error)})
        except PermissionError as error: return self.json_response(403,{"error":str(error)})
        except OverflowError as error: return self.json_response(409,{"error":str(error)})
        except sqlite3.Error as error:
            print("Request failed:",type(error).__name__); return self.json_response(500,{"error":"The server could not save that request."})

if __name__=="__main__":
    initialize_database()
    host=os.environ.get("HOST","0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"); port=int(os.environ.get("PORT","8765"))
    print(f"Ticker Tussle: Forecast Arena listening on {host}:{port}")
    ThreadingHTTPServer((host,port),Handler).serve_forever()
