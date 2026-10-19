"""Persistent manual batch state. All mutations committed immediately."""
import sqlite3
from pathlib import Path
from config import BATCH_DB_PATH

class BatchDB:
    def connect(self):
        Path(BATCH_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        db=sqlite3.connect(BATCH_DB_PATH, timeout=30)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=30000')
        return db
    def init(self):
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',message_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,filename TEXT NOT NULL,size INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',error TEXT, UNIQUE(batch_id,message_id));''')
    def create(self,user,chat,mode):
        with self.connect() as db:
            cur=db.execute('INSERT INTO batches(user_id,chat_id,mode) VALUES(?,?,?)',(user,chat,mode));return cur.lastrowid
    def get(self,bid):
        with self.connect() as db:
            r=db.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone();return dict(r) if r else None
    def active(self,user):
        with self.connect() as db:
            r=db.execute("SELECT * FROM batches WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",(user,)).fetchone();return dict(r) if r else None
    def list(self,user=None):
        with self.connect() as db:
            rows=db.execute('SELECT * FROM batches WHERE user_id=? ORDER BY id DESC LIMIT 20',(user,)).fetchall() if user is not None else db.execute('SELECT * FROM batches ORDER BY id DESC').fetchall()
            return [dict(r) for r in rows]
    def items(self,bid):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM items WHERE batch_id=? ORDER BY id',(bid,))]
    def add(self,bid,chat,msg,name,size):
        with self.connect() as db:db.execute('INSERT OR IGNORE INTO items(batch_id,chat_id,message_id,filename,size) VALUES(?,?,?,?,?)',(bid,chat,msg,name,size))
    def batch_status(self,bid,status):
        with self.connect() as db:db.execute('UPDATE batches SET status=? WHERE id=?',(status,bid))
    def message(self,bid,msg):
        with self.connect() as db:db.execute('UPDATE batches SET message_id=? WHERE id=?',(msg,bid))
    def item_status(self,i,status,error=None):
        with self.connect() as db:db.execute('UPDATE items SET status=?,error=? WHERE id=?',(status,error,i))
    def interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE items SET status='pending' WHERE status='downloading'")
            db.execute("UPDATE batches SET status='queued' WHERE status IN ('downloading','organizing','extracting')")
    def unfinished(self):
        with self.connect() as db:return [dict(r) for r in db.execute("SELECT * FROM batches WHERE status IN ('queued','downloading','organizing','extracting') ORDER BY id")]

batch_db=BatchDB()
