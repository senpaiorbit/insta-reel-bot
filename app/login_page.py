"""Terminal-style /login page (no external JS/CSS — works offline)."""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>reelbot :: login</title>
<style>
  body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px}
  .wrap{max-width:760px;margin:0 auto}
  h1{color:#58a6ff;font-size:20px}
  .card{border:1px solid #30363d;border-radius:8px;padding:16px;margin:16px 0;background:#161b22}
  label{display:block;margin:10px 0 4px;color:#8b949e;font-size:13px}
  input{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:10px;font-family:inherit;font-size:14px}
  button{background:#238636;color:#fff;border:0;border-radius:6px;padding:10px 18px;font-family:inherit;font-size:14px;cursor:pointer;margin-top:12px}
  button:disabled{background:#30363d;cursor:default}
  #term{background:#000;border:1px solid #30363d;border-radius:8px;padding:12px;height:300px;overflow-y:auto;white-space:pre-wrap;font-size:13px}
  .ok{color:#3fb950}.err{color:#f85149}.dim{color:#8b949e}
  #codeBox,#resultBox{display:none}
  #sessionOut{background:#000;border:1px solid #30363d;border-radius:8px;padding:12px;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:12px}
  .row{display:flex;gap:8px}
</style>
</head>
<body>
<div class="wrap">
<h1>$ reelbot login <span class="dim">— generate INSTAGRAM_SESSION</span></h1>

<div class="card" id="authCard">
  <label>BOT SECRET (UPLOAD_SECRET from Render env — never the IG password)</label>
  <input id="secret" type="password" autocomplete="off" placeholder="paste UPLOAD_SECRET">
  <label>INSTAGRAM USERNAME</label>
  <input id="username" autocomplete="username" placeholder="your_ig_username">
  <label>INSTAGRAM PASSWORD (sent once over HTTPS, never stored or logged)</label>
  <input id="password" type="password" autocomplete="current-password" placeholder="••••••••">
  <label>GMAIL (optional — same email as the IG account; enables auto-verification)</label>
  <input id="email" autocomplete="email" placeholder="you@gmail.com">
  <label>GMAIL APP PASSWORD (optional — Google Account → 2-Step Verification → App passwords)</label>
  <input id="apppw" type="password" autocomplete="off" placeholder="xxxx xxxx xxxx xxxx">
  <button id="startBtn" onclick="startLogin()">connect</button>
</div>

<div class="card">
  <div class="dim">terminal</div>
  <div id="term">$ waiting to connect…</div>
</div>

<div class="card" id="codeBox">
  <label>VERIFICATION CODE (Instagram emailed/SMSed you a 6-digit code)</label>
  <div class="row"><input id="code" inputmode="numeric" placeholder="123456">
  <button onclick="sendCode()">submit code</button></div>
</div>

<div class="card" id="resultBox">
  <div class="ok">session ready ✔ <span class="dim" id="who"></span></div>
  <label>SESSION JSON — paste this whole blob as Render env var INSTAGRAM_SESSION</label>
  <div id="sessionOut"></div>
  <div class="row">
    <button onclick="copySession()">copy</button>
    <button onclick="downloadSession()">download session.json</button>
  </div>
</div>
</div>

<script>
let jobId=null, timer=null, sessionText="";
const term=document.getElementById('term');
function hdr(){return {'Content-Type':'application/json','Authorization':'Bearer '+document.getElementById('secret').value.trim()};}
function printLn(s,cls){const d=document.createElement('div');if(cls)d.className=cls;d.textContent=s;term.appendChild(d);term.scrollTop=term.scrollHeight;}
async function startLogin(){
  const u=document.getElementById('username').value.trim(),p=document.getElementById('password').value;
  const e=document.getElementById('email').value.trim(),a=document.getElementById('apppw').value;
  if(!document.getElementById('secret').value.trim()){alert('Enter the bot secret first');return;}
  if(!u||!p){alert('Enter Instagram username + password');return;}
  document.getElementById('startBtn').disabled=true;
  document.getElementById('password').value='';
  document.getElementById('apppw').value='';
  term.innerHTML='';
  printLn('$ login --user '+u,'dim');
  const r=await fetch('/login/start',{method:'POST',headers:hdr(),body:JSON.stringify({username:u,password:p,email:e,app_password:a})});
  if(r.status===401){printLn('unauthorized: wrong bot secret', 'err');document.getElementById('startBtn').disabled=false;return;}
  const j=await r.json(); jobId=j.job_id; printLn('$ job '+jobId,'dim');
  timer=setInterval(poll,1500); poll();
}
let seen=0;
async function poll(){
  const r=await fetch('/login/status/'+jobId,{headers:hdr()});
  const j=await r.json();
  (j.logs||[]).slice(seen).forEach(l=>printLn(l)); seen=(j.logs||[]).length;
  if(j.status==='awaiting_code'){document.getElementById('codeBox').style.display='block';}
  if(j.status==='done'){clearInterval(timer);document.getElementById('codeBox').style.display='none';finish();}
  if(j.status==='failed'){clearInterval(timer);printLn('FAILED: '+(j.error||'unknown'),'err');document.getElementById('startBtn').disabled=false;}
}
async function sendCode(){
  const c=document.getElementById('code').value.trim(); if(!c)return;
  printLn('$ code ****','dim');
  await fetch('/login/code',{method:'POST',headers:hdr(),body:JSON.stringify({job_id:jobId,code:c})});
  document.getElementById('code').value='';
}
async function finish(){
  const r=await fetch('/login/session/'+jobId,{headers:hdr()});
  const j=await r.json(); sessionText=j.session||'';
  document.getElementById('sessionOut').textContent=sessionText;
  document.getElementById('who').textContent=j.account_username?('@'+j.account_username):'';
  document.getElementById('resultBox').style.display='block';
  printLn('DONE — session received ('+sessionText.length+' bytes)','ok');
  document.getElementById('startBtn').disabled=false;
}
function copySession(){navigator.clipboard.writeText(sessionText).then(()=>alert('copied — paste as INSTAGRAM_SESSION in Render'));}
function downloadSession(){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([sessionText],{type:'application/json'}));a.download='session.json';a.click();}
</script>
</body>
</html>
"""
