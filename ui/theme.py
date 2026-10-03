"""SentinelGraph visual identity: near-black canvas with soft colour glows, Google's four colours as accents,
white pill buttons, geometric type (Outfit / Inter / JetBrains Mono) and gradient-edged cards."""

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{
  --bg:#08090C; --s1:#121318; --s2:#191A20; --s3:#202128; --line:#25262D; --line2:#33353E;
  --ink:#F1F3F4; --ink2:#C4C7CF; --mute:#8A8F9A;
  --blue:#8AB4F8; --g-blue:#4285F4; --g-red:#EA4335; --g-yellow:#FBBC04; --g-green:#34A853;
  --green:#34A853; --yellow:#FDD663; --pink:#F28B82; --lime:#81C995;
  --legit:#81C995; --legit-soft:rgba(129,201,149,.12);
  --crit:#F28B82; --crit-soft:rgba(242,139,130,.12);
  --warn:#FDD663; --warn-soft:rgba(253,214,99,.12);
  --grad:linear-gradient(120deg,#4285F4 0%,#34A853 38%,#FBBC04 68%,#EA4335 100%);
  --grad-soft:linear-gradient(120deg,rgba(66,133,244,.55),rgba(52,168,83,.45),rgba(251,188,4,.45),rgba(234,67,53,.5));
  --paper:#F8F9FA; --paper-ink:#15171C; --paper-mute:#5F6570; --paper-line:#E1E4EA;
  --cream:var(--paper); --cream-ink:var(--paper-ink); --cream-mute:var(--paper-mute); --cream-line:var(--paper-line);
  --display:"Outfit","Inter",system-ui,sans-serif;
  --serif:"Outfit","Inter",system-ui,sans-serif;
  --sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
body,.stApp,.stMarkdown,p,label,input,textarea,button,div[data-testid="stHtml"]{font-family:var(--sans)}
.stApp{background:
  radial-gradient(900px 520px at -8% 108%,rgba(66,133,244,.20),transparent 60%),
  radial-gradient(800px 480px at 108% 105%,rgba(234,120,53,.16),transparent 60%),
  radial-gradient(700px 380px at 55% -12%,rgba(52,168,83,.08),transparent 60%),var(--bg);background-attachment:fixed}
[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"],[data-testid="stDecoration"],footer,#MainMenu{display:none!important}
.block-container{padding-top:1.8rem!important;padding-bottom:3rem!important;max-width:1360px}

/* sidebar */
section[data-testid="stSidebar"]{background:rgba(12,13,17,.92);border-right:1px solid var(--line)}
[data-testid="stSidebarNav"] a{border-radius:999px;margin:1px 0}
[data-testid="stSidebarNav"] a:hover{background:rgba(255,255,255,.05)}
[data-testid="stSidebarNav"] a[aria-current="page"]{background:rgba(138,180,248,.12);box-shadow:inset 3px 0 0 var(--g-blue)}
[data-testid="stSidebarNav"] a[aria-current="page"] span{color:var(--ink)!important;font-weight:600}
[data-testid="stNavSectionHeader"]{font:500 11px/1.2 var(--mono)!important;letter-spacing:.14em;color:var(--mute)!important;text-transform:uppercase}

/* buttons: white pill like the event site, ghost pills for secondary */
.stButton>button,.stDownloadButton>button,[data-testid="stPopover"] button{border-radius:999px!important;border:1px solid var(--line2);
  background:rgba(255,255,255,.03);color:var(--ink)}
.stButton>button:hover,[data-testid="stPopover"] button:hover{border-color:#5A5E6A;background:rgba(255,255,255,.07);color:var(--ink)}
.stButton>button[kind="primary"]{background:#FFFFFF;color:#111216;border:0;font-weight:600;
  box-shadow:0 0 0 1px rgba(255,255,255,.08),0 8px 28px -8px rgba(138,180,248,.55)}
.stButton>button[kind="primary"]:hover{background:#E8EAED;color:#111216}
.stTextInput input,.stTextArea textarea,[data-baseweb="select"]>div{border-radius:12px!important;background:var(--s1)!important}
.stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--line)}
.stTabs [data-baseweb="tab"]{height:40px;padding:0 16px;color:var(--mute)}
.stTabs [aria-selected="true"]{color:var(--ink)!important}
.stTabs [data-baseweb="tab-highlight"]{background:var(--grad)}

/* type */
.eyebrow{font:500 11px/1.4 var(--mono);letter-spacing:.13em;text-transform:uppercase;color:var(--mute)}
.mono{font-family:var(--mono)!important}
.muted{color:var(--mute)} .ink2{color:var(--ink2)}
h1.page{font:600 46px/1.04 var(--display);letter-spacing:-.025em;color:var(--ink);margin:6px 0 10px}
.sub{color:var(--ink2);font-size:15px;line-height:1.6;margin-bottom:20px;max-width:980px}
.gtext{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}

/* gradient-edged surface (Google colours) */
.q4>div,.nba,.alertcard,.panel.glow{border:1px solid transparent!important;
  background:linear-gradient(var(--s1),var(--s1)) padding-box,var(--grad-soft) border-box!important}

/* status chips and badges */
.chip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 11px;font:500 12px/1.5 var(--mono);
  border:1px solid var(--line2);color:var(--ink2);background:var(--s2)}
.chip .d{width:7px;height:7px;border-radius:50%}
.c-legit{color:var(--legit);border-color:rgba(129,201,149,.35);background:var(--legit-soft)}
.c-crit{color:var(--crit);border-color:rgba(242,139,130,.4);background:var(--crit-soft)}
.c-warn{color:var(--warn);border-color:rgba(253,214,99,.38);background:var(--warn-soft)}
.badge{display:inline-block;font:600 10.5px/1.7 var(--mono);letter-spacing:.06em;padding:0 8px;border-radius:999px;
  background:var(--s3);color:var(--ink2);border:1px solid var(--line2)}
.badge.tg{background:rgba(253,214,99,.10);color:var(--yellow);border-color:rgba(253,214,99,.3)}
.badge.mem{background:rgba(129,201,149,.10);color:var(--legit);border-color:rgba(129,201,149,.3)}
.badge.cust{background:rgba(242,139,130,.10);color:var(--pink);border-color:rgba(242,139,130,.3)}
.badge.rag{background:rgba(138,180,248,.10);color:var(--blue);border-color:rgba(138,180,248,.3)}

/* case header */
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.head .line{font:500 13px/1.6 var(--mono);color:var(--ink2);margin-top:4px}

/* four questions */
.q4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0 16px}
@media(max-width:1100px){.q4{grid-template-columns:1fr 1fr}}
.q4>div{border-radius:18px;padding:18px 20px}
.q4 .eyebrow{color:var(--blue)}
.q4 .a{font:600 19px/1.3 var(--display);color:var(--ink);margin-top:10px;letter-spacing:-.01em}
.q4 .b{font-size:13px;line-height:1.55;color:var(--ink2);margin-top:8px}
.q4 .next{background:linear-gradient(160deg,rgba(66,133,244,.20),rgba(52,168,83,.08) 60%,rgba(0,0,0,0)) padding-box,
  linear-gradient(var(--s1),var(--s1)) padding-box,var(--grad) border-box!important}
.q4 .next .eyebrow{color:var(--ink)} .q4 .next .a{color:var(--yellow)}

/* kpis */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(3,1fr)}}
.kpi{border:1px solid var(--line);border-radius:16px;padding:14px 16px;background:var(--s1)}
.kpi .n{font:600 24px/1.15 var(--display);color:var(--ink);font-variant-numeric:tabular-nums;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .l{font-size:12px;color:var(--mute);margin-top:2px}

/* next best action */
.nba{border-radius:20px;padding:22px 24px;height:100%;
  background:linear-gradient(165deg,rgba(66,133,244,.16),rgba(0,0,0,0) 55%) padding-box,
  linear-gradient(var(--s1),var(--s1)) padding-box,var(--grad) border-box!important}
.nba .big{font:600 34px/1.08 var(--display);letter-spacing:-.02em;color:var(--ink);margin:10px 0 8px;display:flex;align-items:center;gap:12px}
.nba .big .ic{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;font:700 18px var(--mono)}
.nba .why{font-size:14.5px;line-height:1.6;color:var(--ink2);max-width:680px}
.alist{display:flex;flex-direction:column;gap:6px;margin-top:16px}
.arow{display:grid;grid-template-columns:22px 1fr auto;gap:10px;align-items:center;padding:9px 12px;border-radius:12px;background:rgba(255,255,255,.035);border:1px solid var(--line)}
.arow .t{font:500 13.5px var(--sans);color:var(--ink)}
.arow .r{font:12px var(--sans);color:var(--mute)}
.arow .s{font:500 11px var(--mono);color:var(--mute);text-align:right;white-space:nowrap}
.gov{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-radius:14px;overflow:hidden;margin-top:16px}
@media(max-width:1000px){.gov{grid-template-columns:1fr 1fr}}
.gov div{background:var(--s2);padding:11px 13px}
.gov .v{font:500 13px/1.4 var(--sans);color:var(--ink);margin-top:3px}

/* panels */
.panel{border:1px solid var(--line);border-radius:20px;background:var(--s1);padding:18px 20px;height:100%}
.meter{margin:10px 0 14px}
.meter .top{display:flex;justify-content:space-between;font-size:13px;color:var(--ink2)}
.meter .top b{font:600 13px var(--mono);color:var(--ink)}
.meter .track{height:8px;border-radius:999px;background:var(--s3);margin-top:6px;overflow:hidden}
.meter .fill{height:100%;border-radius:999px}
.tick{display:grid;grid-template-columns:18px 1fr;gap:8px;font-size:13.5px;line-height:1.45;color:var(--ink2);padding:4px 0}
.tick .i{font:700 13px var(--mono)}
.stopbox{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}
.stopbox h4{font:600 17px/1.2 var(--display);color:var(--ink);margin:0 0 8px}

/* evolution */
.evo{display:grid;grid-template-columns:1fr 44px 1fr 44px 1fr;align-items:stretch;margin:6px 0 4px}
@media(max-width:1000px){.evo{grid-template-columns:1fr}.evo .arr{display:none}}
.evo .st{border:1px solid var(--line);border-radius:18px;background:var(--s1);padding:14px 16px}
.evo .st.mid{border-style:dashed;border-color:var(--line2);background:transparent}
.evo .arr{display:grid;place-items:center;color:var(--blue);font:700 20px var(--mono)}
.evo .row{display:flex;justify-content:space-between;font-size:13px;color:var(--ink2);padding:3px 0}
.evo .row b{font:500 13px var(--mono);color:var(--ink)}
.evo .sact{margin-top:8px;font:600 15px var(--display);color:var(--ink)}
.evo .reply{font-size:13px;line-height:1.5;color:var(--ink2);margin-top:8px}

/* evidence cards: dark, colour bar on the left */
.ev{background:var(--s1);color:var(--ink);border:1px solid var(--line);border-radius:14px;padding:12px 15px;box-shadow:inset 4px 0 0 var(--g-green)}
.ev.fraud{box-shadow:inset 4px 0 0 var(--g-red)} .ev.legit{box-shadow:inset 4px 0 0 var(--g-green)} .ev.neutral{box-shadow:inset 4px 0 0 #5F6570}
.ev .c{font-size:14px;line-height:1.55}
.ev .m{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
.ev .m .ref{font:11.5px var(--mono);color:var(--mute)}
.ev .lr{font:600 11.5px var(--mono);color:var(--ink);margin-left:auto}
.evgrid{display:flex;flex-direction:column;gap:8px}
.sec{display:flex;align-items:baseline;justify-content:space-between;margin:34px 0 12px;padding-bottom:10px;position:relative}
.sec::after{content:"";position:absolute;left:0;bottom:0;width:72px;height:3px;border-radius:3px;background:var(--grad)}
.sec h3{font:600 24px/1.2 var(--display);letter-spacing:-.015em;color:var(--ink);margin:0}
.layer{font:600 12px var(--mono);letter-spacing:.1em;text-transform:uppercase;margin:14px 0 8px;display:flex;gap:8px;align-items:center}
.layer .n{width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:11px;background:var(--s3);color:var(--ink)}
.assess{display:grid;grid-template-columns:170px 1fr 64px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}
.assess .bar{height:10px;border-radius:999px}
.assess .x{font:500 12px var(--mono);color:var(--ink);text-align:right}
.decision{border:1px solid var(--line2);border-radius:14px;padding:12px 14px;background:var(--s2);font-size:13.5px;line-height:1.55;color:var(--ink2)}

/* activity */
.act{display:grid;grid-template-columns:58px 14px 1fr;gap:8px;padding:6px 0;font-size:13px;line-height:1.4}
.act .tm{font:500 11.5px var(--mono);color:var(--mute);padding-top:1px}
.act .dt{width:9px;height:9px;border-radius:50%;margin-top:5px}
.act .tt{color:var(--ink)} .act .xx{font:11.5px var(--mono);color:var(--mute);margin-top:2px}

/* tables */
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:18px;background:var(--s1)}
.tbl table{border-collapse:collapse;width:100%;font-size:13px;min-width:960px}
.tbl th{font:500 11px/1.3 var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--mute);text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}
.tbl td{padding:11px 14px;border-bottom:1px solid var(--line);color:var(--ink2);vertical-align:top}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.tbl tr:last-child td{border-bottom:0}
.tbl .num{font-family:var(--mono);text-align:right;color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl a,.sub a,.ev a{color:var(--blue);text-decoration:none;font-family:var(--mono);white-space:nowrap}
.tbl .acts{font:11.5px/1.5 var(--mono);color:var(--mute)}
.stat4{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}

/* SAR: a light paper document, like a real filing */
.sar{background:var(--paper);color:var(--paper-ink);border-radius:18px;overflow:hidden;box-shadow:0 20px 50px -24px rgba(0,0,0,.7)}
.sar .hd{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--paper-line)}
.sar .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));border-bottom:1px solid var(--paper-line)}
.sar .grid div{padding:10px 20px}
.sar .eyebrow{color:var(--paper-mute)}
.sar .body{padding:18px 20px;font-size:14.5px;line-height:1.7;max-width:920px}
.sar .badge{background:#E8EAED;color:#3C4043;border-color:#DADCE0}

.side-status{font-size:12.5px;color:var(--ink2);line-height:1.95;border-top:1px solid var(--line);padding-top:10px;margin-top:8px}
.side-status .d{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.side-status b{font:500 12px var(--mono);color:var(--ink);float:right}
.empty{border:1px dashed var(--line2);border-radius:20px;padding:44px;text-align:center;color:var(--mute);background:rgba(255,255,255,.015)}
.lv{display:grid;grid-template-columns:96px 1fr auto;gap:10px;font-size:13px;padding:3px 0;border-bottom:1px solid var(--line)}
.lv .k{font:500 11px/1.8 var(--mono);text-transform:uppercase}
.lv .x{font:11.5px var(--mono);color:var(--mute);white-space:nowrap}

/* policy markdown */
.md{font-size:14px;line-height:1.6;color:var(--ink2)}
.md p{margin:0 0 8px} .md ul{margin:0 0 8px 18px;padding:0}
.md .ph{font:600 14px var(--sans);margin:6px 0 4px;color:var(--ink)}
.md code.pc{font:500 12px var(--mono);background:var(--s3);color:var(--blue);padding:1px 6px;border-radius:6px;white-space:nowrap}
.md table.pt{border-collapse:collapse;width:100%;margin:4px 0 10px;font-size:13px}
.md table.pt th{font:500 11px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--mute);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.md table.pt td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;line-height:1.9}

/* run banner, comparison, alert card */
.runbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font:12.5px var(--mono);color:var(--ink2);
  border:1px solid var(--line2);border-radius:999px;padding:8px 16px;margin:4px 0 12px;background:var(--s1)}
.runbar.live{border-color:rgba(129,201,149,.45);background:rgba(129,201,149,.06)}
.cmp{border:1px solid var(--line2);border-radius:18px;background:var(--s1);padding:14px 16px;margin:0 0 16px;font-size:13.5px;color:var(--ink2)}
.cmpgrid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
@media(max-width:1000px){.cmpgrid{grid-template-columns:repeat(3,1fr)}}
.cmpgrid .v{font:500 13px/1.4 var(--mono);margin-top:3px}
.alertcard{border-radius:20px;padding:20px 22px}
.alertcard .quote{font:500 22px/1.45 var(--display);color:var(--ink);letter-spacing:-.01em}
</style>
"""
