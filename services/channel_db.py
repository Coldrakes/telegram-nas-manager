import sqlite3
from pathlib import Path
from config import CHANNEL_DB_PATH
SCHEMA='''
CREATE TABLE IF NOT EXISTS channels(channel_id INTEGER PRIMARY KEY,title TEXT NOT NULL,reference TEXT,destination TEXT NOT NULL,scan_complete INTEGER NOT NULL DEFAULT 0,last_scan_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS channel_files(channel_id INTEGER NOT NULL,message_id INTEGER NOT NULL,filename TEXT NOT NULL,size INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',retries INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(channel_id,message_id));
CREATE INDEX IF NOT EXISTS idx_channel_files_queue ON channel_files(channel_id,status,message_id);'''
class ChannelDB:
 def __init__(self,path=CHANNEL_DB_PATH): self.path=Path(path)
 def _c(self):
  self.path.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=30000'); return c
 def init(self):
  with self._c() as c:c.executescript(SCHEMA)
 def upsert_channel(self,cid,title,ref,dest):
  with self._c() as c:c.execute('''INSERT INTO channels(channel_id,title,reference,destination) VALUES(?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title,reference=excluded.reference,destination=excluded.destination,updated_at=CURRENT_TIMESTAMP''',(cid,title,ref,dest))
 def add_file(self,cid,mid,name,size):
  with self._c() as c:c.execute('INSERT OR IGNORE INTO channel_files(channel_id,message_id,filename,size) VALUES(?,?,?,?)',(cid,mid,name,size or 0))
 def reset_interrupted(self,cid=None):
  with self._c() as c:
   if cid is None:c.execute("UPDATE channel_files SET status='pending',updated_at=CURRENT_TIMESTAMP WHERE status='downloading'")
   else:c.execute("UPDATE channel_files SET status='pending',updated_at=CURRENT_TIMESTAMP WHERE channel_id=? AND status='downloading'",(cid,))
 def next_batch(self,cid,limit,max_retries):
  with self._c() as c:return [dict(r) for r in c.execute("SELECT * FROM channel_files WHERE channel_id=? AND (status='pending' OR (status='error' AND retries<?)) ORDER BY message_id LIMIT ?",(cid,max_retries,limit)).fetchall()]
 def set_status(self,cid,mid,status,error=None,retry=False):
  with self._c() as c:c.execute('UPDATE channel_files SET status=?,error=?,retries=retries+?,updated_at=CURRENT_TIMESTAMP WHERE channel_id=? AND message_id=?',(status,error,1 if retry else 0,cid,mid))
 def counts(self,cid):
  with self._c() as c:
   d={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM channel_files WHERE channel_id=? GROUP BY status',(cid,)).fetchall()}; d['total']=sum(d.values()); return d
 def mark_scan_complete(self,cid):
  with self._c() as c:c.execute('UPDATE channels SET scan_complete=1,last_scan_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE channel_id=?',(cid,))
channel_db=ChannelDB()
