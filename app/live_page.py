"""Live activity page (no external JS/CSS). Token comes from ?token= URL."""

LIVE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>reelbot :: live</title>
<style>
  body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px}
  .wrap{max-width:760px;margin:0 auto}
  h1{color:#58a6ff;font-size:20px}
  .card{border:1px solid #30363d;border-radius:8px;padding:16px;margin:16px 0;background:#161b22}
  #term{background:#000;border:1px solid #30363d;border-radius:8px;padding:12px;height:320px;overflow-y:auto;white-space:pre-wrap;font-size:13px}
  .ok{color:#3fb950}.err{color:#f85149}.dim{color:#8b949e}.warn{color:#d29922}
  .stat{color:#79c0ff}
</style>
</head>
<body>
<div class="wrap">
<h1>$ reelbot live <span class="dim" id="sub">— connecting…</span></h1>
<div class="card"><div class="dim">latest run</div><div id="run" class="stat">…</div></div>
<div class="card"><div class="dim">activity</div><div id="term">$ waiting…</div></div>
<div class="card dim">Tip: trigger a run by opening <span class="stat">/upload?token=YOUR_SECRET</span>
in a new tab — watch it here live. Bookmark both URLs (token included) for one-click access.</div>
</div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const term=document.getElementById('term');
async function poll(){
  try{
    const r=await fetch('/api/activity?token='+encodeURIComponent(token));
    if(r.status===401){document.getElementById('sub').textContent='— unauthorized: bad ?token=';return;}
    const j=await r.json();
    document.getElementById('sub').textContent='— '+j.service_time;
    const run=j.last_run;
    document.getElementById('run').textContent=run?('#'+run.id+' '+run.status+' | found:'+run.reels_found+' skipped:'+run.reels_skipped+' uploaded:'+run.reels_uploaded+' failed:'+run.reels_failed+' | '+run.finished_at):'no runs yet';
    term.innerHTML='';
    (j.events||[]).forEach(l=>{
      const d=document.createElement('div');
      if(/success|DONE|COMPLETED|completed/.test(l))d.className='ok';
      else if(/FAILED|failed|error/i.test(l))d.className='err';
      else if(/SKIP|busy|warning|requested but UNSUPPORTED/i.test(l))d.className='warn';
      d.textContent=l;term.appendChild(d);
    });
    term.scrollTop=term.scrollHeight;
  }catch(e){document.getElementById('sub').textContent='— connection error, retrying…';}
}
setInterval(poll,2000);poll();
</script>
</body>
</html>
"""
