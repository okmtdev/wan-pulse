"""Local web app: browse, play, and label recorded segments.

A dependency-free companion UI (Python's stdlib http.server) that scans the
recordings directory, pairs each .wav with its sidecar .json, and shows them in
a browsable list with inline audio playback. It's the foundation for the
labelling loop: you can attach a human review (dog? / emotion / note) to each
segment, stored back into the sidecar JSON under a `review` key without touching
the automatic classification fields.

Bind to 127.0.0.1 by default (local only). It can run standalone (`wan-pulse
web`) or alongside capture (`[web] enabled = true` / `wan-pulse run --web`).
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .logsetup import get_logger

log = get_logger()

# Classification fields surfaced to the UI (everything else in the sidecar is
# kept as-is; we only read these).
_META_KEYS = (
    "is_dog", "dog_label", "dog_score",
    "emotion", "emotion_basis", "emotion_score",
    "top_label", "top_score", "top_label_ns", "top_score_ns",
    "peak_dbfs", "duration_sec", "timestamp",
)


def _safe_wav(output_dir: Path, rel: str) -> Path:
    """Resolve a client-supplied relative path safely under output_dir."""
    base = output_dir.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise PermissionError(f"path escapes output dir: {rel}")
    if target.suffix != ".wav":
        raise ValueError("not a .wav path")
    return target


def scan_segments(output_dir: str | Path) -> list[dict]:
    """List every .wav under output_dir with its sidecar metadata + review."""
    base = Path(output_dir)
    items: list[dict] = []
    if not base.exists():
        return items
    for wav in base.rglob("*.wav"):
        sidecar = wav.with_suffix(".json")
        meta: dict = {}
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
        st = wav.stat()
        items.append({
            "id": wav.relative_to(base).as_posix(),
            "name": wav.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "modified": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "has_sidecar": sidecar.exists(),
            "meta": {k: meta.get(k) for k in _META_KEYS},
            "review": meta.get("review"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def stats(segments: list[dict]) -> dict:
    return {
        "total": len(segments),
        "dogs": sum(1 for s in segments if (s["meta"] or {}).get("is_dog")),
        "reviewed": sum(1 for s in segments if s["review"]),
    }


def save_review(output_dir: str | Path, rel: str, review: dict) -> dict:
    """Merge a human review into the segment's sidecar JSON (creating it if needed)."""
    wav = _safe_wav(Path(output_dir), rel)
    sidecar = wav.with_suffix(".json")
    data: dict = {}
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    clean = {
        "is_dog": review.get("is_dog"),          # True / False / None
        "emotion": (review.get("emotion") or "").strip(),
        "note": (review.get("note") or "").strip(),
        "reviewed_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    data["review"] = clean
    data.setdefault("file", wav.name)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def _make_handler(output_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wan-pulse-web"

        def log_message(self, fmt, *args):  # quiet; route to our logger at debug
            log.debug("[web] " + fmt, *args)

        def _send(self, code, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/segments":
                segs = scan_segments(output_dir)
                self._json({"segments": segs, "stats": stats(segs)})
            elif path == "/audio":
                self._serve_audio(query.get("path", [""])[0])
            else:
                self._json({"error": "not found"}, 404)

        def _serve_audio(self, rel: str):
            try:
                wav = _safe_wav(output_dir, rel)
            except (PermissionError, ValueError):
                self._json({"error": "forbidden"}, 403)
                return
            if not wav.is_file():
                self._json({"error": "not found"}, 404)
                return
            self._send(200, wav.read_bytes(), "audio/wav")

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/review":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                review = save_review(output_dir, payload["path"], payload)
            except (PermissionError, ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"ok": True, "review": review})

    return Handler


def build_server(output_dir: str | Path, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(Path(output_dir)))


def serve_in_background(output_dir: str | Path, host: str, port: int) -> ThreadingHTTPServer:
    """Start the web app on a daemon thread (for running alongside capture)."""
    httpd = build_server(output_dir, host, port)
    threading.Thread(target=httpd.serve_forever, name="wan-pulse-web", daemon=True).start()
    log.info("[wan-pulse] web app: http://%s:%d  (browsing ./%s/)", host, port, output_dir)
    return httpd


def run_web(output_dir: str | Path, host: str, port: int) -> None:
    """Run the web app in the foreground until interrupted (`wan-pulse web`)."""
    httpd = build_server(output_dir, host, port)
    log.info("[wan-pulse] web app: http://%s:%d  (browsing ./%s/, Ctrl+C to stop)",
             host, port, output_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("[wan-pulse] web app stopping...")
    finally:
        httpd.shutdown()


INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>wan-pulse local admin</title>
<style>
  :root { --green:#0b6b4f; --rust:#7a2e16; --bg:#f6f7f5; --card:#fff; --line:#e3e6e1; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:#1c1f1d; }
  header { background:#1c1f1d; color:#fff; padding:14px 20px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  .stats { font-size:13px; color:#b9c2bd; }
  .filters { margin-left:auto; display:flex; gap:6px; }
  .filters button, .reload { background:#2c302d; color:#fff; border:1px solid #3a3f3b; border-radius:6px; padding:6px 10px; cursor:pointer; font-size:13px; }
  .filters button.active { background:var(--green); border-color:var(--green); }
  main { max-width:900px; margin:18px auto 130px; padding:0 16px; }
  .seg { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }
  .seg.active { outline:3px solid var(--green); outline-offset:1px; }
  .seg .row1 { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .when { font-weight:600; }
  .name { color:#7b827d; font-size:12px; word-break:break-all; }
  .badge { font-size:12px; padding:2px 8px; border-radius:999px; }
  .badge.dog { background:#e7f3ee; color:var(--green); }
  .badge.notdog { background:#eee; color:#888; }
  .badge.emo { background:#f3e7e2; color:var(--rust); }
  .badge.rev { background:#fff5d6; color:#7a5b00; }
  .meta { color:#5c635e; font-size:12px; margin:6px 0; }
  audio { width:100%; margin:6px 0; }
  .review { border-top:1px dashed var(--line); margin-top:10px; padding-top:10px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .review label { font-size:12px; color:#5c635e; }
  .review select, .review input { padding:5px 7px; border:1px solid var(--line); border-radius:6px; font-size:13px; }
  .review input.note { flex:1; min-width:160px; }
  .review button { background:var(--green); color:#fff; border:0; border-radius:6px; padding:6px 14px; cursor:pointer; font-size:13px; }
  .review .saved { color:var(--green); font-size:12px; }
  .empty { text-align:center; color:#9aa19b; padding:40px; }
  #cheats { position:fixed; right:14px; bottom:14px; max-width:360px; pointer-events:none;
    background:rgba(28,31,29,.92); color:#e8ece9; font-size:12px; line-height:1.7;
    padding:10px 12px; border-radius:10px; box-shadow:0 4px 16px rgba(0,0,0,.25); }
  #cheats b { color:#fff; }
  #cheats kbd { background:#3a3f3b; border-radius:4px; padding:0 5px; font-family:ui-monospace,monospace; }
  #cheats .cont.on { color:#7fe3b6; font-weight:600; }
  #cheats.hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>🐕 wan-pulse <span style="font-weight:400;font-size:13px;color:#b9c2bd">local admin</span></h1>
  <span class="stats" id="stats"></span>
  <div class="filters">
    <button data-f="all" class="active">すべて</button>
    <button data-f="dogs">犬のみ</button>
    <button data-f="unreviewed">未レビュー</button>
    <button class="reload" id="reload">↻</button>
  </div>
</header>
<main id="list"><div class="empty">読み込み中…</div></main>
<div id="cheats">
  <b>キーボード</b> · <span class="cont" id="contState"></span><br>
  <kbd>j</kbd>/<kbd>k</kbd> 移動 · <kbd>Space</kbd> 再生 · <kbd>←</kbd><kbd>→</kbd> シーク · <kbd>r</kbd> 頭出し<br>
  <kbd>d</kbd> 犬 / <kbd>x</kbd> 非 / <kbd>u</kbd> 未 · <kbd>1</kbd>-<kbd>6</kbd> 感情 / <kbd>0</kbd> 解除<br>
  <kbd>m</kbd> メモ · <kbd>Enter</kbd> 保存&次 · <kbd>c</kbd> 連続再生 · <kbd>f</kbd> 絞込 · <kbd>?</kbd> 表示切替
</div>
<script>
let SEGS=[], FILTER="all", VISIBLE=[], CARDS=[], ACTIVE=0, CURRENT=null, CONTINUOUS=false;
const EMOTIONS=["威嚇・警戒","警戒・興奮","警戒","遠吠え（呼びかけ・寂しさ）","不安・甘え","興奮・驚き"];
const $=(s,e=document)=>e.querySelector(s);
const fmtSize=b=>b>1048576?(b/1048576).toFixed(1)+"MB":Math.round(b/1024)+"KB";
const num=x=>(x===null||x===undefined)?"–":(typeof x==="number"?x.toFixed(2):x);

async function load(){
  const r=await fetch("/api/segments"); const d=await r.json(); SEGS=d.segments;
  $("#stats").textContent=`全 ${d.stats.total} 件 ・ 犬 ${d.stats.dogs} ・ レビュー済 ${d.stats.reviewed}`;
  render();
}
function visible(){
  if(FILTER==="dogs") return SEGS.filter(s=>s.meta&&s.meta.is_dog);
  if(FILTER==="unreviewed") return SEGS.filter(s=>!s.review);
  return SEGS;
}
function render(){
  VISIBLE=visible(); const list=$("#list"); CARDS=[];
  if(!VISIBLE.length){ list.innerHTML='<div class="empty">該当する区間がありません</div>'; return; }
  list.innerHTML="";
  VISIBLE.forEach(s=>{ const c=card(s); list.appendChild(c); CARDS.push(c); });
  if(ACTIVE>=CARDS.length) ACTIVE=CARDS.length-1; if(ACTIVE<0) ACTIVE=0;
  applyActive(false);
}
function applyActive(scroll=true){
  CARDS.forEach((c,i)=>c.classList.toggle("active",i===ACTIVE));
  if(scroll&&CARDS[ACTIVE]) CARDS[ACTIVE].scrollIntoView({block:"nearest"});
}
function setActive(i){ if(!CARDS.length)return; ACTIVE=Math.max(0,Math.min(CARDS.length-1,i)); applyActive(); }
const aCard=()=>CARDS[ACTIVE], aSeg=()=>VISIBLE[ACTIVE], audioOf=c=>c&&c.querySelector("audio");
function play(c){ const a=audioOf(c); if(!a)return; if(CURRENT&&CURRENT!==a)CURRENT.pause(); CURRENT=a; a.paused?a.play():a.pause(); }
function playFromStart(c){ const a=audioOf(c); if(!a)return; if(CURRENT&&CURRENT!==a)CURRENT.pause(); CURRENT=a; a.currentTime=0; a.play(); }
function seek(c,d){ const a=audioOf(c); if(a)a.currentTime=Math.max(0,a.currentTime+d); }
function setDog(c,v){ if(c)c.querySelector(".isdog").value=v; }
function setEmotion(c,t){ if(c)c.querySelector(".emotion").value=t; }
function setFilter(f){ FILTER=f; ACTIVE=0;
  document.querySelectorAll(".filters button[data-f]").forEach(x=>x.classList.toggle("active",x.dataset.f===f));
  render(); }
function cycleFilter(){ const o=["all","dogs","unreviewed"]; setFilter(o[(o.indexOf(FILTER)+1)%o.length]); }
function updateMode(){ const e=$("#contState"); e.textContent="連続再生:"+(CONTINUOUS?"ON":"OFF"); e.classList.toggle("on",CONTINUOUS); }

function card(s){
  const m=s.meta||{}, rv=s.review||{};
  const el=document.createElement("div"); el.className="seg";
  const when=m.timestamp||s.modified;
  const dogBadge=m.is_dog?'<span class="badge dog">犬</span>':(s.has_sidecar?'<span class="badge notdog">非犬</span>':'');
  const emoBadge=m.emotion?`<span class="badge emo">${m.emotion}</span>`:'';
  el.innerHTML=`
    <div class="row1"><span class="when">${when}</span>${dogBadge}${emoBadge}
      <span class="revslot">${s.review?'<span class="badge rev">レビュー済</span>':''}</span>
      <span class="name">${s.name}</span></div>
    <div class="meta">peak ${num(m.peak_dbfs)}dBFS ・ ${num(m.duration_sec)}s ・
      top: ${m.top_label||"–"} ${num(m.top_score)}${m.top_label_ns&&m.top_label_ns!==m.top_label?` → ${m.top_label_ns} ${num(m.top_score_ns)}`:""} ・ dog ${num(m.dog_score)} ・ ${fmtSize(s.size)}</div>
    <audio controls preload="none" src="/audio?path=${encodeURIComponent(s.id)}"></audio>
    <div class="review">
      <label>犬?</label>
      <select class="isdog">
        <option value="">（未設定）</option>
        <option value="true">はい</option>
        <option value="false">いいえ</option>
      </select>
      <input class="emotion" placeholder="感情（任意）" style="width:160px">
      <input class="note" placeholder="メモ（任意）">
      <button>保存</button><span class="saved"></span>
    </div>`;
  if(rv.is_dog===true) $(".isdog",el).value="true"; else if(rv.is_dog===false) $(".isdog",el).value="false";
  $(".emotion",el).value=rv.emotion||(m.emotion||"");
  $(".note",el).value=rv.note||"";
  if(rv.reviewed_at) $(".saved",el).textContent="保存済 "+rv.reviewed_at;
  const a=$("audio",el);
  a.addEventListener("ended",()=>{ if(CONTINUOUS&&a===audioOf(aCard())&&ACTIVE<CARDS.length-1){ setActive(ACTIVE+1); playFromStart(aCard()); } });
  $("button",el).onclick=()=>saveCard(el,s);
  return el;
}
async function saveCard(el,s,after){
  const body={ path:s.id, emotion:$(".emotion",el).value, note:$(".note",el).value,
    is_dog: $(".isdog",el).value===""?null:$(".isdog",el).value==="true" };
  const saved=$(".saved",el);
  try{
    const r=await fetch("/api/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){ s.review=d.review; saved.textContent="保存しました "+d.review.reviewed_at;
      if(!$(".revslot .badge",el)) $(".revslot",el).innerHTML='<span class="badge rev">レビュー済</span>';
      if(after) after();
    } else saved.textContent="保存失敗: "+(d.error||"");
  }catch(e){ saved.textContent="保存失敗: "+e; }
}

document.querySelectorAll(".filters button[data-f]").forEach(b=>b.onclick=()=>setFilter(b.dataset.f));
$("#reload").onclick=load;

document.addEventListener("keydown",(e)=>{
  const tag=document.activeElement&&document.activeElement.tagName;
  const typing=tag==="INPUT"||tag==="TEXTAREA"||tag==="SELECT";
  if(e.key==="Escape"){ if(document.activeElement)document.activeElement.blur(); return; }
  if(typing) return;
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  const c=aCard();
  switch(e.key){
    case "j": case "ArrowDown": setActive(ACTIVE+1); e.preventDefault(); break;
    case "k": case "ArrowUp": setActive(ACTIVE-1); e.preventDefault(); break;
    case "g": setActive(0); break;
    case "G": setActive(CARDS.length-1); break;
    case " ": if(c){play(c);} e.preventDefault(); break;
    case "ArrowLeft": seek(c,-2); e.preventDefault(); break;
    case "ArrowRight": seek(c,2); e.preventDefault(); break;
    case "r": playFromStart(c); break;
    case "c": CONTINUOUS=!CONTINUOUS; updateMode(); break;
    case "d": setDog(c,"true"); break;
    case "x": setDog(c,"false"); break;
    case "u": setDog(c,""); break;
    case "m": if(c){ $(".note",c).focus(); e.preventDefault(); } break;
    case "s": case "Enter": if(c) saveCard(c,aSeg(),()=>setActive(ACTIVE+1)); e.preventDefault(); break;
    case "f": cycleFilter(); break;
    case "?": $("#cheats").classList.toggle("hidden"); break;
    default:
      if(/^[0-6]$/.test(e.key)&&c){ const n=+e.key; setEmotion(c,n===0?"":(EMOTIONS[n-1]||"")); }
  }
});
updateMode(); load();
</script>
</body>
</html>
"""
