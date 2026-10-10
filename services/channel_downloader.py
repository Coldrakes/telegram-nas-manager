from pathlib import Path
from telethon import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from config import API_HASH,API_ID,CHANNEL_SESSION_PATH,CHANNEL_SCAN_PAGE_SIZE
from services.channel_db import channel_db
class ChannelDownloader:
 def __init__(self):self.client=TelegramClient(CHANNEL_SESSION_PATH,API_ID,API_HASH);self._started=False
 async def start(self):
  Path(CHANNEL_SESSION_PATH).parent.mkdir(parents=True,exist_ok=True);await self.client.connect();self._started=await self.client.is_user_authorized()
  if not self._started:await self.client.disconnect()
 async def stop(self):
  if self.client.is_connected():await self.client.disconnect()
  self._started=False
 @property
 def available(self):return self._started
 async def logout(self):
  if self.client.is_connected():
   await self.client.log_out()
  self._started=False
  self.client=TelegramClient(CHANNEL_SESSION_PATH,API_ID,API_HASH)
 async def resolve_allowed(self,item):
  """Comprueba pertenencia al canal privado, sin unirse automáticamente."""
  ref=item['invite_link'];invite_hash=ref.rsplit('+',1)[-1]
  result=await self.client(CheckChatInviteRequest(invite_hash))
  if not isinstance(result,ChatInviteAlready):
   raise PermissionError('La cuenta no pertenece al canal autorizado.')
  return result.chat
 async def scan_to_db(self,ref,dest,progress=None):
  if not self._started:raise RuntimeError('La sesión de usuario para canales no está autorizada.')
  e=await self.client.get_entity(ref.strip() if isinstance(ref,str) else ref);cid=int(e.id);title=getattr(e,'title',None) or str(cid);channel_db.upsert_channel(cid,title,str(getattr(e,'id',ref)),dest);n=0
  async for m in self.client.iter_messages(e,reverse=True):
   if not m.file:continue
   channel_db.add_file(cid,m.id,Path(m.file.name or f'archivo_{m.id}').name,m.file.size or 0);n+=1
   if progress and n%CHANNEL_SCAN_PAGE_SIZE==0:await progress(n)
  channel_db.mark_scan_complete(cid);return e,n
 async def download(self,e,item,destination):
  m=await self.client.get_messages(e,ids=item['message_id'])
  if not m or not m.file:raise RuntimeError('El mensaje ya no contiene un archivo descargable.')
  destination.parent.mkdir(parents=True,exist_ok=True);partial=destination.with_name(destination.name+'.part');partial.unlink(missing_ok=True)
  r=await self.client.download_media(m,file=str(partial))
  if not r or not partial.exists():raise RuntimeError('La descarga terminó pero el archivo temporal no existe.')
  partial.replace(destination);return destination
channel_downloader=ChannelDownloader()
