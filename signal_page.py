#!/usr/bin/env python3
"""1번 피보나치 회귀(TQQQ RSI 5층) + 2번(SPX 200일선 +4%/-3%, 보디가드) 신호 페이지 생성기.  v2 (2026-11-26 규칙)

표준 라이브러리만으로 계산합니다(잠금화면 PNG만 Playwright 사용, 없으면 건너뜀).
야후 파이낸스 일봉으로 docs/index.html, docs/status.json, docs/lock.html, docs/lock.png 를 씁니다.
오프라인 검증: python signal_page.py --csv-spx spx.csv --csv-tqqq tqqq.csv --csv-ndx ndx.csv
  (TradingView 내보내기 CSV: time, close 열)

이 페이지는 '규칙대로 매매했을 때의 상태'입니다. 실제 계좌와 다를 수 있으니 주문 전 TradingView와 대조하세요.
"""
import argparse, csv, datetime as dt, html, json, math, os, sys, time, urllib.request

# ---------- 전략 설정 (운용수칙과 같은 값. 바꾸지 않는 것이 원칙) ----------
# 1번 피보나치 회귀
RSI_LENS = [2, 3, 5, 8, 13]
RSI_TARGETS = [28, 28, 28, 28, 32]          # TQQQ 기준
LAYER_PCT = 20.0                            # 층당 비중(1번 자금 대비 %)
DD_TH, DD_MULT = 20.0, 0.25                 # 1번 자금 EOD DD 20% 이상이면 새 층 ×0.25
OVX_ON, OVX_OFF, OVX_MULT = 30.0, 15.0, 0.5 # 나스닥100 200일선 +30% 이상 → 새 층 ×0.5, +15% 아래에서 해제
# 2번
SMA_LEN = 200
ENTRY_PCT, EXIT_PCT = 4.0, 3.0              # SPX 200일선 +4% 위 마감 ON / -3% 아래 마감 OFF
BG_HALF, BG_ZERO, BG_BACK = 30.0, 40.0, 10.0  # 보디가드: 나스닥 이격 +30% → 50%, +40% → 0%, +10% 아래 → 100%
ON_TICKER = "133690 TIGER 미국나스닥100"
OFF_SPLIT = [("449170 TIGER KOFR금리액티브", 0.70), ("0064K0 KODEX 금액티브", 0.30)]
# 1번 체제 규칙 (2026-11-27 시범 적용, 2027-05 확인) — claude/에너지규칙_사전등록_2026-11-27.md
E_LOW = 0.0006                              # 되돌림 에너지 E = (1 - VR5_3년) × 변동성_1년² 의 '낮음' 경계 (고정)
RP_RATE = 0.029 * (1 - 0.154)               # 메리츠 외화RP 세후 (킬스위치 비교용)
COMM = 0.0007
SYM_SPX, SYM_TQQQ, SYM_NDX = "^GSPC", "TQQQ", "^NDX"
KST = dt.timezone(dt.timedelta(hours=9))


def fetch_yahoo(symbol, rng):
    """(날짜 목록, 종가 목록). 장중이면 끝나지 않은 오늘 봉은 버립니다."""
    last_err = None
    for host in ("query1", "query2"):
        for attempt in range(3):
            try:
                url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                       f"{urllib.request.quote(symbol)}?range={rng}&interval=1d")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.load(r)["chart"]["result"][0]
                ts = res["timestamp"]; cl = res["indicators"]["quote"][0]["close"]
                gmt = res["meta"].get("gmtoffset", -18000)
                rows = [(t, c) for t, c in zip(ts, cl) if c is not None]
                reg = res["meta"].get("currentTradingPeriod", {}).get("regular", {})
                if reg and rows and time.time() < reg.get("end", 0) and rows[-1][0] >= reg.get("start", 0):
                    rows = rows[:-1]
                dates = [dt.datetime.fromtimestamp(t + gmt, dt.timezone.utc).date().isoformat() for t, _ in rows]
                return dates, [float(c) for _, c in rows]
            except Exception as e:
                last_err = e; time.sleep(2 + 2 * attempt)
    raise RuntimeError(f"{symbol} 가격을 받지 못했습니다: {last_err}")


def read_csv(path):
    dates, closes = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("close"): continue
            dates.append(dt.datetime.fromtimestamp(int(float(row["time"])), dt.timezone.utc).date().isoformat())
            closes.append(float(row["close"]))
    return dates, closes


def sma_series(closes, n=SMA_LEN):
    out = []; s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n: s -= closes[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ndx_state(dates, closes):
    """나스닥100 200일선 이격 → 1번 과열 배수(래칫), 2번 보디가드 비중(래칫)."""
    sm = sma_series(closes); ovx = False; bg = 1.0; hist = []
    for c, s in zip(closes, sm):
        if s is None: hist.append((None, ovx, bg)); continue
        g = (c / s - 1) * 100
        if g >= OVX_ON: ovx = True
        elif ovx and g < OVX_OFF: ovx = False
        if g >= BG_ZERO: bg = 0.0
        elif g >= BG_HALF: bg = min(bg, 0.5)
        elif g < BG_BACK: bg = 1.0
        hist.append((g, ovx, bg))
    g, ovx, bg = hist[-1]
    return dict(date=dates[-1], close=closes[-1], sma=sm[-1], gap=g, ovx=ovx, ovx_mult=OVX_MULT if ovx else 1.0,
                bg=bg, bg_prev=hist[-2][2] if len(hist) > 1 else bg, hist={d: h[2] for d, h in zip(dates, hist)},
                ovx_hist=[h[1] for h in hist])


def trend_state(dates, closes, ndx):
    """2번: SPX 200일선 +4%/-3% 상태 기계 + 보디가드로 133690 목표 비중."""
    sm = sma_series(closes); on = False; last_switch = None; hist = []
    for i, (c, s) in enumerate(zip(closes, sm)):
        if s is not None:
            prev = on
            if not on and c > s * (1 + ENTRY_PCT / 100): on = True
            elif on and c < s * (1 - EXIT_PCT / 100): on = False
            if on != prev: last_switch = dates[i]
        hist.append(on)
    bgh = ndx["hist"]
    def weight(i):
        b = bgh.get(dates[i]); b = 1.0 if b is None else b
        return (1.0 if hist[i] else 0.0) * b
    w, wp = weight(-1), weight(-2)
    c, s = closes[-1], sm[-1]
    k = max(len(closes) - 252, SMA_LEN - 1)
    held = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(last_switch)).days if last_switch else None
    return dict(on=on, w133=w, w133_prev=wp, changed=abs(w - wp) > 1e-9, last_switch=last_switch, held_days=held,
                date=dates[-1], close=c, sma=s, pct=(c / s - 1) * 100,
                entry_line=s * (1 + ENTRY_PCT / 100), exit_line=s * (1 - EXIT_PCT / 100),
                chart=dict(dates=dates[k:], close=closes[k:], sma=sm[k:], on=hist[k:]))


def alloc_text(w):
    """2번 자금 안에서의 목표 배분 문구."""
    parts = []
    if w > 0: parts.append((ON_TICKER, w))
    for nm, f in OFF_SPLIT:
        if (1 - w) * f > 0: parts.append((nm, (1 - w) * f))
    return parts


def rsi_layers(dates, closes):
    """1번: Wilder RSI 5층. 층별 RSI, 규칙상 보유 여부, 다음 장 LOC 기준 가격."""
    out = []; c = closes; n = len(c)
    for L, T in zip(RSI_LENS, RSI_TARGETS):
        ag = max(c[1] - c[0], 0.0); al = max(c[0] - c[1], 0.0)
        rsi_prev = 100 * ag / (ag + al + 1e-300); held = False; since = None
        for i in range(2, n):
            ch = c[i] - c[i - 1]
            ag = (ag * (L - 1) + max(ch, 0.0)) / L
            al = (al * (L - 1) + max(-ch, 0.0)) / L
            rsi = 100 * ag / (ag + al + 1e-300)
            if held and rsi_prev < T and rsi >= T: held = False; since = dates[i]
            elif (not held) and rsi_prev > T and rsi <= T: held = True; since = dates[i]
            rsi_prev = rsi
        RS = T / (100 - T)
        if T > rsi_prev: d = RS * al * (L - 1) - ag * (L - 1)
        else:            d = al * (L - 1) - ((100 - T) / T) * ag * (L - 1)
        raw = c[-1] + d
        if rsi_prev > T: price = math.floor(raw * 100 + 1e-9) / 100   # 매수 기준가 내림
        else:            price = math.ceil(raw * 100 - 1e-9) / 100    # 매도 기준가 올림
        if held and rsi_prev < T:   act = "sell"
        elif (not held) and rsi_prev > T: act = "buy"
        else: act = "wait"
        out.append(dict(length=L, target=T, rsi=rsi_prev, held=held, since=since, action=act,
                        price=price, dist_pct=(price / c[-1] - 1) * 100))
    return dict(date=dates[-1], close=c[-1], layers=out, chart=dict(dates=dates[-63:], close=c[-63:]))


def _vr5(x):
    m = sum(x) / len(x); v1 = sum((a - m) ** 2 for a in x) / (len(x) - 1)
    x5 = [sum(x[i:i + 5]) for i in range(len(x) - 4)]; m5 = sum(x5) / len(x5)
    v5 = sum((a - m5) ** 2 for a in x5) / (len(x5) - 1)
    return v5 / (5 * v1)


def _month_start_idx(dates):
    """마지막 데이터가 속한 달의 첫 거래일 위치 (지표는 그 전날까지 데이터로 계산 = 지난달 말)."""
    i = len(dates) - 1
    while i > 0 and dates[i - 1][:7] == dates[-1][:7]: i -= 1
    return i


def regime_energy(dates, closes):
    """나스닥100: VR5 3년·1년, 변동성 1년, 되돌림 에너지. 지난달 말까지 데이터 (백테스트와 같은 시점)."""
    i = _month_start_idx(dates)
    r = [closes[k] / closes[k - 1] - 1 for k in range(1, i)]   # 지난달 말까지 일간 수익
    if len(r) < 756: return None
    x3, x1 = r[-756:], r[-252:]
    m1 = sum(x1) / len(x1); vol = math.sqrt(sum((a - m1) ** 2 for a in x1) / (len(x1) - 1) * 252)
    v3, v1 = _vr5(x3), _vr5(x1)
    return dict(asof=dates[i - 1], vr5_3y=v3, vr5_1y=v1, vol1y=vol, E=(1 - v3) * vol ** 2)


def shadow_s1(tdates, tcloses, ndx_hist):
    """1번 '그림자' (규칙대로 계속 돌린 가상 계좌, 체제 규칙 없음). 킬스위치 판정용 자금 곡선."""
    c = tcloses; n = len(c); K = len(RSI_LENS)
    rsi = [[None] * n for _ in range(K)]; prj = [[None] * n for _ in range(K)]
    for k, (L, T) in enumerate(zip(RSI_LENS, RSI_TARGETS)):
        ag = al = None
        for t in range(1, n):
            ch = c[t] - c[t - 1]; g = max(ch, 0.0); l = max(-ch, 0.0)
            if ag is None: ag, al = g, l
            else: ag = (ag * (L - 1) + g) / L; al = (al * (L - 1) + l) / L
            rv = 50.0 if ag + al == 0 else 100 * ag / (ag + al)
            if T > rv: d = T / (100 - T) * al * (L - 1) - ag * (L - 1)
            else:      d = al * (L - 1) - ((100 - T) / T) * ag * (L - 1)
            rsi[k][t] = rv; prj[k][t] = c[t] + d
    cash = 10000.0; sh = [0.0] * K; sig = [0.0] * K; peak = cash; eq = [None] * n; rd = RP_RATE / 252
    for t in range(1, n):
        cash += cash * rd if cash > 0 else 0.0
        if t > 1:
            for k in range(K):
                r1, p1, T = rsi[k][t - 1], prj[k][t - 1], RSI_TARGETS[k]
                if r1 > T and c[t] <= p1 and sig[k] > 0:
                    q = min(sig[k], max(cash, 0) / (c[t] * (1 + COMM)))
                    if q > 0: v = q * c[t]; cash -= v * (1 + COMM); sh[k] += q
                if r1 < T and c[t] >= p1 and sh[k] > 0:
                    v = sh[k] * c[t]; cash += v * (1 - COMM); sh[k] = 0.0
        e = cash + sum(sh) * c[t]; eq[t] = e; peak = max(peak, e)
        m = DD_MULT if (peak - e) / peak * 100 >= DD_TH else 1.0
        h = ndx_hist.get(tdates[t]); m *= OVX_MULT if (h and h[1]) else 1.0
        for k in range(K):
            sig[k] = e * LAYER_PCT / 100 * m / prj[k][t] if (rsi[k][t] > RSI_TARGETS[k] and prj[k][t] > 0) else 0.0
    return eq


def kill_switch(tdates, eq):
    """K3: 지난달 말 기준 1번 그림자의 3년(756일) 수익 < RP 3년 수익이면 켜짐."""
    i = _month_start_idx(tdates) - 1
    if i - 756 < 1 or eq[i - 756] is None: return None
    s = eq[i] / eq[i - 756]; rp = (1 + RP_RATE / 252) ** 756
    return dict(asof=tdates[i], s1_3y=s - 1, rp_3y=rp - 1, on=s < rp)


def regime_rules(en, ks):
    """R5: VR5 1년 ≥ 1 그리고 E < E_LOW → 새 층 중지. R10: 킬스위치 켜짐 그리고 E < E_LOW → 새 층 중지."""
    if en is None: return dict(stop=False, why=[], ok=False)
    low = en["E"] < E_LOW; why = []
    if low and en["vr5_1y"] >= 1.0: why.append("R5")
    if low and ks is not None and ks["on"]: why.append("R10")
    return dict(stop=bool(why), why=why, ok=True, low=low)


def build(spx, tq, nd):
    n = ndx_state(*nd); t = trend_state(*spx, n); r = rsi_layers(*tq)
    en = regime_energy(*nd)
    eq = shadow_s1(tq[0], tq[1], {d: (None, o, None) for d, o in zip(nd[0], n["ovx_hist"])})
    ks = kill_switch(tq[0], eq); rg = regime_rules(en, ks)
    n = {k: v for k, v in n.items() if k not in ("hist", "ovx_hist")}
    return dict(version="v3 (2026-11-27 체제 규칙 시범)", updated_kst=dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
                trend=t, rsi=r, ndx=n, alloc=alloc_text(t["w133"]), alloc_prev=alloc_text(t["w133_prev"]),
                energy=en, kill=ks, regime=rg,
                rules=dict(layer_pct=LAYER_PCT, dd_th=DD_TH, dd_mult=DD_MULT, ovx_mult=n["ovx_mult"],
                           regime_mult=0.0 if rg["stop"] else 1.0, kill_on=bool(ks and ks["on"])))


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>전략 신호</title>
<style>
:root{--bg:#eef0f3;--card:#fff;--fg:#14171c;--mut:#5f6b7a;--line:#e1e5ea;--on:#0a7d4f;--off:#b3401d;--warn:#8a5a00;--chip:#eef1f5;--sma:#7a4fd6;--track:#dfe3e8;--band:#7a4fd61a}
@media(prefers-color-scheme:dark){:root{--bg:#0b0d10;--card:#16191e;--fg:#f1f4f8;--mut:#9aa4b2;--line:#272c34;--on:#3ecf8e;--off:#ff8a65;--warn:#e3b341;--chip:#222831;--sma:#b392f0;--track:#2a3038;--band:#b392f022}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:520px;margin:0 auto;padding:12px 16px 28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin-top:12px}
h2{font-size:12px;font-weight:600;color:var(--mut);margin:0 0 6px;letter-spacing:.04em}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:12px}
.huge{font-size:44px;font-weight:700;line-height:1.05;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.big{font-size:24px;font-weight:700;line-height:1.2}.on{color:var(--on)}.off{color:var(--off)}.warn{color:var(--warn)}
.sub{color:var(--mut);font-size:12.5px}.kv{text-align:right}.kv b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.kv span{font-size:11px;color:var(--mut);letter-spacing:.04em}
.stats{display:flex;gap:18px;justify-content:flex-end}
svg{display:block;width:100%;height:auto;margin-top:10px}svg text{font:10px system-ui,sans-serif;fill:var(--mut)}
.row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.num{font-variant-numeric:tabular-nums;text-align:right}
.layer{padding:10px 0;border-top:1px solid var(--line)}.layer:first-of-type{border-top:0}
.lh{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-variant-numeric:tabular-nums}
.lh .nm{font-weight:600}.lh .act{font-size:14px}.lh2{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums;margin-top:2px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:0 7px;font-size:11px;font-weight:500;margin-left:4px}
.alert{border-color:var(--warn)}input{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
label{font-size:12px;color:var(--mut);display:block;margin:8px 0 4px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
footer{color:var(--mut);font-size:12px;margin-top:14px}
</style></head><body><main>
<div class="sub">갱신 __UPDATED__ (한국시간) · 미국 종가 기준 · __VER__</div>
__BODY__
<div class="card"><h2>1번 주문 수량 계산 (이 기기에만 저장)</h2>
<div class="grid"><div><label>1번 자금 지금 (달러) = 총자산 × 30%</label><input id="cap" inputmode="decimal" placeholder="예: 4500"></div>
<div><label>1번 자금 고점 (달러)</label><input id="peak" inputmode="decimal" placeholder="예: 4700"></div></div>
<label><input type="checkbox" id="ks" style="width:auto"> 킬스위치 켜짐 (위 1번 칸 '킬스위치 켜짐'이고 운용수칙상 따르기로 했을 때만 체크)</label>
<div class="sub" id="qtynote" style="margin-top:8px">자금을 넣으면 층마다 수량이 표시됩니다.</div></div>
<footer>가격은 야후 파이낸스 일봉입니다. 이 페이지는 규칙대로 매매했을 때의 상태이며 실제 계좌와 다를 수 있습니다. 주문 전에 TradingView(피보나치 회귀 v5.0, 2번 SPX 차트)와 반드시 대조하세요. 킬스위치·체제 규칙은 매달 말 기준으로 이 페이지가 계산합니다(킬스위치는 RP 2.9% 기준 근사).</footer>
</main><script>
const L=__LAYERS__, R=__RULES__;
function g(k){try{return localStorage.getItem(k)}catch(e){return null}}
function s(k,v){try{localStorage.setItem(k,v)}catch(e){}}
const cap=document.getElementById('cap'),pk=document.getElementById('peak'),ks=document.getElementById('ks');
cap.value=g('cap')||'';pk.value=g('peak')||'';ks.checked=g('ks')==='1';
function upd(){s('cap',cap.value);s('peak',pk.value);s('ks',ks.checked?'1':'0');
 const c=parseFloat(cap.value),p=Math.max(parseFloat(pk.value)||0,c||0);const ok=c>0;
 const dd=ok&&p>0?(1-c/p)*100:0, dm=dd>=R.dd_th?R.dd_mult:1, m=dm*R.ovx_mult*R.regime_mult, amt=ok?c*R.layer_pct/100*m:0;
 L.forEach((x,i)=>{const el=document.getElementById('q'+i);if(!el)return;
  if(x.action==='sell'){el.textContent='· 수량 전량';return}
  if(x.action==='buy'&&R.regime_mult===0){el.textContent='· 체제 규칙: 매수 안 함';return}
  if(!ok||x.action!=='buy'||ks.checked){el.textContent=(x.action==='buy'&&ks.checked)?'· 킬스위치: 매수 안 함':'';return}
  el.textContent='· 수량 '+Math.floor(Math.round(amt)/x.price)+'주'});
 document.getElementById('qtynote').textContent=ok?('1번 EOD DD '+dd.toFixed(1)+'% → 낙폭 ×'+dm+' · 과열 ×'+R.ovx_mult+' · 체제 ×'+R.regime_mult+' → 층당 약 $'+Math.round(amt).toLocaleString()):'자금을 넣으면 층마다 수량이 표시됩니다.'}
[cap,pk,ks].forEach(e=>e.addEventListener('input',upd));upd();
</script></body></html>"""


def _path(xs, ys):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def trend_chart(t):
    ch = t["chart"]; cl = ch["close"]; sm = ch["sma"]; n = len(cl)
    W, H, R, TOP, BOT = 340, 150, 40, 6, 16
    en = [v * (1 + ENTRY_PCT / 100) for v in sm]; ex = [v * (1 - EXIT_PCT / 100) for v in sm]
    lo = min(min(cl), min(ex)); hi = max(max(cl), max(en)); pad = (hi - lo) * 0.04; lo -= pad; hi += pad
    X = lambda i: (W - R) * i / (n - 1)
    Y = lambda v: TOP + (H - TOP - BOT) * (1 - (v - lo) / (hi - lo))
    xs = [X(i) for i in range(n)]
    band = _path(xs, [Y(v) for v in en]) + " L" + " L".join(f"{x:.1f},{Y(v):.1f}" for x, v in reversed(list(zip(xs, ex)))) + " Z"
    segs = []; a = 0
    for i in range(1, n + 1):
        if i == n or ch["on"][i] != ch["on"][a]:
            col = "var(--on)" if ch["on"][a] else "var(--off)"
            segs.append(f'<rect x="{X(a):.1f}" y="{H-7}" width="{max(X(i-1)-X(a),1.5):.1f}" height="3" rx="1.5" fill="{col}"/>')
            a = i
    lx = W - R + 5
    lab = (f'<text x="{lx}" y="{Y(en[-1])+3:.1f}" style="fill:var(--on)">진입선</text>'
           f'<text x="{lx}" y="{Y(sm[-1])+3:.1f}" style="fill:var(--sma)">200일</text>'
           f'<text x="{lx}" y="{Y(ex[-1])+3:.1f}" style="fill:var(--off)">청산선</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="S&amp;P500 1년 종가와 200일선">'
            f'<path d="{band}" fill="var(--band)"/>'
            f'<path d="{_path(xs,[Y(v) for v in en])}" fill="none" stroke="var(--on)" stroke-width="1"/>'
            f'<path d="{_path(xs,[Y(v) for v in ex])}" fill="none" stroke="var(--off)" stroke-width="1"/>'
            f'<path d="{_path(xs,[Y(v) for v in sm])}" fill="none" stroke="var(--sma)" stroke-width="1.6"/>'
            f'<path d="{_path(xs,[Y(v) for v in cl])}" fill="none" stroke="var(--fg)" stroke-width="1.7" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{Y(cl[-1]):.1f}" r="3.2" fill="var(--fg)" stroke="var(--card)" stroke-width="1.5"/>'
            + "".join(segs) + lab +
            f'<text x="0" y="{H-11}">{ch["dates"][0][:7]}</text><text x="{W-R}" y="{H-11}" text-anchor="end">{ch["dates"][-1][5:]}</text></svg>')


def trend_gauge(t):
    W, H = 340, 44; lo, hi = -EXIT_PCT - 4, ENTRY_PCT + 5
    X = lambda p: 8 + (W - 16) * (min(max(p, lo), hi) - lo) / (hi - lo)
    y = 12
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="200일선 대비 현재 위치">'
            f'<rect x="8" y="{y-3}" width="{W-16}" height="6" rx="3" fill="var(--track)"/>'
            f'<rect x="8" y="{y-3}" width="{X(-EXIT_PCT)-8:.1f}" height="6" rx="3" fill="var(--off)"/>'
            f'<rect x="{X(ENTRY_PCT):.1f}" y="{y-3}" width="{W-8-X(ENTRY_PCT):.1f}" height="6" rx="3" fill="var(--on)"/>'
            f'<rect x="{X(0)-1:.1f}" y="{y-7}" width="2" height="14" fill="var(--sma)"/>'
            f'<circle cx="{X(t["pct"]):.1f}" cy="{y}" r="6.5" fill="var(--fg)" stroke="var(--card)" stroke-width="2.5"/>'
            f'<text x="{X(-EXIT_PCT):.1f}" y="30" text-anchor="middle" style="fill:var(--off)">청산 -{EXIT_PCT:g}%</text>'
            f'<text x="{X(-EXIT_PCT):.1f}" y="41" text-anchor="middle">{t["exit_line"]:,.0f}</text>'
            f'<text x="{X(0):.1f}" y="30" text-anchor="middle" style="fill:var(--sma)">200일선</text>'
            f'<text x="{X(0):.1f}" y="41" text-anchor="middle">{t["sma"]:,.0f}</text>'
            f'<text x="{X(ENTRY_PCT):.1f}" y="30" text-anchor="middle" style="fill:var(--on)">진입 +{ENTRY_PCT:g}%</text>'
            f'<text x="{X(ENTRY_PCT):.1f}" y="41" text-anchor="middle">{t["entry_line"]:,.0f}</text></svg>')


def rsi_gauge(x):
    W, H = 340, 16; X = lambda v: 4 + (W - 8) * v / 100
    return (f'<svg viewBox="0 0 {W} {H}" style="margin-top:6px" role="img" aria-label="RSI {x["rsi"]:.0f}, 목표 {x["target"]}">'
            f'<rect x="4" y="5" width="{W-8}" height="6" rx="3" fill="var(--track)"/>'
            f'<rect x="4" y="5" width="{X(x["target"])-4:.1f}" height="6" rx="3" fill="var(--on)" opacity=".55"/>'
            f'<rect x="{X(x["target"])-1:.1f}" y="1" width="2" height="14" fill="var(--on)"/>'
            f'<circle cx="{X(x["rsi"]):.1f}" cy="8" r="5.5" fill="var(--fg)" stroke="var(--card)" stroke-width="2"/></svg>')


def tqqq_chart(r):
    ch = r["chart"]; cl = ch["close"]; n = len(cl)
    buys = [x["price"] for x in r["layers"] if x["action"] == "buy"]
    W, H, R, TOP, BOT = 340, 96, 58, 6, 14
    lo = min(cl + buys); hi = max(cl + buys); pad = (hi - lo) * 0.08 or 1; lo -= pad; hi += pad
    X = lambda i: (W - R) * i / (n - 1)
    Y = lambda v: TOP + (H - TOP - BOT) * (1 - (v - lo) / (hi - lo))
    xs = [X(i) for i in range(n)]; zone = ""
    if buys:
        y1, y2 = Y(max(buys)), Y(min(buys))
        zone = (f'<rect x="0" y="{y1:.1f}" width="{W-R}" height="{max(y2-y1,2):.1f}" fill="var(--on)" opacity=".22"/>'
                f'<text x="{W-R+5}" y="{(y1+y2)/2-2:.1f}" style="fill:var(--on)">매수 기준가</text>'
                f'<text x="{W-R+5}" y="{(y1+y2)/2+9:.1f}">{min(buys):,.1f}~{max(buys):,.1f}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="TQQQ 3개월 종가">{zone}'
            f'<path d="{_path(xs,[Y(v) for v in cl])}" fill="none" stroke="var(--fg)" stroke-width="1.7" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{Y(cl[-1]):.1f}" r="3.2" fill="var(--fg)" stroke="var(--card)" stroke-width="1.5"/>'
            f'<text x="{W-R+5}" y="{max(Y(cl[-1])-6,9):.1f}" style="fill:var(--fg)">{cl[-1]:,.2f}</text>'
            f'<text x="0" y="{H-2}">{ch["dates"][0][5:]}</text><text x="{W-R}" y="{H-2}" text-anchor="end">{ch["dates"][-1][5:]}</text></svg>')


def fmt_alloc(parts, sep=" · "):
    return sep.join(f"{html.escape(nm.split()[0])} {f*100:.0f}%" for nm, f in parts)


def change_text(st):
    """2번 목표 배분이 바뀌었을 때 할 일."""
    t = st["trend"]; w, wp = t["w133"], t["w133_prev"]
    if wp == 0 and w > 0: return f"449170·금 매도 → 133690 {w*100:.0f}% 매수 (2번 자금 기준)"
    if w > wp: return f"133690을 2번 자금의 {w*100:.0f}%까지 늘리기 (OFF 자산에서)"
    if w == 0: return "133690 전량 매도 → 449170 70 : 금 30"
    return f"133690을 2번 자금의 {w*100:.0f}%로 줄이고, 판 돈은 449170 70 : 금 30"


def regime_text(st):
    """체제 규칙 한 줄 (페이지·잠금화면 공용)."""
    en, ks, rg = st.get("energy"), st.get("kill"), st.get("regime", {})
    if not en: return "체제 계산 불가 (데이터 부족)", ""
    k = "-" if ks is None else ("켜짐" if ks["on"] else "정상")
    base = f'되돌림 에너지 {en["E"]*1000:.2f} (낮음 < {E_LOW*1000:.1f}) · VR5 1년 {en["vr5_1y"]:.2f} · 킬스위치 {k}'
    if rg.get("stop"): return base, f'새 층 매수 중지 ({"·".join(rg["why"])})'
    return base, ""


def render(st):
    t, r, nd = st["trend"], st["rsi"], st["ndx"]; e = html.escape
    cls = "on" if t["on"] else "off"
    if t["on"]: gap = (t["exit_line"] / t["close"] - 1) * 100; gl = "청산선까지"
    else:       gap = (t["entry_line"] / t["close"] - 1) * 100; gl = "진입선까지"
    hd = f'{t["held_days"]}일' if t["held_days"] is not None else "-"
    bgtxt = {1.0: "보디가드 없음", 0.5: "보디가드 50%", 0.0: "보디가드 0%"}[t["w133"] if t["on"] else 1.0] if t["on"] else "신호 OFF"
    b = []
    if t["changed"]:
        b.append(f'<div class="card alert"><div class="big warn">2번 배분 변경</div>'
                 f'<div>다음 한국장 09:05 이후: {e(change_text(st))}</div></div>')
    b.append(f'<div class="card"><h2>2번 · S&amp;P500 200일선 +{ENTRY_PCT:g} / -{EXIT_PCT:g} · 보디가드</h2>'
             f'<div class="top"><div><div class="huge {cls}">{t["pct"]:+.1f}%</div><div class="sub">200일선 대비</div></div>'
             f'<div class="stats"><div class="kv"><span>{"보유" if t["on"] else "대기"} 기간</span><b>{hd}</b></div>'
             f'<div class="kv"><span>{gl}</span><b>{gap:+.1f}%</b></div></div></div>'
             f'<div class="big {cls}" style="margin-top:8px;font-size:18px">{"ON" if t["on"] else "OFF"} · {e(bgtxt)}</div>'
             f'<div style="margin-top:4px">목표 배분: <b>{fmt_alloc(st["alloc"])}</b></div>'
             + trend_chart(t) + trend_gauge(t) +
             f'<div style="margin-top:8px"><div class="row"><span>S&amp;P500 종가 ({e(t["date"])})</span><span class="num">{t["close"]:,.1f}</span></div>'
             f'<div class="row"><span>나스닥100 200일선 이격</span><span class="num">{nd["gap"]:+.1f}% (보디가드 +{BG_HALF:g}/+{BG_ZERO:g}, 복귀 +{BG_BACK:g})</span></div>'
             f'<div class="row"><span>마지막 전환</span><span class="num">{e(t["last_switch"] or "-")}</span></div></div></div>')
    nheld = sum(1 for x in r["layers"] if x["held"])
    rows = []
    for i, x in enumerate(r["layers"]):
        stop = st.get("regime", {}).get("stop")
        if x["action"] == "buy" and stop: act = f'<span class="warn">매수 중지</span> (기준 {x["price"]:,.2f})'
        elif x["action"] == "buy": act = f'매수 ≤ <b>{x["price"]:,.2f}</b>'
        elif x["action"] == "sell": act = f'매도 ≥ <b>{x["price"]:,.2f}</b>'
        else: act = "대기"
        state = '<span class="chip on">보유</span>' if x["held"] else '<span class="chip">비어 있음</span>'
        rows.append(f'<div class="layer"><div class="lh"><span class="nm">RSI {x["length"]}일{state}</span><span class="act">{act}</span></div>'
                    f'<div class="lh2"><span>RSI {x["rsi"]:.1f} · 목표 {x["target"]}</span><span>종가 대비 {x["dist_pct"]:+.1f}% <span id="q{i}"></span></span></div>'
                    + rsi_gauge(x) + '</div>')
    ovx = (f'<span class="warn">과열 ×{OVX_MULT:g}</span> (나스닥 이격 {nd["gap"]:+.1f}%, +{OVX_OFF:g}% 아래에서 해제)' if nd["ovx"]
           else f'과열 아님 (나스닥 이격 {nd["gap"]:+.1f}%, +{OVX_ON:g}% 이상이면 ×{OVX_MULT:g})')
    b.append(f'<div class="card"><h2>1번 · 피보나치 회귀 TQQQ 5층 (다음 미국장 LOC)</h2>'
             f'<div class="top"><div><div class="huge">{nheld}<span style="font-size:22px;color:var(--mut)"> / 5 층 보유</span></div></div>'
             f'<div class="kv"><span>TQQQ 종가 {e(r["date"][5:])}</span><b>{r["close"]:,.2f}</b></div></div>'
             f'<div class="sub" style="margin-top:6px">층당 {LAYER_PCT:g}% · 낙폭 {DD_TH:g}% 이상 ×{DD_MULT:g} · {ovx}</div>'
             + (lambda bt: f'<div class="sub" style="margin-top:4px">체제(지난달 말): {e(bt[0])}'
                + (f' · <b class="warn">{e(bt[1])}</b>' if bt[1] else ' · <span class="on">정상</span>') + '</div>')(regime_text(st))
             + tqqq_chart(r) + '<div style="margin-top:6px">' + "".join(rows) + '</div></div>')
    layers_js = json.dumps([dict(action=x["action"], price=x["price"]) for x in r["layers"]])
    return (PAGE.replace("__UPDATED__", e(st["updated_kst"])).replace("__VER__", e(st["version"]))
            .replace("__BODY__", "\n".join(b)).replace("__LAYERS__", layers_js)
            .replace("__RULES__", json.dumps(st["rules"])))


# ---------- 잠금화면·AOD용 이미지 (검정 배경, 큰 글씨, 1년 차트) ----------
LOCK = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
:root{--fg:#f4f6f8;--mut:#8d96a3;--card:#141518;--line:#24272c;--on:#3fb950;--off:#ff6b57;--sma:#e94fd1;--hold:#b05ce0;--cash:#e3a33b;--warn:#ffcc4d;--track:#2b2e33}
*{box-sizing:border-box}html,body{margin:0;width:480px;height:1040px;background:#000;color:var(--fg);
font:15px/1.3 "Noto Sans CJK KR","Noto Sans KR",system-ui,sans-serif;overflow:hidden}
.wrap{position:absolute;left:18px;right:18px;top:__TOP__px}
.card{background:var(--card);border-radius:26px;padding:16px 18px 14px;margin-bottom:12px}
.hd{font-size:14px;font-weight:700;color:var(--mut);letter-spacing:.02em}
.on{color:var(--on)}.off{color:var(--off)}.mut{color:var(--mut)}.warn{color:var(--warn)}.hold{color:var(--hold)}.cash{color:var(--cash)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;align-items:end;margin-top:4px}
.big{font-size:58px;font-weight:800;line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.cap{font-size:15px;color:var(--mut);margin-top:4px}
.kv{font-size:12px;color:var(--mut);font-weight:700;letter-spacing:.03em}
.kv b{display:block;font-size:21px;white-space:nowrap;color:var(--fg);letter-spacing:0;font-variant-numeric:tabular-nums}
.kv b.hold{color:var(--hold)}.kv b.cash{color:var(--cash)}
svg{display:block;width:100%;height:auto}svg text{font:700 11px "Noto Sans CJK KR",sans-serif}
.alert{border:2px solid var(--warn);border-radius:18px;padding:9px 14px;margin-bottom:10px;font-size:15px}
.alert b{display:block;font-size:20px;color:var(--warn)}
.row{display:flex;justify-content:space-between;align-items:baseline;padding:3px 0;font-variant-numeric:tabular-nums}
.row .nm{font-size:15px;font-weight:700}.row .act{font-size:20px;font-weight:800}
.foot{font-size:12px;color:var(--mut);text-align:center}
</style></head><body><div class="wrap">__BODY__</div></body></html>"""

C_ON, C_OFF, C_SMA, C_HOLD, C_CASH, C_TRACK, C_MUT = "#3fb950", "#ff6b57", "#e94fd1", "#b05ce0", "#e3a33b", "#2b2e33", "#8d96a3"


def aod_chart(t, W=444, H=210):
    """1년 차트: 진입선(초록)·200일선(분홍)·청산선(빨강) 띠 + 종가(흰색) + 아래 보유 막대."""
    ch = t["chart"]; cl = ch["close"]; sm = ch["sma"]; n = len(cl)
    en = [v * (1 + ENTRY_PCT / 100) for v in sm]; ex = [v * (1 - EXIT_PCT / 100) for v in sm]
    lo = min(min(cl), min(ex)); hi = max(max(cl), max(en)); pad = (hi - lo) * 0.05; lo -= pad; hi += pad
    PH = H - 22
    X = lambda i: 4 + (W - 18) * i / (n - 1)
    Y = lambda v: 4 + (PH - 8) * (1 - (v - lo) / (hi - lo))
    xs = [X(i) for i in range(n)]
    band = _path(xs, [Y(v) for v in en]) + " L" + " L".join(f"{x:.1f},{Y(v):.1f}" for x, v in reversed(list(zip(xs, ex)))) + " Z"
    segs = []; a = 0
    for i in range(1, n + 1):
        if i == n or ch["on"][i] != ch["on"][a]:
            col = C_HOLD if ch["on"][a] else C_CASH
            segs.append(f'<rect x="{X(a)+1:.1f}" y="{H-8}" width="{max(X(i-1)-X(a)-2,3):.1f}" height="6" rx="3" fill="{col}"/>')
            a = i
    return (f'<svg viewBox="0 0 {W} {H}">'
            f'<rect x="0" y="0" width="{W}" height="{PH}" rx="6" fill="#1b1d21"/>'
            f'<path d="{band}" fill="#2a2c31"/>'
            f'<path d="{_path(xs,[Y(v) for v in en])}" fill="none" stroke="{C_ON}" stroke-width="1.6"/>'
            f'<path d="{_path(xs,[Y(v) for v in ex])}" fill="none" stroke="{C_OFF}" stroke-width="1.6"/>'
            f'<path d="{_path(xs,[Y(v) for v in sm])}" fill="none" stroke="{C_SMA}" stroke-width="2"/>'
            f'<path d="{_path(xs,[Y(v) for v in cl])}" fill="none" stroke="#fff" stroke-width="2.2" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{Y(cl[-1]):.1f}" r="7" fill="#fff" stroke="#555" stroke-width="3"/>'
            f'<text x="8" y="{Y(en[0])-5:.1f}" fill="{C_ON}">진입</text>'
            f'<text x="8" y="{min(Y(ex[0])+14, PH-4):.1f}" fill="{C_OFF}">청산</text>'
            + "".join(segs) + '</svg>')


def aod_gauge(t, W=444, H=62):
    """청산선 ~ 200일선 ~ 진입선 사이 현재 위치."""
    lo, hi = -EXIT_PCT - 5, ENTRY_PCT + 5
    X = lambda p: 10 + (W - 20) * (min(max(p, lo), hi) - lo) / (hi - lo)
    y = 14
    return (f'<svg viewBox="0 0 {W} {H}" style="margin-top:8px">'
            f'<rect x="10" y="{y-6}" width="{W-20}" height="12" rx="6" fill="{C_TRACK}"/>'
            f'<rect x="10" y="{y-6}" width="{X(-EXIT_PCT)-10:.1f}" height="12" rx="6" fill="{C_OFF}"/>'
            f'<rect x="{X(ENTRY_PCT):.1f}" y="{y-6}" width="{W-10-X(ENTRY_PCT):.1f}" height="12" rx="6" fill="{C_ON}"/>'
            f'<rect x="{X(0)-2:.1f}" y="{y-12}" width="4" height="24" rx="2" fill="{C_SMA}"/>'
            f'<circle cx="{X(t["pct"]):.1f}" cy="{y}" r="11" fill="#fff" stroke="#141518" stroke-width="3"/>'
            f'<text x="{X(-EXIT_PCT):.1f}" y="{y+30}" text-anchor="middle" fill="{C_OFF}" style="font-size:13px">청산</text>'
            f'<text x="{X(-EXIT_PCT):.1f}" y="{y+46}" text-anchor="middle" fill="{C_MUT}" style="font-size:12px">{t["exit_line"]:,.0f}</text>'
            f'<text x="{X(0):.1f}" y="{y+30}" text-anchor="middle" fill="{C_SMA}" style="font-size:13px">200일</text>'
            f'<text x="{X(0):.1f}" y="{y+46}" text-anchor="middle" fill="{C_MUT}" style="font-size:12px">{t["sma"]:,.0f}</text>'
            f'<text x="{X(ENTRY_PCT):.1f}" y="{y+30}" text-anchor="middle" fill="{C_ON}" style="font-size:13px">진입</text>'
            f'<text x="{X(ENTRY_PCT):.1f}" y="{y+46}" text-anchor="middle" fill="{C_MUT}" style="font-size:12px">{t["entry_line"]:,.0f}</text></svg>')


def aod_bg_bar(gap, W=200, H=30):
    """나스닥100 이격 막대: 보디가드 +30%(주황) / +40%(빨강)."""
    lo, hi = -20.0, 50.0
    X = lambda p: 2 + (W - 4) * (min(max(p, lo), hi) - lo) / (hi - lo)
    x0 = X(0); xg = X(gap)
    fill = (f'<rect x="{min(x0,xg):.1f}" y="4" width="{max(abs(xg-x0),3):.1f}" height="7" rx="3.5" fill="#fff"/>')
    return (f'<svg viewBox="0 0 {W} {H}" style="margin-top:4px">'
            f'<rect x="2" y="4" width="{W-4}" height="7" rx="3.5" fill="{C_TRACK}"/>' + fill +
            f'<rect x="{X(BG_HALF)-1.5:.1f}" y="0" width="3" height="15" fill="{C_CASH}"/>'
            f'<rect x="{X(BG_ZERO)-1.5:.1f}" y="0" width="3" height="15" fill="{C_OFF}"/>'
            f'<text x="{X(BG_HALF):.1f}" y="28" text-anchor="middle" fill="{C_MUT}">30%</text>'
            f'<text x="{X(BG_ZERO):.1f}" y="28" text-anchor="middle" fill="{C_MUT}">40%</text></svg>')


def render_lock(st, top=250):
    t, r, nd = st["trend"], st["rsi"], st["ndx"]; e = html.escape
    cls = "on" if t["on"] else "off"
    if t["on"]: gap = (t["exit_line"] / t["close"] - 1) * 100; gl = "청산선까지"
    else:       gap = (t["entry_line"] / t["close"] - 1) * 100; gl = "진입선까지"
    hd = f'{t["held_days"]}일' if t["held_days"] is not None else "-"
    hold_name = "133690" if t["w133"] > 0 else "KOFR·금"
    hold_cls = "hold" if t["w133"] > 0 else "cash"
    wtxt = f' {t["w133"]*100:.0f}%' if 0 < t["w133"] < 1 else ""
    b = []
    if t["changed"]:
        b.append(f'<div class="alert"><b>오늘 2번 매매</b>{e(change_text(st))}</div>')
    b.append(
        f'<div class="card"><div class="hd">2번 · SPX 200일선 +{ENTRY_PCT:g}/−{EXIT_PCT:g} · 1년</div>'
        f'<div class="grid"><div><div class="big {cls}">{t["pct"]:+.1f}%</div><div class="cap">200일선 대비 · {"ON" if t["on"] else "OFF"}</div></div>'
        f'<div><div class="grid" style="margin:0"><div class="kv">{"보유" if t["on"] else "대기"} {hd}<b class="{hold_cls}">{hold_name}{wtxt}</b></div>'
        f'<div class="kv" style="text-align:right">{gl}<b>{gap:+.1f}%</b></div></div>'
        f'<div class="kv" style="margin-top:8px;display:flex;justify-content:space-between"><span>나스닥 vs 200일</span><span style="color:var(--fg);font-size:15px">{nd["gap"]:+.1f}%</span></div>'
        + aod_bg_bar(nd["gap"]) + '</div></div>'
        + f'<div style="margin-top:10px">{aod_chart(t)}</div>' + aod_gauge(t) + '</div>')
    nheld = sum(1 for x in r["layers"] if x["held"]); rows = []
    for x in r["layers"]:
        if x["action"] == "buy": act = f'<span class="on">매수 ≤ {x["price"]:,.2f}</span>'
        elif x["action"] == "sell": act = f'<span class="off">매도 ≥ {x["price"]:,.2f}</span>'
        else: act = '<span class="mut">대기</span>'
        rows.append(f'<div class="row"><span class="nm">{x["length"]}일{" ●" if x["held"] else ""}</span><span class="act">{act}</span></div>')
    stop = st.get("regime", {}).get("stop")
    if stop:
        rows = [x.replace('<span class="on">매수 ≤', '<span class="warn">중지 · ≤') for x in rows]
    en = st.get("energy")
    rtxt = (f'<span class="warn">새 매수 중지 ({"·".join(st["regime"]["why"])})</span>' if stop else
            (f'체제 정상 · 에너지 {en["E"]*1000:.1f}' if en else '체제 -'))
    rows.append(f'<div class="row"><span class="nm">체제</span><span class="act">{rtxt}</span></div>')
    mult = f'<span class="warn">과열 ×{OVX_MULT:g}</span>' if nd["ovx"] else "과열 아님"
    b.append(f'<div class="card"><div class="hd" style="display:flex;justify-content:space-between"><span>1번 · TQQQ {nheld}/5층</span>'
             f'<span>종가 {r["close"]:,.2f} · {mult}</span></div><div style="margin-top:4px">' + "".join(rows) + '</div></div>')
    b.append(f'<div class="foot">미국 {e(t["date"])} 종가 · 갱신 {e(st["updated_kst"][5:])} · 주문 전 TV 대조</div>')
    return LOCK.replace("__TOP__", str(top)).replace("__BODY__", "\n".join(b))


def save_png(html_path, png_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright가 없어 PNG는 만들지 않았습니다."); return False
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport=dict(width=480, height=1040), device_scale_factor=3)
        pg.goto("file://" + os.path.abspath(html_path)); pg.screenshot(path=png_path); br.close()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-spx"); ap.add_argument("--csv-tqqq"); ap.add_argument("--csv-ndx"); ap.add_argument("--out", default="docs")
    ap.add_argument("--lock-top", type=int, default=250, help="잠금화면 이미지에서 내용이 시작되는 높이(0~1040)")
    a = ap.parse_args()
    spx = read_csv(a.csv_spx) if a.csv_spx else fetch_yahoo(SYM_SPX, "5y")
    tq = read_csv(a.csv_tqqq) if a.csv_tqqq else fetch_yahoo(SYM_TQQQ, "5y")
    nd = read_csv(a.csv_ndx) if a.csv_ndx else fetch_yahoo(SYM_NDX, "5y")
    if len(spx[1]) < SMA_LEN + 5 or len(nd[1]) < 800 or len(tq[1]) < 800:
        sys.exit("가격 데이터가 부족합니다.")
    st = build(spx, tq, nd)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f: f.write(render(st))
    with open(os.path.join(a.out, "status.json"), "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False, indent=1)
    lock_html = os.path.join(a.out, "lock.html")
    with open(lock_html, "w", encoding="utf-8") as f: f.write(render_lock(st, a.lock_top))
    save_png(lock_html, os.path.join(a.out, "lock.png"))
    t, nd_ = st["trend"], st["ndx"]
    print(f"2번 {'ON' if t['on'] else 'OFF'} | SPX {t['close']:.1f} / 200일선 {t['sma']:.1f} ({t['pct']:+.2f}%) | 마지막 전환 {t['last_switch']}")
    print(f"    목표 배분: {fmt_alloc(st['alloc'])} | 나스닥 이격 {nd_['gap']:+.1f}% | 보디가드 {nd_['bg']:.2f} | 변경 {'있음' if t['changed'] else '없음'}")
    print(f"1번 과열 {'×0.5' if nd_['ovx'] else '아님'}")
    en, ks, rg = st["energy"], st["kill"], st["regime"]
    if en: print(f"1번 체제 ({en['asof']} 기준) VR5 3년 {en['vr5_3y']:.3f} · 1년 {en['vr5_1y']:.3f} · 변동성 {en['vol1y']*100:.1f}% · 에너지 {en['E']:.5f} (낮음 < {E_LOW})")
    if ks: print(f"1번 킬스위치 ({ks['asof']} 기준) 그림자 3년 {ks['s1_3y']*100:+.1f}% vs RP {ks['rp_3y']*100:+.1f}% → {'켜짐' if ks['on'] else '정상'}")
    print(f"1번 체제 규칙: {'새 층 매수 중지 ' + '·'.join(rg['why']) if rg['stop'] else '정상 (새 층 매수 가능)'}")
    for x in st["rsi"]["layers"]:
        print(f"1번 {x['length']:>2}일 RSI {x['rsi']:5.1f}/{x['target']} {'보유' if x['held'] else '비움'} {x['action']:4s} 기준가 {x['price']:.2f} ({x['dist_pct']:+.1f}%)")


if __name__ == "__main__":
    main()
