###############################################
# PC Fan Orchestrator – FastAPI backend + UI  #
###############################################
"""
Версия 1.0 – синхронизирована с прошивкой ESP v1.3

Поддерживает **все новые энд‑пойнты** контроллера:
    • /set            – задать PWM одному вентилятору
    • /pwm (bulk)     – задать все восемь за один запрос
    • /status         – массив PWM (ESP)
    • /info           – версия, аптайм, boostSec
    • /boost?sec=N    – изменить длительность turbo‑старта
    • /reboot         – перезапуск ESP32

Сервера предоставляет REST‑обёртку и адаптивный Web‑UI на /ui.
Запуск:  uvicorn main:app --reload
"""
from __future__ import annotations

import json, asyncio
from pathlib import Path
from typing import Dict, List
import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# ───────────────────────── constants ──────────────────────────
CONFIG_FILE = Path("fan_config.json")
ESP_IP       = "192.168.4.1"           # поправить при необходимости
TIMEOUT      = 1.0                      # сек
FAN_COUNT    = 8

# ───────────────────── data‑models / helpers ──────────────────
class FanState(BaseModel):
    pwm : int = Field(255, ge=0, le=255)

class SysInfo(BaseModel):
    fw: str = "-"
    ip: str = "-"
    upt: int = 0
    boost: int = 30

# локальный кеш
state: List[FanState] = [FanState() for _ in range(FAN_COUNT)]
info  = SysInfo()

async def esp_get(path:str):
    url=f"http://{ESP_IP}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        r = await cli.get(url)
        r.raise_for_status();
        if r.headers.get("content-type","" ).startswith("application/json"): return r.json()
        return r.text

async def esp_post_json(path:str,data):
    url=f"http://{ESP_IP}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        r=await cli.post(url,json=data)
        r.raise_for_status(); return r

# ───────────────────────── FastAPI ────────────────────────────
app = FastAPI(title="PC Fan Orchestrator",version="1.0.0")

# ───────────────────────── REST proxy ─────────────────────────
class SetReq(BaseModel):
    fan:int = Field(...,ge=0,lt=FAN_COUNT)
    pwm:int = Field(...,ge=0,le=255)

class BoostReq(BaseModel):
    seconds:int = Field(...,ge=0,le=300)

@app.post("/set",status_code=204)
async def set_pwm(req:SetReq):
    state[req.fan].pwm=req.pwm
    await esp_get(f"/set?fan={req.fan}&pwm={req.pwm}")

@app.post("/bulk",status_code=204)
async def bulk(pwms:List[int]):
    if len(pwms)!=FAN_COUNT or any(not 0<=v<=255 for v in pwms):
        raise HTTPException(400,"Array of 8 PWM values 0‑255 expected")
    await esp_post_json("/pwm",pwms)
    for i,v in enumerate(pwms): state[i].pwm=v

@app.get("/status",response_model=List[FanState]) #
async def get_status():
    try:
        raw=await esp_get("/status")
        if isinstance(raw,list) and len(raw)==FAN_COUNT:
            for i,v in enumerate(raw): state[i].pwm=int(v)
    except Exception: pass
    return state

@app.get("/info",response_model=SysInfo)
async def get_info():
    try:
        raw=await esp_get("/info")
        info.fw  = raw.get("fw","-")
        info.ip  = raw.get("ip","-")
        info.upt = raw.get("upt",0)
        info.boost = raw.get("boost",30)
    except Exception: pass
    return info

@app.post("/boost",status_code=204)
async def set_boost(req:BoostReq):
    await esp_get(f"/boost?sec={req.seconds}")

@app.post("/reboot",status_code=204)
async def reboot():
    await esp_get("/reboot")

# ─────────────────────────── UI  ─────────────────────────────
HTML=r"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>FanCtl</title>
<style>:root{--b:#10151b;--p:#1e212a;--slot:#10151b;--bd:#3b4048;--ac:#79b8ff;--t:#f1f1f1;font-family:system-ui,Arial}
body{margin:0;height:100%;background:var(--b);color:var(--t);display:flex;flex-direction:column}
header{padding:16px;background:var(--p);text-align:center;font-size:18px;font-weight:600}
main{flex:1;display:flex;flex-wrap:wrap;gap:24px;padding:24px;justify-content:center;overflow-y:auto}
.case,.panel{background:var(--p);border-radius:12px;padding:20px}
.case{width:260px;display:grid;gap:8px}
.slot{height:56px;border:1px solid var(--bd);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--ac);font-size:13px}
.panel{min-width:260px;max-width:640px;display:flex;flex-direction:column;gap:12px}
.info{font-size:14px;display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.fan{display:flex;align-items:center;gap:12px}
.fan label{width:60px;text-align:right}
input[type=range]{flex:1;height:6px;background:var(--bd);border-radius:3px;appearance:none}
input::-webkit-slider-thumb{appearance:none;width:16px;height:16px;border-radius:50%;background:var(--ac);cursor:pointer}
.val{width:48px;text-align:right}
.cfg{margin-top:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{padding:6px 12px;background:var(--ac);border:0;border-radius:6px;color:var(--t);cursor:pointer}</style></head>
<body><header id=hdr>FanCtl</header><main><section class=case id=case></section><section class=panel id=panel></section></main>
<script>
const N=8,deb=60;let q={},timer;const $=id=>document.getElementById(id);let hdr,case_,panel;
function build(){panel.insertAdjacentHTML('afterbegin','<div class=info><span id=fw>FW -</span><span id=ip>-</span><span id=upt>0s</span></div>');
for(let i=0;i<N;i++){case_.insertAdjacentHTML('beforeend',`<div class=slot id=s${i}>Fan ${i}</div>`);
panel.insertAdjacentHTML('beforeend',`<div class=fan><label>Fan ${i}</label><input id=r${i} type=range min=0 max=255 oninput=chg(${i},this.value)><span class=val id=v${i}>---</span></div>`);}panel.insertAdjacentHTML('beforeend',`<div class=cfg><label>Boost (s)</label><input id=boost type=number min=0 max=300 style="width:70px"><button onclick=save()>Save</button><button onclick=reb()>Reboot</button></div>`);}
function chg(i,v){$('v'+i).textContent=v;$('s'+i).style.background=`linear-gradient(90deg,#1d2533 ${v/2.55}%,var(--slot)0)`;q[i]=v;clearTimeout(timer);timer=setTimeout(send,deb);} 
async function send(){const arr=Object.entries(q);q={};await Promise.allSettled(arr.map(([k,v])=>fetch('/set',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({fan:+k,pwm:+v})})));}
async function refresh(){const st=await fetch('/status').then(r=>r.json()),inf=await fetch('/info').then(r=>r.json());
$('fw').textContent='FW '+inf.fw;$('ip').textContent=inf.ip;$('upt').textContent=inf.upt+'s';hdr.textContent='FanCtl '+inf.ip;
$('boost').value=inf.boost;st.forEach((f,i)=>{if(q[i])return;const r=$('r'+i);if(document.activeElement===r)return;r.value=f.pwm;$('v'+i).textContent=f.pwm;$('s'+i).style.background=f.pwm?`linear-gradient(90deg,#1d2533 ${f.pwm/2.55}%,var(--slot)0)`:'var(--slot)';});}
async function save(){await fetch('/boost',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({seconds:+$('boost').value})});alert('Saved');}
async function reb(){if(confirm('Reboot ESP32?'))await fetch('/reboot',{method:'POST'});} 
window.addEventListener('load',()=>{hdr=$('hdr');case_=$('case');panel=$('panel');build();refresh();setInterval(refresh,2000);});
</script></body></html>"""

@app.get("/ui",response_class=HTMLResponse)
async def ui():
    return HTML

# ───────────────────────── run from CLI ──────────────────────
if __name__=='__main__':
    import uvicorn; uvicorn.run("main:app",host="0.0.0.0",port=5000,reload=True)
