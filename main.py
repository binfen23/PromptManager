import webview, json, base64, shutil, threading, time, uuid, struct, ctypes, sys
from pathlib import Path

try:
    import requests as _req; HAS_REQ = True
except ImportError:
    HAS_REQ = False

try:
    import win32clipboard, win32con
    from PIL import Image as _PIL
    import io as _io
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import pystray
    from pystray import MenuItem as item
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

BASE = Path(sys.argv[0]).parent
DATA = BASE / "data"
PDIR = DATA / "prompt_images"
GDIR = DATA / "gallery"
for d in [DATA, PDIR, GDIR]: d.mkdir(parents=True, exist_ok=True)
SF = BASE / "settings.json"
PF = BASE / "prompts.json"
GF = BASE / "gallery.json"

WIN_W, WIN_H = 300, 700
PREV_W = 300
PREV_GAP = 6

_win      = None
_prev_win = None
_pos      = {"x": 800, "y": 190}

# ── win32 helpers ─────────────────────────────────────────────────────
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
class _RECT(ctypes.Structure):
    _fields_ = [("left",ctypes.c_long),("top",ctypes.c_long),
                ("right",ctypes.c_long),("bottom",ctypes.c_long)]
class _MONITORINFO(ctypes.Structure):
    class _R(ctypes.Structure):
        _fields_ = [("left",ctypes.c_long),("top",ctypes.c_long),
                    ("right",ctypes.c_long),("bottom",ctypes.c_long)]
    _fields_ = [("cbSize",ctypes.c_ulong),("rcMonitor",_R),
                ("rcWork",_R),("dwFlags",ctypes.c_ulong)]

_u32 = ctypes.windll.user32
SWP_MOVE = 0x0001|0x0004|0x0010

if sys.maxsize > 2**32:
    _GetWindowLong = _u32.GetWindowLongPtrW
    _GetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _GetWindowLong.restype = ctypes.c_ssize_t
    _SetWindowLong = _u32.SetWindowLongPtrW
    _SetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    _SetWindowLong.restype = ctypes.c_ssize_t
else:
    _GetWindowLong = _u32.GetWindowLongW
    _GetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _GetWindowLong.restype = ctypes.c_long
    _SetWindowLong = _u32.SetWindowLongW
    _SetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _SetWindowLong.restype = ctypes.c_long

def _hide_from_taskbar(hwnd):
    """彻底在任务栏隐藏图标，仅保留托盘和屏幕界面"""
    if not HAS_TRAY or not hwnd: return
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    try:
        style = _GetWindowLong(hwnd, GWL_EXSTYLE)
        if style is not None:
            style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
            _SetWindowLong(hwnd, GWL_EXSTYLE, style)
    except: pass

def _hwnd(title):
    return _u32.FindWindowW(None, title)

def _wrect(hwnd):
    r = _RECT(); _u32.GetWindowRect(hwnd, ctypes.byref(r)); return r

def _warea(hwnd):
    mon = _u32.MonitorFromWindow(hwnd, 2)
    mi  = _MONITORINFO(); mi.cbSize = ctypes.sizeof(_MONITORINFO)
    _u32.GetMonitorInfoW(mon, ctypes.byref(mi))
    return mi.rcWork

def _cursor():
    p = _POINT(); _u32.GetCursorPos(ctypes.byref(p)); return p.x, p.y

def _mouse_down():
    return (_u32.GetAsyncKeyState(0x01) & 0x8000) != 0

def _phys_to_log(hwnd, val):
    try:
        dpi = _u32.GetDpiForWindow(hwnd)
        if dpi and dpi != 96:
            return int(val * 96 / dpi)
    except:
        pass
    return int(val)


# ══════════════════════════════════════════════════════════════════════
#  PreviewManager
# ══════════════════════════════════════════════════════════════════════
class PreviewManager:
    MARGIN = 20

    def __init__(self):
        self._lock    = threading.Lock()
        self._visible = False
        self._pending = None
        self._stop    = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def request_show(self, text: str, imgs=None):
        with self._lock:
            self._pending = {"text": text, "images": imgs or []}

    def request_hide(self):
        pass

    def hide_now(self):
        with self._lock:
            self._pending = None
        self._do_hide()

    def _do_show(self, payload):
        if not _prev_win: return
        h_main = _hwnd("Prompt Hub")
        h_prev = _hwnd("ph_preview")
        if not h_main or not h_prev: return

        wa = _warea(h_main)
        wr = _wrect(h_main)
        mid = (wa.left + wa.right) // 2
        if wr.left + WIN_W // 2 < mid:
            px = wr.right + PREV_GAP
        else:
            px = wr.left - PREV_W - PREV_GAP

        py  = wr.top
        ph  = wr.bottom - wr.top
        HWND_TOPMOST = ctypes.c_void_p(-1)
        _u32.SetWindowPos(h_prev, HWND_TOPMOST, int(px), int(py), PREV_W, ph, 0x0010)

        js = f"setContent({json.dumps(payload)})"
        try: _prev_win.evaluate_js(js)
        except: pass
        self._visible = True

    def _do_hide(self):
        h = _hwnd("ph_preview")
        if h:
            _u32.SetWindowPos(h, 0, -9999, -9999, PREV_W, WIN_H, SWP_MOVE)
        self._visible = False

    def _mouse_in_zone(self):
        cx, cy = _cursor()
        M = self.MARGIN
        for title in ("Prompt Hub", "ph_preview"):
            h = _hwnd(title)
            if not h: continue
            r = _wrect(h)
            if r.left-M <= cx <= r.right+M and r.top-M <= cy <= r.bottom+M:
                return True
        return False

    def _run(self):
        while not self._stop:
            time.sleep(0.08)
            try:
                with self._lock:
                    pending = self._pending

                if pending is not None:
                    self._do_show(pending)
                    with self._lock:
                        if self._pending is pending:
                            self._pending = None
                elif self._visible:
                    if not self._mouse_in_zone():
                        self._do_hide()
            except Exception:
                pass

_preview = PreviewManager()


# ══════════════════════════════════════════════════════════════════════
#  边缘吸附拖拽 (支持强制弹出动画)
# ══════════════════════════════════════════════════════════════════════
class EdgeDocker:
    DOCK_THRESH = 8
    EDGE_SENSE  = 3
    PEEK_MARGIN = 50
    COOLDOWN    = 0.8
    IDLE="idle"; DOCKED="docked"; PEEKING="peeking"

    def __init__(self):
        self.hwnd  = None
        self.prev_hwnd = None
        self.state = self.IDLE
        self.edge  = None
        self._stop = False
        self._request_peek = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        
    def force_peek(self):
        self._request_peek = True

    @staticmethod
    def _mouse_in_preview():
        h = _hwnd("ph_preview")
        if not h: return False
        r = _wrect(h)
        if r.left < -1000: return False
        M = 10
        cx, cy = _cursor()
        return r.left-M <= cx <= r.right+M and r.top-M <= cy <= r.bottom+M

    def _run(self):
        # 等待主窗口和预览窗口全部就绪
        while not self._stop:
            self.hwnd = _hwnd("Prompt Hub")
            self.prev_hwnd = _hwnd("ph_preview")
            if self.hwnd and self.prev_hwnd:
                break
            time.sleep(0.2)
            
        if not self.hwnd: return
        
        # 将主界面和预览窗口同时从任务栏隐藏
        _hide_from_taskbar(self.hwnd)
        _hide_from_taskbar(self.prev_hwnd)

        HWND_TOPMOST = ctypes.c_void_p(-1)
        _u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001|0x0002|0x0010)

        def mv_phys(px, py):
            if self.hwnd:
                _u32.SetWindowPos(self.hwnd, 0, int(px), int(py), 0, 0, 0x0001|0x0004|0x0010)
                try:
                    _pos["x"] = _phys_to_log(self.hwnd, px)
                    _pos["y"] = _phys_to_log(self.hwnd, py)
                except: pass

        def anim(tx_phys, ty_phys, steps=14, dur=0.15):
            r = _wrect(self.hwnd)
            sx = r.left
            sy = r.top
            for i in range(1, steps+1):
                t = i/steps; t2 = 1-(1-t)**2
                mv_phys(sx+(tx_phys-sx)*t2, sy+(ty_phys-sy)*t2)
                time.sleep(dur/steps)
            mv_phys(tx_phys, ty_phys)

        cooldown_until = 0.0
        outside_count  = 0          
        OUTSIDE_THRESH = 3          

        while not self._stop:
            try:
                time.sleep(0.1)
                now = time.time()
                cx, cy = _cursor()

                wa = _warea(self.hwnd)
                wr = _wrect(self.hwnd)
                ww = wr.right-wr.left; wh = wr.bottom-wr.top
                
                if getattr(self, '_request_peek', False):
                    self._request_peek = False
                    if self.state == self.DOCKED:
                        if   self.edge=='left':   anim(wa.left,      wr.top)
                        elif self.edge=='right':  anim(wa.right-ww,  wr.top)
                        elif self.edge=='top':    anim(wr.left,     wa.top)
                        elif self.edge=='bottom': anim(wr.left,     wa.bottom-wh)
                        self.state = self.PEEKING
                        outside_count = 0
                        continue

                M = self.PEEK_MARGIN
                inside_main = (wr.left - M <= cx <= wr.right + M and wr.top - M <= cy <= wr.bottom + M)
                is_inside = inside_main or self._mouse_in_preview()

                is_down = _mouse_down()
                
                if is_down:
                    cooldown_until = now + self.COOLDOWN
                    outside_count  = 0

                if now < cooldown_until:
                    if self.state == self.PEEKING:
                        is_at_edge = False
                        if self.edge=='left' and wr.left <= wa.left + self.DOCK_THRESH: is_at_edge = True
                        elif self.edge=='right' and wr.right >= wa.right - self.DOCK_THRESH: is_at_edge = True
                        elif self.edge=='top' and wr.top <= wa.top + self.DOCK_THRESH: is_at_edge = True
                        elif self.edge=='bottom' and wr.bottom >= wa.bottom - self.DOCK_THRESH: is_at_edge = True
                        if not is_at_edge:
                            self.state = self.IDLE
                            self.edge = None
                    if self.state != self.DOCKED:
                        continue

                if self.state == self.IDLE:
                    outside_count = 0
                    if is_inside: continue 
                    
                    e = None
                    if   wr.left   <= wa.left   + self.DOCK_THRESH: e='left'
                    elif wr.right  >= wa.right  - self.DOCK_THRESH: e='right'
                    elif wr.top    <= wa.top    + self.DOCK_THRESH: e='top'
                    elif wr.bottom >= wa.bottom - self.DOCK_THRESH: e='bottom'
                    
                    if e:
                        self.edge=e; self.state=self.DOCKED
                        if   e=='left':   anim(wa.left-ww,  wr.top)
                        elif e=='right':  anim(wa.right,    wr.top)
                        elif e=='top':    anim(wr.left,     wa.top-wh)
                        elif e=='bottom': anim(wr.left,     wa.bottom)

                elif self.state == self.DOCKED:
                    outside_count = 0
                    rev = False
                    if self.edge=='left' and cx<=wa.left+self.EDGE_SENSE and wr.top-40<=cy<=wr.top+wh+40: rev=True
                    elif self.edge=='right' and cx>=wa.right-self.EDGE_SENSE and wr.top-40<=cy<=wr.top+wh+40: rev=True
                    elif self.edge=='top' and cy<=wa.top+self.EDGE_SENSE and wr.left-40<=cx<=wr.left+ww+40: rev=True
                    elif self.edge=='bottom' and cy>=wa.bottom-self.EDGE_SENSE and wr.left-40<=cx<=wr.left+ww+40: rev=True
                    if rev:
                        if   self.edge=='left':   anim(wa.left,      wr.top)
                        elif self.edge=='right':  anim(wa.right-ww,  wr.top)
                        elif self.edge=='top':    anim(wr.left,     wa.top)
                        elif self.edge=='bottom': anim(wr.left,     wa.bottom-wh)
                        self.state=self.PEEKING

                elif self.state == self.PEEKING:
                    if is_inside:
                        outside_count = 0
                    else:
                        outside_count += 1
                        if outside_count >= OUTSIDE_THRESH:
                            outside_count = 0
                            is_at_edge = False
                            if self.edge=='left' and wr.left <= wa.left + self.DOCK_THRESH: is_at_edge = True
                            elif self.edge=='right' and wr.right >= wa.right - self.DOCK_THRESH: is_at_edge = True
                            elif self.edge=='top' and wr.top <= wa.top + self.DOCK_THRESH: is_at_edge = True
                            elif self.edge=='bottom' and wr.bottom >= wa.bottom - self.DOCK_THRESH: is_at_edge = True
                            
                            if not is_at_edge:
                                self.state = self.IDLE
                                self.edge = None
                            else:
                                if   self.edge=='left':   anim(wa.left-ww, wr.top)
                                elif self.edge=='right':  anim(wa.right,    wr.top)
                                elif self.edge=='top':    anim(wr.left,     wa.top-wh)
                                elif self.edge=='bottom': anim(wr.left,     wa.bottom)
                                self.state=self.DOCKED
            except Exception:
                time.sleep(1)

_docker = EdgeDocker()


# ══════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════
class API:
    window = None

    def app_ready(self):
        """前端界面加载完毕后主动呼叫后端，彻底解决初始化死锁问题"""
        if not hasattr(self, '_started'):
            self._started = True
            _docker.start()
            _preview.start()
            setup_tray()
        return True

    def get_settings(self):
        try: return json.loads(SF.read_text("utf-8")) if SF.exists() else {"theme":"dark","pos":{"x":800,"y":190}}
        except: return {"theme":"dark","pos":{"x":800,"y":190}}
    def save_settings(self, s):
        SF.write_text(json.dumps(s,ensure_ascii=False,indent=2),"utf-8"); return True
    def get_prompts(self):
        try: return json.loads(PF.read_text("utf-8")) if PF.exists() else []
        except: return []
    def save_prompts(self, d):
        PF.write_text(json.dumps(d,ensure_ascii=False,indent=2),"utf-8"); return True
    def get_gallery(self):
        try: return json.loads(GF.read_text("utf-8")) if GF.exists() else []
        except: return []
    def save_gallery(self, d):
        GF.write_text(json.dumps(d,ensure_ascii=False,indent=2),"utf-8"); return True

    def get_image_b64(self, rel):
        try:
            p = BASE/rel
            if not p.exists(): return None
            ext = p.suffix.lower().lstrip(".")
            mime = {"jpg":"jpeg","jpeg":"jpeg","png":"png","gif":"gif","webp":"webp"}.get(ext,"png")
            return f"data:image/{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
        except: return None

    def upload_image(self, data_url:str, folder:str):
        try:
            hd,enc = data_url.split(",",1)
            ext = hd.split("/")[1].split(";")[0].lower()
            if ext not in ("jpeg","jpg","png","gif","webp","bmp"): ext="png"
            if ext=="jpeg": ext="jpg"
            name = uuid.uuid4().hex+"."+ext
            dest = (PDIR if folder=="prompts" else GDIR)/name
            dest.write_bytes(base64.b64decode(enc))
            return {"ok":True,"path":str(dest.relative_to(BASE)).replace("\\","/")}
        except Exception as e: return {"ok":False,"err":str(e)}

    def download_image(self, url:str, folder:str):
        if not HAS_REQ: return {"ok":False,"err":"requests未安装"}
        try:
            r=_req.get(url,timeout=20); r.raise_for_status()
            ct=r.headers.get("content-type","image/jpeg")
            ext=ct.split("/")[1].split(";")[0].lower()
            if ext not in ("jpeg","jpg","png","gif","webp"): ext="jpg"
            name=uuid.uuid4().hex+"."+ext
            dest=(PDIR if folder=="prompts" else GDIR)/name
            dest.write_bytes(r.content)
            return {"ok":True,"path":str(dest.relative_to(BASE)).replace("\\","/")}
        except Exception as e: return {"ok":False,"err":str(e)}

    def delete_file(self, rel):
        try: (BASE/rel).unlink(missing_ok=True); return True
        except: return False

    def copy_text(self, text:str):
        if HAS_WIN32:
            try:
                win32clipboard.OpenClipboard(); win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                win32clipboard.CloseClipboard(); return True
            except:
                try: win32clipboard.CloseClipboard()
                except: pass
        try:
            import subprocess
            subprocess.run("clip", input=text.encode("utf-16"), capture_output=True, check=True)
            return True
        except: return False

    def copy_image_file(self, rel:str):
        if not HAS_WIN32: return False
        try:
            img = _PIL.open(str(BASE/rel))
            
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            
            buf_bmp = _io.BytesIO()
            img.save(buf_bmp, "BMP")
            dib = buf_bmp.getvalue()[14:]
            
            buf_png = _io.BytesIO()
            img.save(buf_png, "PNG")
            png_data = buf_png.getvalue()

            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
            
            png_format = _u32.RegisterClipboardFormatW("PNG")
            if png_format:
                win32clipboard.SetClipboardData(png_format, png_data)
                
            win32clipboard.CloseClipboard()
            return True
        except Exception as e:
            try: win32clipboard.CloseClipboard()
            except: pass
            return False

    def get_clipboard_image(self):
        if not HAS_WIN32: return None
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
                dib=win32clipboard.GetClipboardData(win32con.CF_DIB)
                win32clipboard.CloseClipboard()
                bsz=struct.unpack_from("<I",dib,0)[0]; bc=struct.unpack_from("<H",dib,14)[0]
                cu=struct.unpack_from("<I",dib,32)[0]
                if cu==0 and bc<=8: cu=1<<bc
                po=14+bsz+cu*4; fsz=14+len(dib)
                hdr=b"BM"+struct.pack("<I",fsz)+b"\x00\x00\x00\x00"+struct.pack("<I",po)
                img=_PIL.open(_io.BytesIO(hdr+dib)); out=_io.BytesIO()
                img.save(out,"PNG")
                return "data:image/png;base64,"+base64.b64encode(out.getvalue()).decode()
            win32clipboard.CloseClipboard()
        except:
            try: win32clipboard.CloseClipboard()
            except: pass
        return None

    def get_clipboard_text(self):
        if not HAS_WIN32: return None
        try:
            win32clipboard.OpenClipboard()
            text = None
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
            return text
        except:
            try: win32clipboard.CloseClipboard()
            except: pass
        return None

    def get_win_pos(self): return {"x":_pos["x"],"y":_pos["y"]}

    def move_win(self, x, y):
        _pos["x"]=int(x); _pos["y"]=int(y)
        if _win: _win.move(int(x),int(y))
        return True

    def show_preview(self, text:str, img_paths:list=None):
        def _fetch():
            imgs_data = []
            if img_paths:
                for p in img_paths[:6]:
                    b64 = self.get_image_b64(p)
                    if b64: imgs_data.append({"path": p, "b64": b64})
            _preview.request_show(text, imgs_data)
        threading.Thread(target=_fetch, daemon=True).start()
        return True

    def hide_preview(self):
        _preview.request_hide()
        return True

    def hide_preview_now(self):
        _preview.hide_now()
        return True

    def set_preview_theme(self, theme: str):
        if _prev_win:
            try: _prev_win.evaluate_js(f"setTheme({json.dumps(theme)})")
            except: pass
        return True

    def minimize_win(self):
        if HAS_TRAY:
            if _win: _win.hide()
            return {"ok": True}
        else:
            if _win: _win.minimize()
            return {"ok": False, "err": "未安装 pystray 库，仅最小化到任务栏。请执行 pip install pystray"}

    def close_win(self):
        _preview.hide_now()
        if HAS_TRAY and 'tray_icon' in globals() and tray_icon:
            tray_icon.stop()
        if _prev_win:
            try: _prev_win.destroy()
            except: pass
        if _win: _win.destroy()
        import os
        os._exit(0)

# ══════════════════════════════════════════════════════════════════════
#  预览窗口 HTML
# ══════════════════════════════════════════════════════════════════════
PREV_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0c0c;--surface:#161616;--elevated:#232323;
  --border:#2d2d2d;--border2:#3a3a3a;
  --text:#e4e4e4;--text2:#868686;--text3:#515151;
  --accent:#a78bfa;
  --rs:8px;--rx:5px;
}
.light{
  --bg:#f2f0ed;--surface:#fff;--elevated:#f0eeeb;
  --border:#e2dedd;--border2:#cbc7c2;
  --text:#1a1818;--text2:#7d7870;--text3:#b0ada8;
  --accent:#7c3aed;
}
html,body{
  width:100%;height:100%;overflow:hidden;
  background:var(--bg);
  font-family:'DM Sans',system-ui,sans-serif;
  font-size:12.5px;color:var(--text);
  -webkit-font-smoothing:antialiased;
}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--text3)}
.panel{display:flex;flex-direction:column;height:100%;border:1px solid var(--border)}
.ph{
  padding:8px 12px 7px;border-bottom:1px solid var(--border);
  background:var(--surface);flex-shrink:0;display:flex;align-items:center;
}
.ph-lbl{
  font-family:'DM Mono',monospace;font-size:10px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--text3);
}
.pb{
  flex:1;overflow-y:auto;padding:12px;
  display:flex;flex-direction:column;gap:10px;min-height:0;
  background:var(--bg);
}
.thumb-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  flex-shrink: 0;
}
.thumb{
  border-radius:var(--rx);overflow:hidden;
  border:1px solid var(--border);cursor:pointer;position:relative;
  background:var(--elevated);aspect-ratio:1;
  display:flex;align-items:center;justify-content:center;
}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.zh{
  position:absolute;inset:0;background:rgba(0,0,0,.6);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  font-size:10px;color:var(--accent);font-weight:500;
  opacity:0;transition:opacity .15s;pointer-events:none;
  text-align:center;line-height:1.4;
}
.thumb:hover .zh{opacity:1}
.txt{
  font-size:12.5px;line-height:1.8;color:var(--text);
  word-break:break-all;white-space:pre-wrap;
}
.lb{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);
  z-index:999;align-items:center;justify-content:center;cursor:pointer;
}
.lb.on{display:flex}
.lb img{
  max-width:90%;max-height:90%;object-fit:contain;
  border-radius:var(--rs);box-shadow:0 8px 40px rgba(0,0,0,.8);cursor:default;
}
.toast{position:absolute;bottom:20px;left:50%;transform:translateX(-50%) translateY(6px);background:var(--elevated);border:1px solid rgba(74,222,128,.3);border-radius:20px;padding:4px 13px;font-size:11.5px;color:#4ade80;white-space:nowrap;z-index:300;opacity:0;transition:opacity .15s,transform .15s;pointer-events:none}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
</style></head>
<body>
<div class="panel">
  <div class="ph"><span class="ph-lbl">预览</span></div>
  <div class="pb" id="pb"></div>
</div>
<div class="lb" id="lb"><img id="lbimg" alt=""/></div>
<div class="toast" id="toast">图片已复制</div>
<script>
async function ca(m,...a){const ap=window.pywebview&&window.pywebview.api;if(!ap)return null;try{return await ap[m](...a)}catch(e){return null}}
function setTheme(t){ document.documentElement.className = t==='light' ? 'light' : ''; }
let _tt;
function showToast(){
  const t=document.getElementById('toast');
  t.classList.add('on');
  clearTimeout(_tt);
  _tt=setTimeout(()=>t.classList.remove('on'), 1200);
}

function setContent(d){
  const pb=document.getElementById('pb');
  pb.innerHTML='';
  if(d.images && d.images.length > 0){
    const grid = document.createElement('div');
    grid.className = 'thumb-grid';
    d.images.forEach(imgObj => {
      const w=document.createElement('div'); w.className='thumb';
      const im=document.createElement('img'); im.src=imgObj.b64; im.alt='';
      const zh=document.createElement('div'); zh.className='zh'; zh.innerHTML='单击复制<br/>双击预览';
      w.appendChild(im); w.appendChild(zh);
      
      let clkTimer;
      w.onclick = (e) => {
        clearTimeout(clkTimer);
        clkTimer = setTimeout(async () => {
          await ca('copy_image_file', imgObj.path);
          showToast();
        }, 220);
      };
      w.ondblclick = (e) => {
        clearTimeout(clkTimer);
        document.getElementById('lbimg').src=imgObj.b64;
        document.getElementById('lb').classList.add('on');
      };
      grid.appendChild(w);
    });
    pb.appendChild(grid);
  }
  const t=document.createElement('div'); t.className='txt'; t.textContent=d.text;
  pb.appendChild(t);
}
document.getElementById('lb').addEventListener('click',function(e){
  if(e.target!==document.getElementById('lbimg')) this.classList.remove('on');
});
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  主界面 HTML
# ══════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Prompt Manager</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#0c0c0c;--surface:#161616;--card:#1c1c1c;--elevated:#232323;
  --border:#2d2d2d;--border2:#3a3a3a;
  --text:#e4e4e4;--text2:#868686;--text3:#515151;
  --accent:#a78bfa;--accent-d:#8b5cf6;
  --abg:rgba(167,139,250,.1);--abg2:rgba(167,139,250,.18);
  --red:#f87171;--rbg:rgba(248,113,113,.12);--ok:#4ade80;
  --rs:8px;--rx:5px;
  --sh:0 8px 32px rgba(0,0,0,.7),0 2px 8px rgba(0,0,0,.5);
  --tr:140ms cubic-bezier(.4,0,.2,1);--trs:240ms cubic-bezier(.4,0,.2,1);
}
.light{
  --bg:#f2f0ed;--surface:#fff;--card:#fafaf9;--elevated:#f0eeeb;
  --border:#e2dedd;--border2:#cbc7c2;
  --text:#1a1818;--text2:#7d7870;--text3:#b0ada8;
  --accent:#7c3aed;--accent-d:#6d28d9;
  --abg:rgba(124,58,237,.08);--abg2:rgba(124,58,237,.14);
  --red:#dc2626;--rbg:rgba(220,38,38,.08);
  --sh:0 8px 32px rgba(0,0,0,.12),0 2px 8px rgba(0,0,0,.07);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;background:var(--bg);font-family:'DM Sans',sans-serif;font-size:13px;color:var(--text);-webkit-font-smoothing:antialiased;overflow:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--text3)}
button{font-family:inherit;cursor:pointer;border:none;background:none;outline:none;color:inherit}
input,textarea{font-family:inherit;outline:none;border:none;background:none;color:inherit;resize:none}
img{display:block;user-select:none;-webkit-user-drag:none}
.app{width:100vw;height:100vh;background:var(--bg);overflow:hidden;display:flex;flex-direction:column;box-shadow:var(--sh);border:1px solid var(--border);position:relative;
  -webkit-app-region:no-drag}
.tb{height:44px;min-height:44px;flex-shrink:0;display:flex;align-items:center;padding:0 12px;background:var(--surface);border-bottom:1px solid var(--border);gap:8px;user-select:none;
  -webkit-app-region:drag;cursor:move}
.tb-icon{font-size:14px;color:var(--accent);font-family:'Syne',sans-serif;flex-shrink:0}
.tb-name{font-family:'Syne',sans-serif;font-weight:700;font-size:13px;letter-spacing:-.01em;flex:1}
.tb-ctrls{display:flex;gap:3px;-webkit-app-region:no-drag}

.cbtn{width:26px;height:26px;border-radius:6px;flex-shrink:0;cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--text2);transition:background var(--tr),color var(--tr)}
.cbtn:hover{background:var(--elevated);color:var(--text)}
.cbtn.cl:hover{background:var(--rbg);color:var(--red)}
.cbtn svg{width:12px;height:12px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.nav{display:flex;gap:4px;padding:7px 10px 0;flex-shrink:0;background:var(--surface);border-bottom:1px solid var(--border)}
.ntab{flex:1;height:33px;border-radius:var(--rx);font-family:'Syne',sans-serif;font-size:12.5px;font-weight:600;letter-spacing:.01em;color:var(--text2);transition:all var(--tr)}
.ntab.on{color:var(--accent);background:var(--abg)}
.ntab:not(.on):hover{color:var(--text);background:var(--elevated)}
.pw{flex:1;overflow:hidden;position:relative;min-height:0}
.pg{position:absolute;inset:0;display:flex;flex-direction:column;opacity:1;transform:translateX(0);transition:opacity var(--trs),transform var(--trs)}
.pg.off{opacity:0;pointer-events:none;transform:translateX(16px)}
.pg.sl.off{transform:translateX(-16px)}
.ptb{display:flex;align-items:center;padding:9px 12px 7px;gap:8px;flex-shrink:0}
.plbl{font-family:'DM Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);flex:1}
.badge{font-family:'DM Mono',monospace;font-size:10px;color:var(--text3);padding:2px 7px;background:var(--elevated);border-radius:20px}
.abtn{height:27px;padding:0 10px;background:var(--abg);color:var(--accent);border-radius:var(--rx);font-size:12px;font-weight:500;display:flex;align-items:center;gap:4px;transition:background var(--tr),transform var(--tr)}
.abtn:hover{background:var(--abg2);transform:translateY(-1px)}
.abtn:active{transform:translateY(0)}
.abtn svg{width:9px;height:9px;stroke:currentColor;stroke-width:2.8;fill:none;stroke-linecap:round}

.tag-bar {display:flex; gap:6px; padding:0 12px 6px; overflow-x:auto; flex-shrink:0; cursor:grab; user-select:none; align-items:center;}
.tag-bar:active {cursor:grabbing;}
.tag-bar::-webkit-scrollbar {display: none;}
.tag-pill {
  display: inline-flex; align-items: center; justify-content: center;
  padding: 0 10px; height: 23px; font-size: 11px; line-height: 1;
  border-radius: 12px; background: var(--elevated); border: 1px solid var(--border); 
  color: var(--text2); cursor: pointer; white-space: nowrap; transition: all var(--tr); flex-shrink: 0;
}
.tag-pill:hover {border-color:var(--border2); color:var(--text)}
.tag-pill.on {background:var(--abg); border-color:var(--accent); color:var(--accent); font-weight: 500;}

.pscroll{flex:1;overflow-y:auto;overflow-x:hidden;padding:8px;display:flex;flex-direction:column;gap:5px;min-height:0}
.pcard{min-height:80px;flex-shrink:0;background:var(--card);border:1px solid var(--border);border-radius:var(--rs);display:flex;align-items:stretch;overflow:hidden;cursor:pointer;position:relative;transition:border-color var(--tr),background var(--tr),transform var(--tr),box-shadow var(--tr)}
.pcard:hover{border-color:var(--border2);background:var(--elevated);transform:translateY(-1px);box-shadow:0 4px 14px rgba(0,0,0,.22)}
.pcard.cp{border-color:var(--ok)!important}
.pcard::after{content:'已复制 ✓';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(74,222,128,.07);color:var(--ok);font-size:13px;font-weight:500;opacity:0;border-radius:var(--rs);pointer-events:none;transition:opacity var(--tr)}
.pcard.cp::after{opacity:1}
.cimg{width:70px;min-width:70px;overflow:hidden;flex-shrink:0;position:relative}
.cimg img{width:100%;height:100%;object-fit:cover}
.cimg .icnt {position:absolute; bottom:2px; right:2px; background:rgba(0,0,0,0.6); color:#fff; font-size:9px; padding:1px 4px; border-radius:4px; font-weight:600}
.cbody{flex:1;padding:9px 6px 9px 10px;display:flex;flex-direction:column;justify-content:center;overflow:hidden;min-width:0; gap:4px}
.ctxt{font-size:12px;line-height:1.55;color:var(--text2);display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;word-break:break-all}
.pcard.noi .ctxt{color:var(--text)}

.ctags {display:flex; gap:4px; flex-wrap:wrap; margin-bottom:2px}
.ctags span {
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10px; padding: 0 6px; height: 18px; line-height: 1;
  background: var(--abg); color: var(--accent); border-radius: 4px; 
  border: 1px solid var(--accent); font-weight: 500;
}

.cacts{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:5px 4px;gap:3px;opacity:0;transition:opacity var(--tr);flex-shrink:0}
.pcard:hover .cacts{opacity:1}
.iact{width:24px;height:24px;border-radius:5px;display:flex;align-items:center;justify-content:center;color:var(--text3);transition:background var(--tr),color var(--tr)}
.iact:hover{background:var(--elevated);color:var(--text)}
.iact.d:hover{background:var(--rbg);color:var(--red)}
.iact svg{width:11px;height:11px;stroke:currentColor;stroke-width:1.8;fill:none;stroke-linecap:round;stroke-linejoin:round}
.empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;color:var(--text3);padding:20px;text-align:center}
.empty svg{opacity:.28}
.empty p{font-size:12px;line-height:1.65}
.gscroll{flex:1;overflow-y:auto;overflow-x:hidden;padding:0 8px 4px;position:relative;min-height:0}
.ggrid{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.gitem{aspect-ratio:1;overflow:hidden;border-radius:var(--rx);background:var(--card);border:1px solid var(--border);cursor:pointer;position:relative;transition:transform var(--tr),border-color var(--tr)}
.gitem:hover{transform:scale(1.04);border-color:var(--border2)}
.gitem.cp{border-color:var(--ok)}
.gitem img{width:100%;height:100%;object-fit:cover}
.govl{position:absolute;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity var(--tr);border-radius:var(--rx)}
.gitem:hover .govl{opacity:1}
.govl svg{color:#fff;width:18px;height:18px;stroke:currentColor;stroke-width:1.6;fill:none}
.gdel{position:absolute;top:3px;right:3px;width:19px;height:19px;background:rgba(0,0,0,.6);border-radius:4px;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity var(--tr);cursor:pointer;z-index:2;color:var(--red)}
.gitem:hover .gdel{opacity:1}
.gdel:hover{background:var(--rbg)}
.gdel svg{width:8px;height:8px;stroke:currentColor;stroke-width:2.5;fill:none;stroke-linecap:round}
.gdrop{position:absolute;inset:4px;border:2px dashed transparent;border-radius:var(--rs);pointer-events:none;transition:all var(--tr);display:flex;align-items:center;justify-content:center;background:transparent;z-index:20}
.gpg.dov .gdrop{border-color:var(--accent);background:var(--abg)}
.drophint{display:flex;flex-direction:column;align-items:center;gap:7px;color:var(--accent);font-size:12px;font-weight:500;opacity:0;transition:opacity var(--tr)}
.gpg.dov .drophint{opacity:1}
.drophint svg{width:26px;height:26px;stroke:currentColor;stroke-width:1.5;fill:none}
.gurlbar{display:flex;gap:6px;padding:6px 8px 8px;flex-shrink:0}
.urlip{flex:1;height:31px;background:var(--elevated);border:1px solid var(--border);border-radius:var(--rx);padding:0 9px;font-size:12px;color:var(--text);transition:border-color var(--tr)}
.urlip:focus{border-color:var(--accent)}
.urlip::placeholder{color:var(--text3)}
.dlbtn{height:31px;padding:0 10px;background:var(--elevated);border:1px solid var(--border);border-radius:var(--rx);font-size:11.5px;color:var(--text2);transition:all var(--tr);white-space:nowrap}
.dlbtn:hover{border-color:var(--accent);color:var(--accent)}
.mbk{position:absolute;inset:0;background:rgba(0,0,0,.68);z-index:100;opacity:0;pointer-events:none;transition:opacity var(--trs);backdrop-filter:blur(3px)}
.mbk.on{opacity:1;pointer-events:auto}
.modal{position:absolute;bottom:0;left:0;right:0;background:var(--surface);border-top:1px solid var(--border);z-index:101;transform:translateY(100%);transition:transform var(--trs) cubic-bezier(.34,1.56,.64,1);display:flex;flex-direction:column;max-height:92%}
.modal.on{transform:translateY(0)}
.mhdr{display:flex;align-items:center;padding:15px 15px 11px;border-bottom:1px solid var(--border);flex-shrink:0}
.mtitle{font-family:'Syne',sans-serif;font-weight:700;font-size:14px;flex:1}
.mcl{width:27px;height:27px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--text2);transition:background var(--tr),color var(--tr)}
.mcl:hover{background:var(--elevated);color:var(--text)}
.mcl svg{width:11px;height:11px;stroke:currentColor;stroke-width:2.5;fill:none;stroke-linecap:round}
.mbody{flex:1;overflow-y:auto;padding:13px 13px 2px;display:flex;flex-direction:column;gap:11px;min-height:0}
.flbl{font-family:'DM Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:5px;display:flex;justify-content:space-between}
.uz{border:1.5px dashed var(--border2);border-radius:var(--rs);min-height:78px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:border-color var(--tr),background var(--tr);position:relative;overflow:hidden}
.uz:hover,.uz.dov{border-color:var(--accent);background:var(--abg)}
.uz.has{border-style:solid;min-height:116px}
.uhint{display:flex;flex-direction:column;align-items:center;gap:6px;color:var(--text3);font-size:11.5px;text-align:center;padding:14px;pointer-events:none}
.uhint svg{opacity:.5;color:var(--accent);width:22px;height:22px;stroke:currentColor;stroke-width:1.5;fill:none}
.uhint span{color:var(--accent);font-weight:500}
.upv{width:100%;padding:6px}

.ugrid {display:grid; grid-template-columns:repeat(3, 1fr); gap:6px;}
.ugitem {aspect-ratio:1; position:relative; border-radius:var(--rx); overflow:hidden; border:1px solid var(--border); background:var(--bg)}
.ugitem img {width:100%; height:100%; object-fit:cover;}
.ugitem .rm {position:absolute; top:2px; right:2px; width:18px; height:18px; background:rgba(0,0,0,.7); color:var(--red); border-radius:3px; display:flex; align-items:center; justify-content:center; cursor:pointer;}
.ugitem .rm:hover {background:var(--rbg)}
.ugadd {aspect-ratio:1; border:1.5px dashed var(--border2); border-radius:var(--rx); display:flex; align-items:center; justify-content:center; color:var(--text3); cursor:pointer; transition:all var(--tr)}
.ugadd:hover {border-color:var(--accent); color:var(--accent); background:var(--abg)}
.ugadd svg {width:20px; height:20px; stroke:currentColor; stroke-width:1.5; fill:none}

.urlrow{display:flex;gap:6px;align-items:center}
.pta{width:100%;min-height:80px;background:var(--elevated);border:1px solid var(--border);border-radius:var(--rx);padding:9px 10px;font-size:12.5px;line-height:1.6;color:var(--text);transition:border-color var(--tr)}
.pta:focus{border-color:var(--accent)}
.pta::placeholder{color:var(--text3)}
.ptagip{width:100%; height:32px; background:var(--elevated);border:1px solid var(--border);border-radius:var(--rx);padding:0 9px;font-size:12px;color:var(--text);transition:border-color var(--tr)}
.ptagip:focus{border-color:var(--accent)}
.ptagip::placeholder{color:var(--text3)}
.mftr{display:flex;gap:7px;padding:11px 13px 15px;flex-shrink:0}
.bp,.bs{flex:1;height:35px;border-radius:var(--rx);font-size:13px;font-weight:500;transition:all var(--tr)}
.bp{background:var(--accent);color:#fff}
.bp:hover{background:var(--accent-d);transform:translateY(-1px)}
.bp:active{transform:translateY(0)}
.bs{background:var(--elevated);border:1px solid var(--border);color:var(--text2)}
.bs:hover{border-color:var(--border2);color:var(--text)}
.pvm{position:absolute;inset:0;background:rgba(0,0,0,.93);z-index:200;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity var(--trs);cursor:pointer}
.pvm.on{opacity:1;pointer-events:auto}
.pvm img{max-width:90%;max-height:80%;border-radius:var(--rs);box-shadow:0 8px 40px rgba(0,0,0,.8);cursor:default;transform:scale(.9);transition:transform var(--trs) cubic-bezier(.34,1.56,.64,1)}
.pvm.on img{transform:scale(1)}
.toast{position:absolute;bottom:50px;left:50%;transform:translateX(-50%) translateY(6px);background:var(--elevated);border:1px solid var(--border2);border-radius:20px;padding:4px 13px;font-size:11.5px;color:var(--text2);white-space:nowrap;z-index:300;opacity:0;transition:opacity var(--tr),transform var(--tr);pointer-events:none}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.ok{color:var(--ok);border-color:rgba(74,222,128,.3)}
.toast.er{color:var(--red);border-color:rgba(248,113,113,.3)}
</style></head>
<body>
<div class="app" id="app">
  <div class="tb pywebview-drag-region" id="tb">
    <span class="tb-icon">✦</span><span class="tb-name">Prompt Manager</span>
    <div class="tb-ctrls">
      <button class="cbtn" id="btnTheme" title="切换主题"><svg viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg></button>
      <button class="cbtn" id="btnMin" title="最小化到托盘"><svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
      <button class="cbtn cl" id="btnClose" title="关闭"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
  </div>
  <div class="nav">
    <button class="ntab on" data-tab="p">提示词</button>
    <button class="ntab" data-tab="g">图库</button>
  </div>
  <div class="pw">
    <div class="pg" id="pgP">
      <div class="ptb"><span class="plbl">提示词</span><span class="badge" id="pcnt">0</span>
        <button class="abtn" id="btnAdd"><svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>新增</button>
      </div>
      <div class="tag-bar" id="tagBar"></div>
      <div class="pscroll" id="plist"></div>
    </div>
    <div class="pg off sl gpg" id="pgG">
      <div class="ptb"><span class="plbl">图库</span><span class="badge" id="gcnt">0</span>
        <button class="abtn" id="btnGUp"><svg viewBox="0 0 24 24" style="width:10px;height:10px;stroke:currentColor;stroke-width:2.5;fill:none;stroke-linecap:round;stroke-linejoin:round"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>上传</button>
      </div>
      <div class="gscroll" id="gscroll">
        <div class="ggrid" id="ggrid"></div>
        <div class="gdrop"><div class="drophint"><svg viewBox="0 0 24 24"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg><span>释放以上传</span></div></div>
      </div>
      <div class="gurlbar"><input class="urlip" id="gurlin" placeholder="输入图片 URL 添加到图库…" type="text"/><button class="dlbtn" id="gurlbtn">下载</button></div>
    </div>
  </div>
  <div class="mbk" id="mbk"></div>
  <div class="modal" id="mmodal">
    <div class="mhdr"><span class="mtitle" id="mtitle">新增提示词</span><button class="mcl" id="btnMCl"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button></div>
    <div class="mbody">
      <div>
        <div class="flbl"><span>图片 (最多6张)</span> <span id="mucnt" style="color:var(--accent)">0/6</span></div>
        <div class="uz" id="uz">
          <div class="uhint" id="uhint"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg><div><span>点击</span> / 拖拽 / <span>Ctrl+V</span></div><div style="color:var(--text3);font-size:11px">粘贴剪贴板图片</div></div>
          <div class="upv" id="upv" style="display:none">
            <div class="ugrid" id="ugrid"></div>
          </div>
        </div>
        <div class="urlrow" style="margin-top:6px"><input class="urlip" id="imgurlip" placeholder="或输入图片 URL 自动下载…" type="text"/><button class="dlbtn" id="btnIDL">下载</button></div>
      </div>
      <div><div class="flbl">提示词 *</div><textarea class="pta" id="pta" placeholder="输入提示词内容…" rows="4"></textarea></div>
      <div><div class="flbl">标签 (可选)</div><input type="text" class="ptagip" id="ptagip" placeholder="如: 建筑, 赛博朋克 (空格或逗号分隔)"/></div>
    </div>
    <div class="mftr"><button class="bs" id="btnCan">取消</button><button class="bp" id="btnSav">保存</button></div>
  </div>
  <div class="pvm" id="pvm"><img id="pvmimg" alt=""/></div>
  <div class="toast" id="toast"></div>
</div>
<input type="file" id="fpi" accept="image/*" multiple style="display:none"/>
<script>
const S={theme:'dark',tab:'p',prompts:[],gallery:[],editId:null,activeTag:null,pos:{x:800,y:190}};
let MImgs = []; 

const $=id=>document.getElementById(id);
const el=(t,c,h='')=>{const e=document.createElement(t);if(c)e.className=c;if(h)e.innerHTML=h;return e};
const uid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,6);
const ts=()=>Date.now();
const esc=s=>(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
let _tt=null;
function toast(msg,type='',ms=1800){const t=$('toast');t.textContent=msg;t.className='toast on'+(type?' '+type:'');clearTimeout(_tt);_tt=setTimeout(()=>t.className='toast',ms);}
const api=()=>window.pywebview&&window.pywebview.api;
async function ca(m,...a){const ap=api();if(!ap)return null;try{return await ap[m](...a)}catch(e){console.error(m,e);return null}}

async function init(){
  const cfg=await ca('get_settings');
  if(cfg){setTheme(cfg.theme||'dark');if(cfg.pos)S.pos=cfg.pos;}
  
  let rawP = await ca('get_prompts')||[];
  S.prompts = rawP.map(p => {
    if(p.imagePath && !p.imagePaths) p.imagePaths = [p.imagePath];
    if(!p.imagePaths) p.imagePaths = [];
    if(!p.tags) p.tags = [];
    return p;
  });
  
  S.gallery=await ca('get_gallery')||[];
  renderTags();
  renderPrompts();
  renderGallery();
  ca('set_preview_theme', S.theme);
  
  // 核心死锁修复：界面完全渲染完成后，安全唤醒后端底层功能
  ca('app_ready');
}
window.addEventListener('pywebviewready',init);
setTimeout(()=>{if(!api())return;init();},600);

function setTheme(t){S.theme=t;document.documentElement.className=t==='light'?'light':'';ca('set_preview_theme',t);}
$('btnTheme').onclick=async()=>{const t=S.theme==='dark'?'light':'dark';setTheme(t);await ca('save_settings',{theme:t,pos:S.pos});};

$('btnMin').onclick=async()=>{
    const res = await ca('minimize_win');
    if(res && !res.ok) toast(res.err, 'er', 3000);
};

$('btnClose').onclick=async()=>{await ca('save_settings',{theme:S.theme,pos:S.pos});ca('close_win');};

document.querySelectorAll('.ntab').forEach(b=>b.addEventListener('click',()=>{
  const tab=b.dataset.tab;if(tab===S.tab)return;
  const prev=S.tab;S.tab=tab;
  document.querySelectorAll('.ntab').forEach(x=>x.classList.toggle('on',x.dataset.tab===tab));
  const order=['p','g'];const fwd=order.indexOf(tab)>order.indexOf(prev);
  const pages={p:$('pgP'),g:$('pgG')};
  pages[prev].classList.toggle('sl',fwd);pages[prev].classList.add('off');
  pages[tab].classList.toggle('sl',!fwd);pages[tab].classList.remove('off');
  ca('hide_preview_now');
}));

const imgCache={};
async function getImg(rel){
  if(!rel)return null;if(imgCache[rel])return imgCache[rel];
  const b=await ca('get_image_b64',rel);if(b)imgCache[rel]=b;return b;
}

const tagBar = $('tagBar');
let isDraggingTag = false;
let tagStartX, tagScrollLeft;

tagBar.addEventListener('mousedown', e => {
  isDraggingTag = false;
  tagBar.dataset.isDown = 'true';
  tagStartX = e.pageX - tagBar.offsetLeft;
  tagScrollLeft = tagBar.scrollLeft;
});
window.addEventListener('mouseup', () => { tagBar.dataset.isDown = 'false'; });
window.addEventListener('mousemove', e => {
  if(tagBar.dataset.isDown !== 'true') return;
  e.preventDefault();
  const x = e.pageX - tagBar.offsetLeft;
  const walk = (x - tagStartX) * 1.5;
  if(Math.abs(walk) > 4) isDraggingTag = true; 
  tagBar.scrollLeft = tagScrollLeft - walk;
});

function renderTags(){
  tagBar.innerHTML = '';
  const allTags = new Set();
  S.prompts.forEach(p => p.tags.forEach(t => allTags.add(t)));
  if(allTags.size === 0){ tagBar.style.display = 'none'; return; }
  tagBar.style.display = 'flex';
  
  const allPill = el('div','tag-pill'+(S.activeTag===null?' on':''),'全部');
  allPill.onclick = () => { if(isDraggingTag) return; S.activeTag = null; renderTags(); renderPrompts(); };
  tagBar.appendChild(allPill);
  
  Array.from(allTags).sort().forEach(tag => {
    const p = el('div','tag-pill'+(S.activeTag===tag?' on':''),esc(tag));
    p.onclick = () => { if(isDraggingTag) return; S.activeTag = tag; renderTags(); renderPrompts(); };
    tagBar.appendChild(p);
  });
}

async function renderPrompts(){
  const list=$('plist');
  
  let viewData = S.prompts;
  if(S.activeTag) viewData = viewData.filter(p => p.tags.includes(S.activeTag));
  
  $('pcnt').textContent = viewData.length;
  list.innerHTML='';
  
  if(!viewData.length){
    list.innerHTML=`<div class="empty"><svg width="36" height="36" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.2" fill="none"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><p>没有符合条件的提示词</p></div>`;
    return;
  }
  for(const p of viewData){
    const hasImg = p.imagePaths && p.imagePaths.length > 0;
    const card=el('div','pcard'+(hasImg?'':' noi'));
    card.dataset.id=p.id;
    
    if(hasImg){
      const wrap=el('div','cimg');
      const img=document.createElement('img');img.alt='';img.loading='lazy';
      wrap.appendChild(img);
      if(p.imagePaths.length > 1){
        const cnt = el('div','icnt', p.imagePaths.length);
        wrap.appendChild(cnt);
      }
      card.appendChild(wrap);
      getImg(p.imagePaths[0]).then(b=>{if(b)img.src=b;});
    }
    
    const body=el('div','cbody');
    if(p.tags && p.tags.length > 0){
      const tdiv = el('div', 'ctags');
      p.tags.forEach(t => tdiv.appendChild(el('span', '', esc(t))));
      body.appendChild(tdiv);
    }
    body.appendChild(el('p','ctxt',esc(p.text)));
    card.appendChild(body);
    
    const acts=el('div','cacts');
    const eb=el('button','iact',`<svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`);
    eb.title='编辑';eb.addEventListener('click',e=>{e.stopPropagation();openModal(p.id);});
    const db=el('button','iact d',`<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>`);
    db.title='删除';db.addEventListener('click',e=>{e.stopPropagation();delPrompt(p.id);});
    acts.appendChild(eb);acts.appendChild(db);card.appendChild(acts);
    
    card.addEventListener('mouseenter',()=>ca('show_preview', p.text, p.imagePaths));
    card.addEventListener('mouseleave',()=>ca('hide_preview'));
    card.addEventListener('click',e=>{if(e.target.closest('.iact'))return;copyP(p.text,card);});
    list.appendChild(card);
  }
}

async function copyP(text,card){
  const ok=await ca('copy_text',text);
  if(ok!==false){card.classList.add('cp');toast('提示词已复制','ok');setTimeout(()=>card.classList.remove('cp'),1200);}
  else toast('复制失败','er');
}
async function delPrompt(id){
  const p=S.prompts.find(x=>x.id===id);
  if(p&&p.imagePaths){
    for(const path of p.imagePaths){
      await ca('delete_file', path); delete imgCache[path];
    }
  }
  S.prompts=S.prompts.filter(x=>x.id!==id);
  await ca('save_prompts',S.prompts);renderTags();renderPrompts();toast('已删除');
}

async function updateMImgUI(){
  const grid = $('ugrid');
  grid.innerHTML = '';
  $('mucnt').textContent = MImgs.length + '/6';
  
  if(MImgs.length === 0){
    $('uhint').style.display='';
    $('upv').style.display='none';
    $('uz').classList.remove('has');
    return;
  }
  
  $('uhint').style.display='none';
  $('upv').style.display='block';
  $('uz').classList.add('has');
  
  for(let i=0; i<MImgs.length; i++){
    const item = MImgs[i];
    const elDiv = el('div', 'ugitem');
    const img = document.createElement('img');
    if(item.type === 'b64') img.src = item.val;
    else if(item.type === 'path'){
       const b = await getImg(item.val);
       if(b) img.src = b;
    }
    const rm = el('div', 'rm', `<svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:currentColor;stroke-width:2.5;fill:none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`);
    rm.onclick = (e) => { e.stopPropagation(); MImgs.splice(i, 1); updateMImgUI(); };
    elDiv.appendChild(img); elDiv.appendChild(rm);
    grid.appendChild(elDiv);
  }
  
  if(MImgs.length < 6){
    const addBtn = el('div', 'ugadd', `<svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`);
    grid.appendChild(addBtn);
  }
}

async function handleMImgAdd(b64){
  if(MImgs.length >= 6) { toast('最多添加6张图片', 'er'); return; }
  MImgs.push({type: 'b64', val: b64});
  updateMImgUI();
}

async function savePrompt(){
  const text=$('pta').value.trim();if(!text){toast('请输入提示词内容','er');return;}
  const tagStr=$('ptagip').value;
  const tags = tagStr.split(/[,，\s]+/).map(t=>t.trim()).filter(t=>t);
  
  let finalPaths = [];
  for(const img of MImgs){
    if(img.type === 'path') finalPaths.push(img.val);
    else if(img.type === 'b64') {
      const r = await ca('upload_image', img.val, 'prompts');
      if(r && r.ok) finalPaths.push(r.path);
    }
  }

  if(S.editId){
    const idx=S.prompts.findIndex(x=>x.id===S.editId);
    if(idx>-1){
      const old=S.prompts[idx];
      const oldPaths = old.imagePaths || [];
      for(const op of oldPaths){
        if(!finalPaths.includes(op)){ await ca('delete_file', op); delete imgCache[op]; }
      }
      S.prompts[idx]={...old,text,tags,imagePaths:finalPaths,updatedAt:ts()};
    }
    toast('已更新','ok');
  }else{
    S.prompts.unshift({id:uid(),text,tags,imagePaths:finalPaths,createdAt:ts()});
    toast('已添加','ok');
  }
  await ca('save_prompts',S.prompts);renderTags();renderPrompts();closeModal();
}

function openModal(editId=null){
  S.editId=editId; MImgs=[];
  $('mtitle').textContent=editId?'编辑提示词':'新增提示词';
  $('pta').value='';$('imgurlip').value='';$('ptagip').value='';
  
  if(editId){
    const p=S.prompts.find(x=>x.id===editId);
    if(p){
      $('pta').value=p.text;
      if(p.tags) $('ptagip').value = p.tags.join(', ');
      if(p.imagePaths) {
        p.imagePaths.forEach(path => MImgs.push({type: 'path', val: path}));
      }
    }
  }
  updateMImgUI();
  $('mbk').classList.add('on');$('mmodal').classList.add('on');
  ca('hide_preview_now');
  setTimeout(()=>$('pta').focus(),300);
}

function closeModal(){$('mbk').classList.remove('on');$('mmodal').classList.remove('on');S.editId=null; MImgs=[];}
$('btnAdd').onclick=()=>openModal();
$('btnMCl').onclick=closeModal;$('btnCan').onclick=closeModal;$('btnSav').onclick=savePrompt;
$('mbk').onclick=e=>{if(e.target===$('mbk'))closeModal();};

$('uz').addEventListener('click',e=>{if(e.target.closest('.rm'))return;$('fpi').dataset.t='modal';$('fpi').click();});
$('fpi').addEventListener('change',async e=>{
  const files=[...(e.target.files||[])];if(!files.length)return;
  const t=$('fpi').dataset.t;
  if(t==='modal'){
     for(const f of files){
       if(MImgs.length>=6) break;
       const b=await f2b(f); await handleMImgAdd(b);
     }
  }else{
     for(const f of files)await addGImg(f);
  }
  e.target.value='';
});

const uzEl=$('uz');
uzEl.addEventListener('dragover',e=>{e.preventDefault();uzEl.classList.add('dov');});
uzEl.addEventListener('dragleave',e=>{if(!uzEl.contains(e.relatedTarget))uzEl.classList.remove('dov');});
uzEl.addEventListener('drop',async e=>{
  e.preventDefault();uzEl.classList.remove('dov');
  const files = e.dataTransfer.files;
  for(const f of files){
    if(MImgs.length>=6) break;
    if(f.type.startsWith('image/')){ const b=await f2b(f); await handleMImgAdd(b); }
  }
});

$('btnIDL').addEventListener('click',async()=>{
  const url=$('imgurlip').value.trim();if(!url)return;
  if(MImgs.length >= 6) { toast('最多添加6张图片', 'er'); return; }
  toast('下载中…');
  const r=await ca('download_image',url,'prompts');
  if(r&&r.ok){
    MImgs.push({type:'path', val:r.path});
    delete imgCache[r.path]; 
    updateMImgUI();
    $('imgurlip').value='';toast('已下载','ok');
  } else toast(r?r.err:'下载失败','er');
});

async function renderGallery(){
  const grid=$('ggrid');$('gcnt').textContent=S.gallery.length;grid.innerHTML='';
  if(!S.gallery.length){grid.innerHTML=`<div class="empty" style="grid-column:1/-1"><svg width="36" height="36" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.2" fill="none"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg><p>图库为空<br/>拖拽图片或点击「上传」</p></div>`;return;}
  for(const g of S.gallery){
    const item=el('div','gitem');item.dataset.id=g.id;
    const img=document.createElement('img');img.alt='';img.loading='lazy';
    const ovl=el('div','govl','<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>');
    const del=el('div','gdel','<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>');
    del.title='删除';del.addEventListener('click',e=>{e.stopPropagation();delGallery(g.id);});
    item.appendChild(img);item.appendChild(ovl);item.appendChild(del);
    getImg(g.imagePath).then(b=>{if(b)img.src=b;});
    let _ct=null;
    item.addEventListener('click',()=>{_ct=setTimeout(async()=>{const ok=await ca('copy_image_file',g.imagePath);if(ok!==false){item.classList.add('cp');toast('图片已复制','ok');setTimeout(()=>item.classList.remove('cp'),1200);}else toast('复制失败','er');},220);});
    item.addEventListener('dblclick',()=>{clearTimeout(_ct);getImg(g.imagePath).then(b=>{if(b){$('pvmimg').src=b;$('pvm').classList.add('on');}});});
    grid.appendChild(item);
  }
}
async function delGallery(id){
  const g=S.gallery.find(x=>x.id===id);if(g){await ca('delete_file',g.imagePath);delete imgCache[g.imagePath];}
  S.gallery=S.gallery.filter(x=>x.id!==id);await ca('save_gallery',S.gallery);renderGallery();toast('已删除');
}
async function addGImg(file){const b=await f2b(file);const r=await ca('upload_image',b,'gallery');if(r&&r.ok){S.gallery.push({id:uid(),imagePath:r.path,createdAt:ts()});await ca('save_gallery',S.gallery);renderGallery();}else toast('上传失败','er');}
async function addGImgB64(b){const r=await ca('upload_image',b,'gallery');if(r&&r.ok){S.gallery.push({id:uid(),imagePath:r.path,createdAt:ts()});await ca('save_gallery',S.gallery);renderGallery();toast('已添加到图库','ok');}}
$('btnGUp').onclick=()=>{$('fpi').dataset.t='gallery';$('fpi').click();};
const gpg=$('pgG');
gpg.addEventListener('dragenter',e=>{e.preventDefault();gpg.classList.add('dov');});
gpg.addEventListener('dragover',e=>e.preventDefault());
gpg.addEventListener('dragleave',e=>{if(!gpg.contains(e.relatedTarget))gpg.classList.remove('dov');});
gpg.addEventListener('drop',async e=>{e.preventDefault();gpg.classList.remove('dov');for(const f of e.dataTransfer.files)if(f.type.startsWith('image/'))await addGImg(f);});
$('gurlbtn').addEventListener('click',async()=>{
  const url=$('gurlin').value.trim();if(!url)return;toast('下载中…');
  const r=await ca('download_image',url,'gallery');
  if(r&&r.ok){S.gallery.push({id:uid(),imagePath:r.path,createdAt:ts()});await ca('save_gallery',S.gallery);renderGallery();$('gurlin').value='';toast('已添加','ok');}
  else toast(r?r.err:'下载失败','er');
});
$('gurlin').addEventListener('keydown',e=>{if(e.key==='Enter')$('gurlbtn').click();});
$('pvm').addEventListener('click',e=>{if(e.target===$('pvm'))$('pvm').classList.remove('on');});

document.addEventListener('keydown',async e=>{
  if(!e.ctrlKey||e.key.toLowerCase()!=='v')return;
  const act=document.activeElement;
  
  if($('mmodal').classList.contains('on')){
    if(act.tagName==='TEXTAREA'||(act.tagName==='INPUT'&&act!==$('imgurlip')&&act!==$('ptagip')))return;
    e.preventDefault();
    const b=await ca('get_clipboard_image');
    if(b){ await handleMImgAdd(b); toast('图片已粘贴','ok');} else toast('剪贴板无图片','er');
    return;
  }
  
  if(act.tagName==='INPUT'||act.tagName==='TEXTAREA')return;
  e.preventDefault();

  const b = await ca('get_clipboard_image');
  if(b){
    if(S.tab !== 'g'){ const gTabBtn = document.querySelector('.ntab[data-tab="g"]'); if(gTabBtn) gTabBtn.click(); }
    await addGImgB64(b);
    return;
  }

  const text = await ca('get_clipboard_text');
  if(text && text.trim()){
    const newText = text.trim();
    if(S.tab !== 'p'){ const pTabBtn = document.querySelector('.ntab[data-tab="p"]'); if(pTabBtn) pTabBtn.click(); }
    S.prompts.unshift({id:uid(), text:newText, tags:[], imagePaths:[], createdAt:ts()});
    await ca('save_prompts', S.prompts);
    renderPrompts();
    toast('已快速新增提示词','ok');
  } else {
    toast('剪贴板中没有有效图片或文本','er');
  }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if($('pvm').classList.contains('on')){$('pvm').classList.remove('on');return;}
    if($('mmodal').classList.contains('on')){closeModal();return;}
  }
  if(e.ctrlKey&&e.key==='n'&&S.tab==='p'){e.preventDefault();openModal();}
});
function f2b(file){return new Promise((res,rej)=>{const r=new FileReader();r.onload=e=>res(e.target.result);r.onerror=rej;r.readAsDataURL(file);});}

document.addEventListener('dragenter', (e) => {
  if (e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
    const isModalOpen = $('mmodal').classList.contains('on');
    if (!isModalOpen && S.tab !== 'g') {
      const gTabBtn = document.querySelector('.ntab[data-tab="g"]');
      if (gTabBtn) gTabBtn.click();
    }
  }
});
document.addEventListener('dragover', (e) => {
  if (e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {
    e.preventDefault();
  }
});
</script>
</body></html>"""


def create_tray_icon():
    # 优先加载当前目录的 icon.ico
    icon_path = BASE / "icon.ico"
    if icon_path.exists():
        try:
            return _PIL.open(str(icon_path))
        except:
            pass
    # 备用方案：生成默认紫色正方形
    return _PIL.new('RGB', (64, 64), color=(167, 139, 250))

def setup_tray():
    if not HAS_TRAY: return
    
    def on_show(icon, item):
        if _win: 
            _win.show()
            _win.restore()
        if getattr(globals().get('_docker', None), 'state', None) == EdgeDocker.DOCKED:
            _docker.force_peek()
        
    def on_exit(icon, item):
        icon.stop()
        if _prev_win:
            try: _prev_win.destroy()
            except: pass
        if _win: _win.destroy()
        import os
        os._exit(0)

    image = create_tray_icon()
    menu = pystray.Menu(item('显示主界面', on_show, default=True), item('退出', on_exit))
    
    global tray_icon
    tray_icon = pystray.Icon("PromptHub", image, "Prompt Manager", menu)
    tray_icon.run_detached()

def main():
    global _win, _prev_win
    
    api_inst_main = API()
    api_inst_prev = API()
    
    cfg = api_inst_main.get_settings()
    pos = cfg.get("pos", {"x": 800, "y": 190})
    _pos.update(pos)

    _win = webview.create_window(
        title="Prompt Hub", html=HTML, js_api=api_inst_main,
        width=WIN_W, height=WIN_H,
        x=pos.get("x", 800), y=pos.get("y", 190),
        resizable=False, frameless=True, easy_drag=False,
        background_color="#0c0c0c",
    )
    api_inst_main.window = _win

    _prev_win = webview.create_window(
        title="ph_preview", html=PREV_HTML, js_api=api_inst_prev,
        width=PREV_W, height=WIN_H, x=-9999, y=-9999,
        resizable=False, frameless=True, easy_drag=False,
        background_color="#0c0c0c",
    )

    # 去掉所有可能引发竞争的后台代码，保持干净启动
    webview.start(debug=False)

if __name__ == "__main__":
    main()
