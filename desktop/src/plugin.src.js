// UNBL0CK — everything blocked on you, in one place. Plain ESM: no JSX, no build step
// beyond desktop/build.py, which inlines the subset fonts (/*@@FONTS@@*/) into
// desktop/plugin.js. Visual language: the X55 Kyanite instrument (hermes-gadget-x55
// design/ART-DIRECTION.md): near-black mineral base, steel structure, cyan data,
// amber action, magenta only for faults. One solid primary action per card.
import {
  atom,
  host,
  useValue,
  KANBAN_CARD_AREAS,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PATH = '/unblock'
const POLL_MS = 45000
const $queue = atom({ items: [], count: 0, errors: [], loading: true, at: 0 })
const $undo = atom(null)   // the last "Not for me": { id, title } while it can be undone
const UNDO_MS = 12000
// An answer that says to keep the card blocked, sent with the unblocking button.
const KEEP_BLOCKED = /\b(keep|leave|stay|remain)s?\b[^.]{0,24}\bblocked\b|\b(do not|don't|dont|never)\s+(unblock|release)/i

const FONTS = /*@@FONTS@@*/ {}

const CSS = `
${Object.entries(FONTS)
  .map(([fam, src]) => `@font-face{font-family:'UB ${fam.split(':')[0]}';font-weight:${fam.split(':')[1]};font-display:swap;src:url(data:font/woff2;base64,${src}) format('woff2')}`)
  .join('\n')}
.ub{--base:#05070a;--panel:#0a0f16;--panel2:#111a24;--text:#f4f7fb;--muted:#b9c4d3;--dim:#7e8a9b;
  --steel:#596e87;--steeld:#38475c;--label:#8ca1ba;--cyan:#00f5ff;--green:#45b280;--amber:#f0a202;--mag:#ff2bd6;--hazard:#6b1357;
  --disp:'UB Rajdhani','Rajdhani','Bahnschrift','DIN Alternate',system-ui,sans-serif;
  --mono:'UB ShareTechMono','Share Tech Mono','SF Mono',ui-monospace,monospace;
  position:relative;height:100%;width:100%;overflow:hidden;background:var(--base);color:var(--text);font-family:var(--disp);
  --ascii:ui-monospace,'SF Mono',Menlo,Consolas,monospace}
.ub *{box-sizing:border-box}
.ub-wrap{max-width:980px;margin:0 auto;padding:28px 32px 56px;position:relative}
.ub-corner{position:absolute;width:22px;height:22px;border-color:var(--steel);border-style:solid;opacity:.9;pointer-events:none}
.ub-corner.tl{top:12px;left:12px;border-width:2px 0 0 2px}.ub-corner.tr{top:12px;right:12px;border-width:2px 2px 0 0}
.ub-eyebrow{font-family:var(--mono);font-size:14px;letter-spacing:.14em;color:var(--label);text-transform:uppercase}
.ub-ticks{display:flex;gap:6px;margin:0 0 10px}.ub-ticks i{display:block;width:14px;height:3px}
.ub-head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap;
  border-bottom:1px solid var(--steeld);padding-bottom:18px;margin-bottom:22px}
.ub-title{font-weight:700;font-size:52px;line-height:.95;letter-spacing:.02em;margin:6px 0 6px;color:var(--text);
  text-shadow:-2px 0 0 rgba(255,43,214,.75),2px 0 0 rgba(0,245,255,.75)}
.ub-sub{font-size:19px;font-weight:500;color:var(--muted)}
.ub-tiles{display:flex;gap:10px;flex-wrap:wrap}
.ub-tile{min-width:104px;padding:9px 14px 8px;background:var(--panel);border:1px solid var(--steeld);
  clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px)}
.ub-tile b{display:block;font-family:var(--mono);font-weight:400;font-size:26px;line-height:1.05;color:var(--cyan)}
.ub-tile span{font-family:var(--mono);font-size:12px;letter-spacing:.14em;color:var(--label)}
.ub-tile.warn b{color:var(--amber)}.ub-tile.ok b{color:var(--green)}
.ub-sec{display:flex;align-items:center;gap:12px;margin:30px 0 14px}
.ub-sec .ub-eyebrow{color:var(--label)}.ub-sec hr{flex:1;border:0;border-top:1px solid var(--steeld);margin:0}
.ub-list{display:flex;flex-direction:column;gap:14px}
.ub-card{position:relative;padding:1px;background:var(--steel);
  clip-path:polygon(0 0,calc(100% - 18px) 0,100% 18px,100% 100%,18px 100%,0 calc(100% - 18px))}
.ub-card>.in{background:var(--panel);padding:18px 22px 18px 18px;display:grid;grid-template-columns:56px 1fr;gap:4px 18px;
  clip-path:polygon(0 0,calc(100% - 17px) 0,100% 17px,100% 100%,17px 100%,0 calc(100% - 17px))}
.ub-card>.in>:not(.ub-idx){grid-column:2}
.ub-card:hover,.ub-card:focus-within{background:var(--amber)}
.ub-idx{grid-row:1/span 4;width:52px;height:52px;display:grid;place-items:center;font-family:var(--mono);font-size:20px;color:var(--cyan);
  background:var(--base);border:1px solid var(--steel);clip-path:polygon(12px 0,100% 0,100% 100%,0 100%,0 12px)}
.ub-card.chat .ub-idx{color:var(--green)}
.ub-top{display:flex;justify-content:space-between;align-items:baseline;gap:14px}
.ub-h{font-weight:700;font-size:22px;line-height:1.15;letter-spacing:.01em;text-transform:uppercase;color:var(--text);margin:0}
.ub-age{flex:none;font-family:var(--mono);font-size:14px;letter-spacing:.08em;color:var(--dim)}
.ub-age.warn{color:var(--amber)}.ub-age.stale{color:var(--mag)}
.ub-meta{font-family:var(--mono);font-size:13.5px;letter-spacing:.06em;color:var(--dim);text-transform:uppercase}
.ub-meta em{font-style:normal;color:var(--cyan)}
.ub-ask{margin:8px 0 2px;font-size:18px;font-weight:500;line-height:1.45;color:var(--muted);white-space:pre-wrap;
  max-height:5.8em;overflow:hidden;cursor:pointer;-webkit-mask-image:linear-gradient(#000 3.6em,transparent 5.8em);mask-image:linear-gradient(#000 3.6em,transparent 5.8em)}
.ub-ask.open{max-height:none;-webkit-mask-image:none;mask-image:none;cursor:default}
.ub-bluf{margin:10px 0 4px;font-size:21px;font-weight:600;line-height:1.35;color:var(--text)}
.ub-full{justify-self:start;background:none;border:0;padding:4px 0;font-family:var(--mono);font-size:13px;letter-spacing:.12em;color:var(--cyan);cursor:pointer}
.ub-full:hover,.ub-full:focus-visible{color:var(--text)}
.ub-pending{font-family:var(--mono);font-size:12px;letter-spacing:.14em;color:var(--cyan);opacity:.8;animation:ub-blink2 1.06s steps(1) infinite}
.ub-zero{font-family:var(--mono);color:var(--cyan);font-size:.92em}
.ub-why{font-size:16px;font-weight:500;color:var(--muted)}.ub-why b{font-family:var(--mono);font-weight:400;color:var(--amber);letter-spacing:.12em;margin-right:8px}
.ub-act{grid-column:2;display:flex;flex-direction:column;gap:10px;margin-top:10px}
.ub-ta{width:100%;resize:vertical;background:var(--base);color:var(--text);border:1px solid var(--steel);
  padding:9px 12px;font:500 17px/1.4 var(--disp);outline:none;height:44px;min-height:44px;transition:height .15s}
.ub-ta:focus,.ub-ta.has{height:110px}
.ub-ta::placeholder{color:var(--dim)}.ub-ta:focus{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber)}
.ub-btns{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.ub-btns.tools{gap:4px 6px;margin-left:-14px}.ub-btns.tools .ub-btn{padding:0 14px}
.ub-btn{height:40px;padding:0 18px;border:1px solid var(--steel);background:var(--panel2);color:var(--text);cursor:pointer;
  font:700 16px/1 var(--disp);letter-spacing:.08em;text-transform:uppercase;
  clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px)}
.ub-btn:hover:not(:disabled){border-color:var(--cyan);color:var(--cyan)}
.ub-btn.primary{background:var(--amber);border-color:var(--amber);color:#05070a}
.ub-btn.primary:hover:not(:disabled){background:#ffb420;color:#05070a}
.ub-btn.ghost{background:transparent;border-color:transparent;color:var(--dim);font-weight:500}
.ub-btn.ghost:hover:not(:disabled){color:var(--text);border-color:transparent}
.ub-btn:disabled{opacity:.55;cursor:default}
.ub-btn.primary:disabled{background:transparent;border-color:rgba(240,162,2,.45);color:rgba(240,162,2,.7)}
.ub-btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.ub-key{display:inline-grid;place-items:center;width:20px;height:20px;margin-right:8px;font-family:var(--mono);font-size:13px;
  background:#05070a;color:var(--amber);clip-path:polygon(5px 0,100% 0,100% 100%,0 100%,0 5px)}
.ub-clear{margin-top:30px;padding:28px;border:1px solid var(--steeld);background:var(--panel);display:flex;gap:16px;align-items:center}
.ub-clear b{font-family:var(--mono);font-weight:400;font-size:15px;letter-spacing:.14em;color:var(--green)}
.ub-clear span{font-size:20px;font-weight:500;color:var(--muted)}
.ub-err{margin:0 0 14px;padding:10px 14px;border:1px solid rgba(255,43,214,.45);border-left:3px solid var(--mag);background:rgba(107,19,87,.18);
  font-family:var(--mono);font-size:13px;line-height:1.5;color:var(--mag);letter-spacing:.04em;overflow-wrap:anywhere}
.ub-err b{font-weight:400;color:var(--text);margin-right:8px}
.ub-card:focus{outline:none}
.ub-undo{display:flex;align-items:center;gap:12px;margin:0 0 14px;padding:8px 8px 8px 14px;border:1px solid var(--steeld);border-left:3px solid var(--steel);background:rgba(10,14,20,.7);font-family:var(--mono);font-size:13px;letter-spacing:.04em;color:var(--label)}
.ub-undo span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ub-undo b{font-weight:400;color:var(--text);margin-right:8px}
.ub-hint{margin:2px 0 0;font-family:var(--mono);font-size:12.5px;line-height:1.5;letter-spacing:.04em;color:var(--amber)}
.ub-hint::before{content:'▲ ';}
.ub-hazard{height:6px;margin-bottom:12px;background:repeating-linear-gradient(135deg,var(--hazard) 0 8px,transparent 8px 16px)}
.ub-chip{background:none;border:0;cursor:pointer;padding:0 8px;font:400 12px/1 'UB ShareTechMono','Share Tech Mono','SF Mono',ui-monospace,monospace;letter-spacing:.1em;color:#f0a202}
.ub-chip:hover,.ub-chip:focus-visible{color:#00f5ff}
/* ── card context + ask drawer ── */
.ub-detail{grid-column:2;display:flex;flex-direction:column;gap:10px;margin-top:2px}
.ub-dtoggle{align-self:flex-start;background:none;border:1px solid var(--steeld);color:var(--label);cursor:pointer;
  padding:5px 12px;font:400 12.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;
  clip-path:polygon(8px 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%,0 8px)}
.ub-dtoggle:hover,.ub-dtoggle[aria-expanded="true"]{border-color:var(--cyan);color:var(--cyan)}
.ub-panel{border:1px solid var(--steeld);background:rgba(5,7,10,.55);padding:14px 16px;
  clip-path:polygon(0 0,calc(100% - 12px) 0,100% 12px,100% 100%,12px 100%,0 calc(100% - 12px));
  display:flex;flex-direction:column;gap:12px}
.ub-block{margin:0;padding:10px 12px;border-left:3px solid var(--amber);background:rgba(240,162,2,.08);
  font-size:16px;line-height:1.45;color:var(--text);white-space:pre-wrap}
.ub-block b{display:block;font-family:var(--mono);font-weight:400;font-size:11.5px;letter-spacing:.14em;
  color:var(--amber);margin-bottom:4px}
.ub-body{margin:0;font-size:16px;font-weight:500;line-height:1.5;color:var(--muted);white-space:pre-wrap}
.ub-kvs{display:grid;grid-template-columns:max-content 1fr;gap:5px 14px;font-size:15px;color:var(--muted);margin:0}
.ub-kvs dt{font-family:var(--mono);font-size:12px;letter-spacing:.1em;color:var(--label);text-transform:uppercase;padding-top:2px}
.ub-kvs dd{margin:0;min-width:0;overflow-wrap:anywhere}
.ub-kvs dd em{font-style:normal;color:var(--cyan)}
.ub-subhead{font-family:var(--mono);font-size:12px;letter-spacing:.14em;color:var(--label);text-transform:uppercase;
  border-bottom:1px solid var(--steeld);padding-bottom:5px;margin:2px 0 0}
.ub-cmt{padding:8px 0;border-bottom:1px solid rgba(89,110,135,.28)}
.ub-cmt:last-child{border-bottom:0}
.ub-cmt .h{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--dim);text-transform:uppercase;margin-bottom:3px}
.ub-cmt .h em{font-style:normal;color:var(--cyan)}
.ub-cmt p{margin:0;font-size:15.5px;line-height:1.45;color:var(--muted);white-space:pre-wrap}
.ub-ev{display:flex;gap:10px;font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--dim);text-transform:uppercase}
.ub-ev em{font-style:normal;color:var(--cyan);min-width:160px}
.ub-note{font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--dim);letter-spacing:.04em}
.ub-fault{font-family:var(--mono);font-size:12.5px;line-height:1.5;color:var(--mag);white-space:pre-wrap}
.ub-askwrap{border:1px solid var(--steeld);background:rgba(5,7,10,.55);display:flex;flex-direction:column}
.ub-askwrap .hd{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 12px;
  border-bottom:1px solid var(--steeld);font-family:var(--mono);font-size:12px;letter-spacing:.12em;color:var(--label);text-transform:uppercase}
.ub-msgs{display:flex;flex-direction:column;gap:9px;padding:12px;max-height:300px;overflow-y:auto}
.ub-msg{max-width:92%;padding:8px 11px;border:1px solid var(--steeld);background:var(--panel2);
  clip-path:polygon(0 0,calc(100% - 8px) 0,100% 8px,100% 100%,8px 100%,0 calc(100% - 8px))}
.ub-msg.user{align-self:flex-end;border-color:rgba(0,245,255,.45);background:rgba(0,245,255,.06)}
.ub-msg.assistant{align-self:flex-start;border-color:rgba(240,162,2,.35)}
.ub-msg .who{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;color:var(--label);text-transform:uppercase;margin-bottom:3px}
.ub-msg p{margin:0;font-size:15.5px;line-height:1.48;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere}
.ub-msg .use{margin-top:7px}
.ub-chatbar{display:flex;gap:8px;align-items:flex-end;padding:10px 12px;border-top:1px solid var(--steeld)}
.ub-chatbar textarea{flex:1;min-height:40px;max-height:120px;resize:vertical;background:var(--base);color:var(--text);
  border:1px solid var(--steel);padding:8px 10px;font:500 15.5px/1.4 var(--disp);outline:none}
.ub-chatbar textarea:focus{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber)}
.ub-chatbar textarea::placeholder{color:var(--dim)}
.ub-thinking{font-family:var(--mono);font-size:12.5px;letter-spacing:.1em;color:var(--cyan)}
.ub-thinking::after{content:'…';animation:ub-blink2 1s steps(2) infinite}
/* ── layers: ASCII contour field (canvas) → tints/scanlines/grain → scrolling content ── */
.ub-field{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.ub-fx{position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(rgba(153,97,255,.07),rgba(153,97,255,0) 320px),
    linear-gradient(0deg,rgba(26,219,184,.045),rgba(26,219,184,0) 220px),
    repeating-linear-gradient(180deg,transparent 0 3px,rgba(0,0,0,.045) 3px 4px)}
.ub-grain{position:absolute;inset:-64px;pointer-events:none;opacity:.07;mix-blend-mode:overlay;background-size:256px 256px;
  animation:ub-grain .9s steps(6) infinite}
@keyframes ub-grain{0%{transform:translate(0,0)}20%{transform:translate(-31px,17px)}40%{transform:translate(23px,-41px)}
  60%{transform:translate(-11px,53px)}80%{transform:translate(47px,9px)}100%{transform:translate(0,0)}}
.ub-scroll{position:absolute;inset:0;overflow-y:auto}
/* ── hero: ANSI-shadow wordmark with the X55 glitch_title split ── */
.ub-hero{display:grid;grid-template-columns:1fr auto;gap:28px;align-items:end;position:relative}
.ub-water{position:absolute;right:-6px;top:-14px;margin:0;font:700 15px/1.05 var(--ascii);white-space:pre;pointer-events:none;user-select:none}
.ub-banner{position:relative;margin:12px 0 10px;font:13px/1.02 var(--ascii);white-space:pre;letter-spacing:0;user-select:none}
.ub-banner pre{margin:0;font:inherit;white-space:pre}
.ub-banner .s{color:var(--steel)}.ub-banner .b{color:var(--text)}
.ub-banner .c,.ub-banner .m{position:absolute;inset:0;opacity:.9;pointer-events:none}
.ub-banner .c{color:var(--cyan);transform:translateX(-1.5px)}.ub-banner .m{color:var(--mag);transform:translateX(1.5px)}
.ub-banner .c *,.ub-banner .m *{color:inherit!important}
.ub-banner .top{position:relative}
.ub-banner .c{animation:ub-gc 5.2s steps(1) infinite}.ub-banner .m{animation:ub-gm 5.2s steps(1) infinite}
.ub-banner .top{animation:ub-gt 5.2s steps(1) infinite}
@keyframes ub-gc{0%,88%,100%{transform:translateX(-1.5px);clip-path:none}
  89%{transform:translateX(-7px);clip-path:inset(18% 0 58% 0)}91%{transform:translateX(5px);clip-path:inset(62% 0 12% 0)}93%{transform:translateX(-3px);clip-path:inset(40% 0 40% 0)}}
@keyframes ub-gm{0%,88%,100%{transform:translateX(1.5px);clip-path:none}
  89%{transform:translateX(6px);clip-path:inset(55% 0 20% 0)}91%{transform:translateX(-5px);clip-path:inset(8% 0 70% 0)}93%{transform:translateX(4px);clip-path:inset(30% 0 52% 0)}}
@keyframes ub-gt{0%,88%,94%,100%{transform:none}90%{transform:translateX(3px)}92%{transform:translateX(-2px)}}
/* ── Nous girl tamagotchi: canvas pet in a chamfered shell, mood meter + XP ── */
.ub-pet{position:relative;width:360px;flex:none;background:rgba(10,15,22,.86);border:1px solid var(--steel);cursor:pointer;
  clip-path:polygon(14px 0,100% 0,100% calc(100% - 14px),calc(100% - 14px) 100%,0 100%,0 14px)}
.ub-pet canvas{display:block;width:360px;height:392px}
.ub-pet .bar{display:flex;justify-content:space-between;align-items:center;padding:6px 12px 8px;border-top:1px solid var(--steeld);
  font-family:var(--mono);font-size:12px;letter-spacing:.12em;color:var(--label)}
.ub-pet .bar b{font-weight:400}.ub-pet .hearts{color:var(--mag);letter-spacing:.2em;font-family:var(--ascii)}
.ub-pet:hover{border-color:var(--amber)}
/* ── status strip: tiles + SFX + ticker ── */
.ub-strip{display:flex;gap:10px;flex-wrap:wrap;align-items:stretch;margin:18px 0 0}
.ub-sfx{display:flex;align-items:center;gap:8px}
.ub-sfx .bars{display:inline-flex;gap:2px;align-items:flex-end;height:14px}
.ub-sfx .bars i{display:block;width:3px;background:currentColor;animation:ub-eq .9s ease-in-out infinite}
.ub-sfx .bars i:nth-child(2){animation-delay:-.3s}.ub-sfx .bars i:nth-child(3){animation-delay:-.6s}
.ub-sfx.off .bars i{animation:none;height:3px!important;opacity:.5}
@keyframes ub-eq{0%,100%{height:4px}50%{height:14px}}
.ub-ticker{position:relative;overflow:hidden;margin:16px 0 0;height:30px;border-top:1px solid var(--steeld);border-bottom:1px solid var(--steeld);
  background:rgba(5,7,10,.7);-webkit-mask-image:linear-gradient(90deg,transparent,#000 40px,#000 calc(100% - 40px),transparent)}
.ub-ticker .run{position:absolute;top:0;left:0;display:flex;white-space:nowrap;height:30px;align-items:center;
  font-family:var(--mono);font-size:13.5px;letter-spacing:.1em;color:var(--label);animation:ub-tick linear infinite}
.ub-ticker .run span{padding-right:44px}.ub-ticker em{font-style:normal;color:var(--cyan)}.ub-ticker b{font-weight:400;color:var(--amber)}
@keyframes ub-tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
/* ── card motion: staggered boot-in, hover scan, unbl0cked flash ── */
.ub-card{animation:ub-in .42s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(var(--i,0) * 55ms)}
@keyframes ub-in{0%{opacity:0;transform:translateX(-14px)}55%{opacity:1;transform:translateX(2px)}
  62%{transform:translateX(-3px) skewX(-2deg)}70%{transform:translateX(1px)}100%{opacity:1;transform:none}}
.ub-card>.in{position:relative}
.ub-card>.in::after{content:'';position:absolute;top:0;bottom:0;left:-30%;width:30%;pointer-events:none;opacity:0;
  background:linear-gradient(90deg,transparent,rgba(240,162,2,.07) 80%,rgba(240,162,2,.35) 99%,transparent)}
.ub-card:hover>.in::after{animation:ub-scan .7s ease-out}
@keyframes ub-scan{0%{left:-30%;opacity:1}100%{left:100%;opacity:1}}
.ub-card.done{background:var(--green)!important}
.ub-card.done>.in::before{content:'■ UNBL0CKED';position:absolute;inset:0;display:grid;place-items:center;z-index:2;
  font:400 26px var(--mono);letter-spacing:.3em;color:var(--green);background:rgba(5,7,10,.88);animation:ub-done .5s steps(4) both}
/* answered but still blocked (keep-blocked or triage): no false UNBL0CKED */
.ub-card.done.kept>.in::before{content:'■ ANSWERED'}
@keyframes ub-done{0%{opacity:0;letter-spacing:1.2em}100%{opacity:1;letter-spacing:.3em}}
.ub-chip.new{animation:ub-blink .5s steps(2) 6}
@keyframes ub-blink{50%{color:#ff2bd6}}
.ub-cursor{display:inline-block;width:.6em;height:1em;margin-left:4px;vertical-align:-2px;background:var(--amber);animation:ub-blink2 1.06s steps(1) infinite}
@keyframes ub-blink2{50%{opacity:0}}
@media (min-width:761px) and (max-width:1100px){.ub-pet,.ub-pet canvas{width:300px}.ub-pet canvas{height:327px}.ub-hero{gap:20px}.ub-banner{font-size:clamp(9.5px,1.34vw,11px)}}
@media (max-width:760px){.ub-hero{grid-template-columns:1fr}.ub-pet{display:none}.ub-banner{font-size:9px}.ub-water{display:none}}
@media (max-width:380px){.ub-banner{font-size:8px}}
@media (max-width:640px){.ub-wrap{padding:20px 16px 40px}
  .ub-card>.in{grid-template-columns:1fr}.ub-idx{display:none}.ub-card>.in>:not(.ub-idx){grid-column:1}
  .ub-ta{height:72px}.ub-btns.tools{margin-left:-10px}.ub-btns.tools .ub-btn{padding:0 10px;height:34px;font-size:14px}}
.ub.fx-off *,.ub.fx-off *::before,.ub.fx-off *::after{animation:none!important;transition:none!important}
.ub.fx-off .ub-grain,.ub.fx-off .ub-banner .c,.ub.fx-off .ub-banner .m{display:none}
.ub.fx-off .ub-ticker .run{position:static;flex-wrap:wrap}
@media (prefers-reduced-motion:reduce){.ub *,.ub *::before,.ub *::after{transition:none!important;animation:none!important}
  .ub-ticker .run{position:static;flex-wrap:wrap}}
`

// ── ASCII assets (pyfiglet ansi_shadow / standard; crystal = luminance pass over the sunstone logo) ──
const BANNER = [
  '██╗   ██╗███╗   ██╗██████╗ ██╗      ██████╗  ██████╗██╗  ██╗',
  '██║   ██║████╗  ██║██╔══██╗██║     ██╔═████╗██╔════╝██║ ██╔╝',
  '██║   ██║██╔██╗ ██║██████╔╝██║     ██║██╔██║██║     █████╔╝',
  '██║   ██║██║╚██╗██║██╔══██╗██║     ████╔╝██║██║     ██╔═██╗',
  '╚██████╔╝██║ ╚████║██████╔╝███████╗╚██████╔╝╚██████╗██║  ██╗',
  ' ╚═════╝ ╚═╝  ╚═══╝╚═════╝ ╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝'
]
const HERMES = [
  ' _   _ _____ ____  __  __ _____ ____',
  '| | | | ____|  _ \\|  \\/  | ____/ ___|',
  '| |_| |  _| | |_) | |\\/| |  _| \\___ \\',
  '|  _  | |___|  _ <| |  | | |___ ___) |',
  '|_| |_|_____|_| \\_\\_|  |_|_____|____/'
]
// Nous girl (Hermes Agent mascot art, MIT, © 2025 Nous Research; see NOTICE). 96×52 shape-matched ASCII of
// assets/nous-girl-white.svg: ink inside her silhouette becomes glyphs. blink/wink/talk use hermes-gadget face geometry.
const NOUS = {"base": ["                                          ._____________.", "                                  ,__~=<PMMMM%@@@@@@@@@@@#ggg__", "                             __ygZ______,      ^fM#@@@@@@@@@@@@#gg_", "                         _gg#@@@@@@@@@@@@#gg_      fM@@@@@@@@@@@@@@%g_", "                     ._g#@@@@@@@@@@@@@@@@@@@@%g,     ^M@@@@@@@@@@@@@@@g_.", "                  ._g@@@@@@@@@@@@@@@@@@@@@@@@@@@g_     ^M@@@@@@@@@@@@@@@#p.", "                _g@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@g,     T@@@@@@@@@@@@#@@#}_", "              _@@@@@@@@@@@@@@@@@@@@@@@@@%@@@#@Z%PfM#L     ?%@@@@@@@@@@@ZQ#E'l,", "            _%M#@@@@@@@Q@@@@@@@@@@@@@@@@bf#@B\"   ;p9@L     '%@@@D@@@@@Mfl\"l gp,", "          _gPg#Q%#@%@%l#@@%@#$@@@@@@@@@%M;'Q     J@g7ML     '%M%EfMM%Ml   .;%@p,", "         ;Pl4M5E%@Q#M.dM@B#@l$'^^fT@%MTt   '%ggjgZ@@%gZL     ?gg_ ?gg__g/_$g9@@p,", "        jt.r   fTtMt Z   \"^' $L,.. $     . ,'%@@@@@@@@@#,     ]@@%gZ@@@@#Zp##B@@p", "       jE_c.,; .r_gpj#ggggg@gZ@@@@@g#ggjg$Lg$%@@@@@@@@@@E      Q@@@@Z@@@@#Q@@#@@#L", "      .#Eg@Bggpg@@@##@@@@@@@@@%@@@@@@@@@@#@@%@@@@@@@@@@@@;.___gJ@@@@#$@@@@%%@@@@@EL", "      g#g@@Q@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@@@@@@@@@@@@@@@@Ld#@@Ml@@@@@@Q@@@@#@@@@@@p,", "     |@#@@%@@@@@@@@@@@@@@@@@@@@/@@@@@@@@@@@@@@@@@@@@@@@@@@@$@@EM%@@@@@E#@@@@@@@@@@#L", "     $@@@@@@@@@@@@@@@@@@@@@@@@@b7@@@@@@@@@@@@@@@@@@@@@@@@%Q#@@@@%gD@@@@Z@@@@@@@@@@@@,", "     $@@@@@@@@@@@@@@@#@@@@@@@@@@L7@@@@@@@@@@@@@@@@@@@@@@@#@@@@@@@@@@@@@%$@@@@@@@@@@@L", "     $@@@@@@@@@@@@@@@EM@@@@@@@@@@Lf#@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@E#@@@@@@@@@@#", "     $@@@@@@@@@@@@@@@@LfM@@@#%@@#MPfM@@@@@@@@$@@@@@@@@@@@@@@@@@@@@@@@@@@@Z@@@@@@@@@@@L", "     '#@@@@@@@@@@@@#MPf\" ^^'j@Pl_gg;_!\"'''''''^ffM@@@@@@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@E", "      7@@@@#@@@@@PfTL.        sQ@#@@@#gg_        |@@@@@@@@@@@@@@@@@@@@@@@@Z@@@@@@@@@@@,", "       ^M@@#7#@@@L_gg2c       !P\"@@@@@fM#pn-     |@@@@@@@@@@fl___fM@@@@@@@@$@@@@@@@@@@L", "         ^M%%/T#@@#T@@g        - ?%@@P._Mt       |@@@@@@@@@@@@@@@@@@@@@@@@@E#@@@@@@@@@#", "            'f* %@% %@F        \"Y<<nwKf\"         j@@@@@@@@@@@@@@@@@@@@@@@@@#Z@@@@@@@@@@L", "                @@@gpF                           $@@@@@@@@@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@", "               |@@%^                             @@@@@@@@@@@@@@@@@@@@@@@@@@@@Z@@@@@@@@@@L", "               j@@PL                            :@@@@@@@@@@@@@@@@@@@@@@@@@@@@%$@@@@@@@@@b", "               $@@['5_                          j@@@@@@@@@@@@@@@@@@@@@@@@@@@@@p@@@@@@@@@@L", "               $@@L  J                          %@@@@@@@@@@@@@@@@@@@@@@@@@@@@@bQ@@@@@@@@@b", "               #@@L ]b___                      |@@@@@@@@@@@@@@@@@@@@@@@@@@#@@@@$@@@@@@@@@@L", "              :@@@@, T#g@%g__,                 $@@@@@@@@@#@@@@@@@@@@@@@@@@Q@@@@E@@@@@@@@@@%", "              j@@@@@,?EY  -f^                  #@@@@@@@@E$@@@@@@@@@@@@@@@@Q@@@@%%@@@#@@@@@@L", "              $@@@@@g,'T\"                     j@@@@@@@@% $@@@@@@@@@@@@@@@@@@@@@@@@@@#%@@@@@#,", "    _         %@@@@@@g,                       $@@@@@@@@l ]@@@@@@@@@@@@@@@#@@@@@@@@@@@$@@@@@@L", "  .gl        |@@@@@@@@%_                      @@@@@@@@E .j@@@@@@@@@@@@#@@@@@@@@@@@@@@E@@@@@@#.", "  g@         $@@@@@@@@@#_                    .@@@@@@@#_g@@@@@@@@@@@@@@@@@@@@@@@@@@@@@BQ@@@@@@L", " .@@,       .#@@@@@@@@@@@g_________ggg_,     '@@@@@@@@@#P^$@@@@@@@@@@@@@@@@@@@@@@@@@@@$@@@@@@@", " !@@g,     .g@@@@@@@@@@@@@@@@@@@@@@@@@@@p    '@@@@@@@P\"   ?@@@@@@@@@@@@@@@@@@@@@@@@@@@$@@@@@@@;", " L%@@#g___g#@@@@@E@@@@@@@@@@@@@@@@@@@@@@@%_   Q@@@@@@L   _g@@@@@@@@@@@@@@@@@@@@@@@@@@@$@@@@@@@L", " $/#@@@@@@@@@@@%tg@@@@@@@@@@@@@@@@@@@@@@@@@__g@@@@@@@@#%#@@@@@@@@@@@@@@@@@@@@@@@@@@@@@$@@@@@@@E", " ]%/M#@@@@@@@Mt.g@@@@#Q@@@@@@@@@@@@@@@@@@@@@@M^3@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@B#@@@@@@@B", "  T@g_fffff^ _g#@@@@@l@@@@@@@@@@@@@@@@@@@@@M\"   7#@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@%@@@@@@@@D", "   fM@@#gggg%@@@@@@@F$@@@@@@@@@@@@@@@@#%MP\"      'fM@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@E", "     fM%@@@@@@@@@@M\"j@@@@@@@@@@@@@@@@@@_            ^TM@@@@@@@@@@@@@@@@##%#@@@@@@@@@@@@@@@@@@@L", "        'ff#MMMff\".g@@@@@@@@@@@@@@@@@@@#_           _g#@MQ@MMZg#@#MMMMMMMM@ggZTM@@@@@@@@@@@@@B", "           ]@ggggg@@@@@M$@@@@@@@@@@@@@@@@L      __g##Mf_n^_g#@Mf\"          'fMNgZM@@@@@@@@@@@l", "            fM@@@@@@@Mt|@@@@@@@@@@@@@@@@@@L __g#@MP\" _r'_g@#P\"                 fMg/M@@@@@@@%l", "              'ffff^' .%@@@@@@@@@@@@@@@@@@@#M#Mf'  ./\" g@#P                      f%/M@@@@%P", "                     _@@@@@@@@@@@@@@@@Pi@@P'^     _P .g@M\"                        ?#/#@M^", "                   .g#@@@@@@@@@@#MM@#lg@P\"      _gP  g@P                           ]E]%", "                   ffffffffffff\"'\"ff\"ff\"       'ff   ff'                           'f'T"], "patch": {"blink": [[20, "     '#@@@@@@@@@@@@#MPf\" ^^'j@P\"-~~- \"\"'''''''^ffM@@@@@@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@E"], [21, "      7@@@@#@@@@@PfTL.                           |@@@@@@@@@@@@@@@@@@@@@@@@Z@@@@@@@@@@@,"], [22, "       ^M@@#7#@@@L_           jg_,      ._gL     |@@@@@@@@@@fl___fM@@@@@@@@$@@@@@@@@@@L"], [23, "         ^M%%/T#@@#,  .        ^TMM@g@@MMf\"      |@@@@@@@@@@@@@@@@@@@@@@@@@E#@@@@@@@@@#"], [24, "            'f* %@#MMMP'                         j@@@@@@@@@@@@@@@@@@@@@@@@@#Z@@@@@@@@@@L"], [25, "                @@@,,                            $@@@@@@@@@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@"]], "wink": [[20, "     '#@@@@@@@@@@@@#MPf\" ^^'j@P\"-~~- \"\"'''''''^ffM@@@@@@@@@@@@@@@@@@@@@@@EQ@@@@@@@@@@E"], [21, "      7@@@@#@@@@@PfTL.                           |@@@@@@@@@@@@@@@@@@@@@@@@Z@@@@@@@@@@@,"], [22, "       ^M@@#7#@@@L_gg2c       jg_,      ._gL     |@@@@@@@@@@fl___fM@@@@@@@@$@@@@@@@@@@L"], [23, "         ^M%%/T#@@#T@@g        ^TMM@g@@MMf\"      |@@@@@@@@@@@@@@@@@@@@@@@@@E#@@@@@@@@@#"], [24, "            'f* %@% %@F                          j@@@@@@@@@@@@@@@@@@@@@@@@@#Z@@@@@@@@@@L"]], "talk": [[31, "              :@@@@, T#g%#g__,                 $@@@@@@@@@#@@@@@@@@@@@@@@@@Q@@@@E@@@@@@@@@@%"], [32, "              j@@@@@,?EMMMff^                  #@@@@@@@@E$@@@@@@@@@@@@@@@@Q@@@@%%@@@#@@@@@@L"]]}}
// X55 palette as rgb triplets (x55_pocket.py: MINT SIGNAL VIOLET ROSE GOLD PROOF)
const RGB = { mint: '89,255,184', signal: '0,245,255', violet: '153,97,255', rose: '255,97,148', gold: '255,189,56', proof: '240,162,2' }
const reducedMotion = () => {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false }
}

// ── Sound: sample-exact port of x55_pocket.py _tone/_seq/_env (22050 Hz mono, v2 levels) ──
const SFX_RATE = 22050
const env = (i, n, attack = 0.1, release = 0.45) => {
  const a = Math.max(1, Math.floor(n * attack)), r = Math.max(1, Math.floor(n * release))
  if (i < a) return i / a
  if (i >= n - r) return Math.max(0, (n - 1 - i) / r)
  return 1
}
const tone = (freq, ms, amp = 0.3, harm = 0) => {
  const n = Math.max(1, Math.floor(SFX_RATE * ms / 1000)), out = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const t = i / SFX_RATE
    let v = Math.sin(2 * Math.PI * freq * t)
    if (harm) v = (1 - harm) * v + harm * Math.sin(2 * Math.PI * freq * 2 * t)
    out[i] = v * amp * env(i, n)
  }
  return out
}
const seq = notes => {
  const gap = new Float32Array(Math.floor(SFX_RATE * 6 / 1000))
  const parts = notes.flatMap(([f, ms, amp]) => [tone(f, ms, amp), gap])
  const out = new Float32Array(parts.reduce((s, p) => s + p.length, 0))
  let o = 0
  for (const p of parts) { out.set(p, o); o += p.length }
  return out
}
const SFX_SPECS = {
  move: () => tone(1500, 14, 0.22, 0.08),
  ok: () => seq([[660, 45, 0.38], [990, 72, 0.3]]),
  warn: () => seq([[300, 70, 0.38], [220, 92, 0.32]]),
  err: () => seq([[200, 80, 0.42], [140, 120, 0.36]])
}
let sfxOnNow = true
try { sfxOnNow = localStorage.getItem('unbl0ck.sfx') !== 'off' } catch {}
const $sfxOn = atom(sfxOnNow)
let fxInit = true
try { fxInit = localStorage.getItem('unbl0ck.fx') !== 'off' } catch {}
const $fxOn = atom(fxInit)
const setFx = on => { $fxOn.set(on); try { localStorage.setItem('unbl0ck.fx', on ? 'on' : 'off') } catch {} }
const setSfx = on => { sfxOnNow = on; $sfxOn.set(on); try { localStorage.setItem('unbl0ck.sfx', on ? 'on' : 'off') } catch {} }
const sfx = (() => {
  let ac = null, bufs = null, lastAny = 0
  const last = {}
  const boot = () => {
    if (ac) return ac.state === 'suspended' ? ac.resume() : null
    try {
      ac = new (window.AudioContext || window.webkitAudioContext)()
      bufs = {}
      for (const [k, make] of Object.entries(SFX_SPECS)) {
        const data = make(), b = ac.createBuffer(1, data.length, SFX_RATE)
        b.copyToChannel(data, 0)
        bufs[k] = b
      }
    } catch { ac = null }
  }
  return {
    boot,
    play(name) {
      if (!sfxOnNow || !ac || !bufs || ac.state !== 'running') return
      const now = performance.now()
      if (now - lastAny < 30 || now - (last[name] || 0) < 45) return // X55 throttles: 30 ms global, 45 ms per sound
      lastAny = last[name] = now
      const src = ac.createBufferSource()
      src.buffer = bufs[name]
      src.connect(ac.destination)
      src.start()
    },
    close() { try { ac && ac.close() } catch {} ac = null }
  }
})()

// ── X55 draw_ascii_field: topographic contours of a Gaussian height map, drifting slowly ──
const BUMPS = [[0.16, 0.3, 0.23, 0.92], [0.7, 0.2, 0.3, 0.78], [0.52, 0.88, 0.38, 1.0], [0.9, 0.78, 0.24, 0.62]]
const LEVELS = [[0.3, RGB.mint], [0.46, RGB.signal], [0.62, RGB.violet], [0.78, RGB.rose], [0.92, RGB.gold]]
function heightAt(nx, ny, t) {
  let v = 0
  for (let k = 0; k < 4; k++) {
    const [cx0, cy0, r, a] = BUMPS[k]
    const cx = cx0 + 0.035 * Math.sin(t * 0.045 + k * 1.7), cy = cy0 + 0.03 * Math.cos(t * 0.038 + k * 2.3)
    v += a * Math.exp(-((nx - cx) ** 2 + (ny - cy) ** 2) / (2 * r * r))
  }
  return v + 0.1 * Math.sin(nx * 7 + ny * 2 + t * 0.07) + 0.06 * Math.cos(nx * 2 - ny * 8 - t * 0.05)
}
function paintField(g, w, h, t) {
  const cw = 9, chh = 14, size = 11, cols = Math.floor(w / cw) + 1, rows = Math.floor(h / chh) + 1
  const H = new Float32Array((cols + 1) * (rows + 1))
  for (let r = 0; r <= rows; r++) for (let c = 0; c <= cols; c++) H[r * (cols + 1) + c] = heightAt(c / cols, r / rows, t)
  g.clearRect(0, 0, w, h)
  g.font = `${size}px ui-monospace,Menlo,monospace`
  g.textBaseline = 'alphabetic'
  const batches = LEVELS.map(() => [])
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const v = H[r * (cols + 1) + c], vr = H[r * (cols + 1) + c + 1], vd = H[(r + 1) * (cols + 1) + c]
      for (let li = 0; li < LEVELS.length; li++) {
        const lv = LEVELS[li][0]
        const hx = (v - lv) * (vr - lv) <= 0 && Math.abs(v - vr) > 1e-9
        const vx = (v - lv) * (vd - lv) <= 0 && Math.abs(v - vd) > 1e-9
        if (hx || vx) {
          batches[li].push(hx && vx ? '+' : hx ? '-' : '|', c * cw, r * chh + size)
          break
        }
      }
    }
  }
  batches.forEach((b, li) => {
    g.fillStyle = `rgba(${LEVELS[li][1]},${0.1 + 0.018 * li})`
    for (let i = 0; i < b.length; i += 3) g.fillText(b[i], b[i + 1], b[i + 2])
  })
  // seeded speckle dust (random.seed(77); 240 per 1280×720)
  let s = 77
  const rnd = () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648)
  g.font = '10px ui-monospace,Menlo,monospace'
  g.fillStyle = 'rgba(147,163,183,.045)'
  const n = Math.round(240 * (w * h) / (1280 * 720))
  for (let i = 0; i < n; i++) g.fillText('.:+'[Math.floor(rnd() * 3)], rnd() * w, rnd() * h)
}
function Field({ fx }) {
  const ref = useRef(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const still = !fx || reducedMotion()
    const off = document.createElement('canvas')
    let w = 0, h = 0, dpr = 1, raf = 0, lastField = -1e9, alive = true
    const t0 = performance.now()
    const size = () => {
      const b = cv.getBoundingClientRect()
      dpr = Math.min(2, window.devicePixelRatio || 1)
      w = Math.max(1, Math.round(b.width)); h = Math.max(1, Math.round(b.height))
      for (const c of [cv, off]) { c.width = w * dpr; c.height = h * dpr }
      lastField = -1e9
    }
    const frame = now => {
      if (!alive) return
      raf = still ? 0 : requestAnimationFrame(frame)
      if (document.hidden) return
      const t = (now - t0) / 1000
      if (now - lastField > 260) { // contours drift slowly; re-trace ~4×/s
        const og = off.getContext('2d')
        og.setTransform(dpr, 0, 0, dpr, 0, 0)
        paintField(og, w, h, still ? 0 : t)
        lastField = now
      }
      const g = cv.getContext('2d')
      g.setTransform(1, 0, 0, 1, 0, 0)
      g.clearRect(0, 0, cv.width, cv.height)
      g.drawImage(off, 0, 0)
      if (still) return
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
      // full-height scan sweep at 52 px/s with trailing glow (draw_ascii_anim)
      const sy = (t * 52) % (h + 40) - 20
      g.fillStyle = 'rgba(0,245,255,.018)'; g.fillRect(0, sy - 28, w, 28)
      g.fillStyle = 'rgba(0,245,255,.10)'; g.fillRect(0, sy, w, 1)
      // occasional horizontal glitch slice, ±4 px
      if (Math.floor(t * 4) % 23 === 0) {
        const gy = Math.floor((t * 91) % Math.max(1, h - 8)), o = Math.floor(t * 7) % 2 ? 4 : -4
        g.save(); g.beginPath(); g.rect(0, gy, w, 6); g.clip()
        g.setTransform(1, 0, 0, 1, 0, 0)
        g.clearRect(0, gy * dpr, cv.width, 6 * dpr); g.drawImage(off, o * dpr, 0)
        g.restore()
        g.fillStyle = 'rgba(255,43,214,.18)'; g.fillRect(0, gy, w, 1)
      }
    }
    const ro = new ResizeObserver(() => { size(); if (still) frame(performance.now()) })
    ro.observe(cv)
    size()
    raf = requestAnimationFrame(frame)
    return () => { alive = false; cancelAnimationFrame(raf); ro.disconnect() }
  }, [fx])
  return jsx('canvas', { ref, className: 'ub-field', 'aria-hidden': true })
}

// film grain: one 256 px noise tile, generated once
let GRAIN = ''
function grainUrl() {
  if (GRAIN) return GRAIN
  try {
    const c = document.createElement('canvas')
    c.width = c.height = 256
    const g = c.getContext('2d'), img = g.createImageData(256, 256)
    for (let i = 0; i < img.data.length; i += 4) {
      const v = Math.random() * 255
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v
      img.data[i + 3] = 255
    }
    g.putImageData(img, 0, 0)
    GRAIN = `url(${c.toDataURL()})`
  } catch {}
  return GRAIN
}

const bannerRow = line =>
  line.split(/(█+)/).filter(Boolean).map((run, i) => jsx('span', { className: run[0] === '█' ? 'b' : 's', children: run }, i))
const bannerPre = cls => jsx('pre', { className: cls, 'aria-hidden': true, children: BANNER.map((l, i) => jsxs('div', { children: bannerRow(l) }, i)) })
const Banner = () =>
  jsxs('h1', {
    className: 'ub-banner',
    'aria-label': 'UNBL0CK',
    children: [bannerPre('c'), bannerPre('m'), bannerPre('top')]
  })
const WATER_TONES = [RGB.signal, RGB.violet, RGB.proof, RGB.mint, RGB.rose]
const Watermark = () =>
  jsx('pre', {
    className: 'ub-water',
    'aria-hidden': true,
    children: HERMES.map((l, i) => jsx('div', { style: { color: `rgba(${WATER_TONES[i]},.045)` }, children: l }, i))
  })
// ── Pet: tamagotchi loop. Mood follows the queue; events (unbl0cked, fault, new ask, pat) drive reactions ──
const petBus = new Set()
const petEmit = ev => petBus.forEach(f => f(ev))
const PET_ROWS = NOUS.base.length, PET_COLS = 96
const frameRows = name => {
  const rows = NOUS.base.slice()
  for (const [r, line] of NOUS.patch[name] || []) rows[r] = line
  return rows
}
const PET_FRAMES = Object.fromEntries(['idle', 'blink', 'wink', 'talk'].map(n => [n, frameRows(n)]))
// Selective ramps from the X55 accents (not a rainbow): each mood drifts back and forth along 2-3 colours.
const RAMPS = {
  calm: [[0, 245, 255], [92, 140, 255], [153, 97, 255]],      // signal cyan → periwinkle → violet
  scan: [[0, 245, 255], [92, 140, 255], [153, 97, 255]],
  happy: [[89, 255, 184], [0, 245, 255], [120, 200, 255]],    // mint → cyan → ice
  worried: [[255, 189, 56], [240, 162, 2], [255, 97, 148]]   // gold → amber → rose
}
const rampAt = (stops, u) => {
  u = ((u % 2) + 2) % 2; if (u > 1) u = 2 - u                  // ping-pong, no hard wrap
  const f = u * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(f)), k = f - i
  const a = stops[i], b = stops[i + 1]
  return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]
}
const DENSE = new Set('@#%*')
const SCHEDULE = { // [frame, ms] loops per mood
  calm: [['idle', 2600], ['blink', 120], ['idle', 1800], ['talk', 140], ['idle', 100], ['talk', 140], ['idle', 1400], ['blink', 120], ['idle', 900], ['wink', 300], ['idle', 1600]],
  happy: [['idle', 1500], ['wink', 320], ['idle', 1200], ['talk', 120], ['idle', 90], ['talk', 120], ['idle', 90], ['talk', 120], ['idle', 1300], ['blink', 110], ['idle', 800]],
  worried: [['idle', 900], ['blink', 110], ['idle', 300], ['blink', 110], ['idle', 1800], ['talk', 140], ['idle', 120], ['talk', 140], ['idle', 2000]],
  scan: [['idle', 500], ['blink', 110], ['idle', 400], ['talk', 120], ['idle', 300]]
}
const MOOD_LABEL = { happy: 'HAPPY', calm: 'OK', worried: 'NEEDS YOU', scan: 'SCANNING' }
function Pet({ count, oldestH, loading, fx }) {
  const ref = useRef(null)
  const [xp, setXp] = useState(0)
  const mood = loading ? 'scan' : !count ? 'happy' : count > 6 || oldestH > 72 ? 'worried' : 'calm'
  const moodRef = useRef(mood)
  moodRef.current = mood
  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const still = !fx || reducedMotion()
    const W = 360, H = 392, dpr = Math.min(2, window.devicePixelRatio || 1)
    cv.width = W * dpr; cv.height = H * dpr
    const g = cv.getContext('2d')
    const cw = 3.6, lh = 7.2, size = 6, ox = (W - PET_COLS * cw) / 2, oy = 8
    let raf = 0, alive = true, step = 0, stepAt = performance.now(), react = null, reactUntil = 0
    const parts = []
    const burst = (chars, rgb, n, x = W / 2, y = H * 0.42) => {
      for (let i = 0; i < n; i++) parts.push({ ch: chars[i % chars.length], x: x + (Math.random() - 0.5) * 90, y: y + (Math.random() - 0.5) * 40,
        vx: (Math.random() - 0.5) * 40, vy: -30 - Math.random() * 50, life: 1.4 + Math.random() * 0.6, rgb })
    }
    const onEvent = ev => {
      const now = performance.now()
      if (ev === 'joy') { react = { frame: 'wink', hop: true }; reactUntil = now + 1500; burst('*+*+', [255, 97, 148], 14); setXp(x => x + 1) }
      else if (ev === 'pat') { react = { frame: 'wink', hop: true }; reactUntil = now + 900; burst('*', [255, 43, 214], 4, W * 0.3, 150) }
      else if (ev === 'err') { react = { frame: 'blink', shake: true, tint: [255, 43, 214] }; reactUntil = now + 900; burst('!?', [255, 43, 214], 3, W * 0.78, 50) }
      else if (ev === 'new') { react = { frame: 'talk', hop: true }; reactUntil = now + 700; burst('!', [240, 162, 2], 2, W * 0.78, 50) }
      if (still) draw(now)
    }
    petBus.add(onEvent)
    function draw(now) {
      const t = now / 1000
      const loop = SCHEDULE[moodRef.current] || SCHEDULE.calm
      if (!still && now - stepAt > loop[step % loop.length][1]) { step = (step + 1) % loop.length; stepAt = now }
      const reacting = react && now < reactUntil
      const frame = reacting ? react.frame : still ? 'idle' : loop[step % loop.length][0]
      const rows = PET_FRAMES[frame]
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
      g.clearRect(0, 0, W, H)
      // shell glow tinted by mood
      const glow = moodRef.current === 'worried' ? '240,162,2' : moodRef.current === 'happy' ? '89,255,184' : '0,245,255'
      const grd = g.createRadialGradient(W / 2, H * 0.45, 20, W / 2, H * 0.45, 170)
      grd.addColorStop(0, `rgba(${glow},${0.07 + (still ? 0 : 0.03 * Math.sin(t * 1.3))})`); grd.addColorStop(1, 'rgba(0,0,0,0)')
      g.fillStyle = grd; g.fillRect(0, 0, W, H)
      // body motion: breathe-bob, hop on joy, shake on fault
      let dx = 0, dy = still ? 0 : Math.sin(t * 2.1) * 1.6
      if (reacting && react.hop) { const k = 1 - (reactUntil - now) / 1500; dy -= Math.abs(Math.sin(k * Math.PI * 3)) * 9 * (1 - k) }
      if (reacting && react.shake) dx = Math.sin(now / 18) * 3.5
      g.font = `${size}px ui-monospace,Menlo,monospace`
      g.textBaseline = 'top'
      // flowing rainbow: hue by diagonal position + time; dense ink saturated, light ink pastel
      const buckets = new Map()
      const speed = moodRef.current === 'worried' ? 0.04 : 0.07
      for (let r = 0; r < rows.length; r++) {
        const line = rows[r]
        const wob = still ? 0 : Math.sin(t * 2.4 + r * 0.35) * 0.35
        for (let c = 0; c < line.length; c++) {
          const ch = line[c]
          if (ch === ' ') continue
          const u = (c * 0.006 + r * 0.012) - (still ? 0 : t * speed)
          const q = Math.round(((u % 2) + 2) % 2 * 18) % 36
          const dense = DENSE.has(ch) ? 1 : 0
          const key = q * 2 + dense
          let arr = buckets.get(key)
          if (!arr) buckets.set(key, (arr = []))
          arr.push(ch, ox + c * cw + dx + wob, oy + r * lh + dy)
        }
      }
      const tint = reacting && react.tint
      for (const [key, arr] of buckets) {
        let [R, G, B] = rampAt(RAMPS[moodRef.current] || RAMPS.calm, (key >> 1) / 18)
        if (!(key & 1)) { R = R + (255 - R) * 0.3; G = G + (255 - G) * 0.3; B = B + (255 - B) * 0.3 }
        if (tint) { R = (R + tint[0]) / 2; G = (G + tint[1]) / 2; B = (B + tint[2]) / 2 }
        g.fillStyle = `rgba(${R | 0},${G | 0},${B | 0},${key & 1 ? 0.96 : 0.62})`
        for (let i = 0; i < arr.length; i += 3) g.fillText(arr[i], arr[i + 1], arr[i + 2])
      }
      // mood props: sparkles when happy (nothing is ever drawn over her ASCII)
      if (!still && moodRef.current === 'happy' && Math.random() < 0.05) burst('*+.', rampAt(RAMPS.happy, Math.random()).map(v => v | 0), 1, 30 + Math.random() * (W - 60), 40 + Math.random() * 200)
      // particles
      g.font = '13px ui-monospace,Menlo,monospace'
      for (let i = parts.length - 1; i >= 0; i--) {
        const p = parts[i]
        p.life -= 1 / 30; p.x += p.vx / 30; p.y += p.vy / 30; p.vy += 26 / 30
        if (p.life <= 0) { parts.splice(i, 1); continue }
        g.fillStyle = `rgba(${p.rgb[0]},${p.rgb[1]},${p.rgb[2]},${Math.min(1, p.life)})`
        g.fillText(p.ch, p.x, p.y)
      }
      // CRT: scan sweep over the pet
      if (!still) {
        const sy = (t * 52) % (H + 20) - 10
        g.fillStyle = `rgba(${glow},.05)`; g.fillRect(0, sy - 18, W, 18)
        g.fillStyle = `rgba(${glow},.45)`; g.fillRect(0, sy, W, 1)
      }
    }
    let last = 0
    const loop = now => {
      if (!alive) return
      raf = requestAnimationFrame(loop)
      if (document.hidden || now - last < 33) return // ~30 fps
      last = now
      draw(now)
    }
    draw(performance.now())
    if (!still) raf = requestAnimationFrame(loop)
    return () => { alive = false; cancelAnimationFrame(raf); petBus.delete(onEvent) }
  }, [fx])
  useEffect(() => { if (fx === false || reducedMotion()) petEmit('redraw') }, [mood])
  const hearts = mood === 'happy' ? 4 : mood === 'calm' ? 3 : mood === 'scan' ? 2 : 1
  return jsxs('div', {
    className: 'ub-pet',
    title: 'Pat her',
    onClick: () => { sfx.boot(); sfx.play('ok'); petEmit('pat') },
    children: [
      jsx('canvas', { ref, 'aria-label': `Nous mascot, mood ${MOOD_LABEL[mood]}` }),
      jsxs('div', {
        className: 'bar',
        children: [
          jsxs('span', { children: ['NOUS // ', jsx('b', { style: { color: mood === 'worried' ? '#f0a202' : mood === 'happy' ? '#45b280' : '#00f5ff' }, children: MOOD_LABEL[mood] })] }),
          jsx('span', { className: 'hearts', children: '♥'.repeat(hearts) + '♡'.repeat(4 - hearts) }),
          jsxs('span', { children: ['XP ', jsx('b', { style: { color: '#f0a202' }, children: pad(xp) })] })
        ]
      })
    ]
  })
}
function Ticker({ items }) {
  if (!items.length) return null
  const runs = items.map(i =>
    jsxs('span', {
      children: [jsx('b', { children: '◆ ' }), jsx('em', { children: i.source === 'kanban' ? i.task_id : (i.owner || i.profile || 'CHAT').toUpperCase() }), ` · ${String(i.title || '').toUpperCase()} · ${ago(i.since)}`]
    }, `${i.id}`)
  )
  const chars = items.reduce((n, i) => n + String(i.title || '').length + 24, 0)
  const dur = Math.max(12, (chars * 8.6 + items.length * 44) / 24) // X55 ticker: 24 px/s
  return jsx('div', {
    className: 'ub-ticker',
    'aria-hidden': true,
    children: jsxs('div', { className: 'run', style: { animationDuration: `${dur}s` }, children: [...runs, ...runs.map(r => ({ ...r, key: `${r.key}~` }))] })
  })
}
function FxToggle() {
  const on = useValue($fxOn)
  return button({
    kind: on ? '' : 'ghost',
    'aria-pressed': on,
    title: 'Motion and glitch effects (photosensitivity)',
    onClick: () => { setFx(!on); sfx.play('move') },
    children: on ? 'FX ON' : 'FX OFF'
  })
}
function SfxToggle() {
  const on = useValue($sfxOn)
  return button({
    kind: on ? 'ub-sfx' : 'ub-sfx off',
    'aria-pressed': on,
    title: 'Sound effects',
    onClick: () => {
      const next = !on
      setSfx(next)
      if (next) { sfx.boot(); setTimeout(() => sfx.play('ok'), 30) }
    },
    children: [jsxs('span', { className: 'bars', children: [jsx('i', {}), jsx('i', {}), jsx('i', {})] }), on ? 'SFX ON' : 'SFX OFF']
  })
}

function ago(sec) {
  if (!sec) return ''
  const s = Math.max(0, Date.now() / 1000 - sec)
  if (s < 90) return 'JUST NOW'
  if (s < 5400) return `${Math.round(s / 60)} MIN`
  if (s < 129600) return `${Math.round(s / 3600)} H`
  return `${Math.round(s / 86400)} D`
}
const ageClass = sec => {
  const h = (Date.now() / 1000 - (sec || 0)) / 3600
  return h > 72 ? 'ub-age stale' : h > 24 ? 'ub-age warn' : 'ub-age'
}
const pad = n => String(n).padStart(2, '0')

const button = ({ kind = '', keycap, children, ...rest }) =>
  jsxs('button', {
    type: 'button',
    className: `ub-btn ${kind}`,
    ...rest,
    children: [keycap ? jsx('span', { className: 'ub-key', children: keycap }) : null, children]
  })

const ts = value => {
  if (!value) return ''
  try { return new Date(value * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return '' }
}
const bytes = n => {
  if (!n) return '0 B'
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n > 1024) return `${Math.round(n / 1024)} KB`
  return `${n} B`
}

const seedDraft = (item, data) => {
  const d = data || {}
  const parts = [
    'Help me decide about this Kanban card. Do not change anything without asking me first.',
    '',
    `Board: ${item.board}`,
    `Card: ${item.task_id} — ${item.title}`
  ]
  if (item.ask) parts.push(`Blocked because: ${item.ask}`)
  if (d.body) parts.push('', 'Description:', d.body)
  const comments = (d.comments || []).slice(-5)
  if (comments.length) parts.push('', 'Recent comments:', ...comments.map(c => `- ${c.author}: ${c.body}`))
  parts.push('', 'My question: ')
  return parts.join('\n')
}

function CardDetail({ data, loading, error }) {
  if (loading && !data) return jsx('div', { className: 'ub-note', children: 'Loading card context…' })
  if (error && !data) return jsx('div', { className: 'ub-fault', children: `CONTEXT FAULT // ${error}` })
  if (!data) return null
  const block = data.block || {}
  const ws = data.workspace || {}
  const graph = data.graph || {}
  const run = data.last_run
  const rows = [
    ['STATUS', data.status],
    ['PRIORITY', data.priority != null ? String(data.priority) : ''],
    ['BLOCK KIND', block.kind],
    ['RECURRENCES', block.recurrences != null ? String(block.recurrences) : ''],
    ['ASSIGNEE', data.assignee],
    ['CREATED BY', data.created_by],
    ['SESSION', data.session_id],
    ['CREATED', ts(data.created_at)],
    ['WORKSPACE', ws.kind],
    ['PATH', ws.path],
    ['BRANCH', ws.branch],
    ['PROJECT', ws.project_id],
    ['PR', data.pr ? (data.pr.reference || data.pr.classification || '') : '']
  ].filter(([, value]) => value !== null && value !== undefined && value !== '')
  return jsxs('div', {
    className: 'ub-panel',
    children: [
      block.reason ? jsxs('div', {
        className: 'ub-block',
        children: [
          jsx('b', { children: `LATEST BLOCK${block.kind ? ` · ${String(block.kind).toUpperCase()}` : ''}${block.recurrences > 1 ? ` · LOOP ${block.recurrences}` : ''}` }),
          block.reason
        ]
      }) : null,
      error ? jsx('div', { className: 'ub-fault', children: `CONTEXT REFRESH FAULT // ${error}` }) : null,
      data.body ? jsxs('div', {
        children: [jsx('div', { className: 'ub-subhead', children: 'DESCRIPTION' }), jsx('p', { className: 'ub-body', children: data.body })]
      }) : null,
      rows.length ? jsx('dl', {
        className: 'ub-kvs',
        children: rows.map(([key, value]) => jsxs('div', {
          style: { display: 'contents' },
          children: [jsx('dt', { children: key }), jsx('dd', { children: String(value) })]
        }, key))
      }) : null,
      (graph.parents?.length || graph.children?.length) ? jsxs('div', {
        children: [
          jsx('div', { className: 'ub-subhead', children: 'GRAPH' }),
          jsx('div', {
            className: 'ub-note',
            children: [
              graph.parents?.length ? `Parents: ${graph.parents.map(x => `${x.id} ${x.title} (${x.status})`).join(' · ')}` : '',
              graph.parents?.length && graph.children?.length ? '\n' : '',
              graph.children?.length ? `Children: ${graph.children.map(x => `${x.id} ${x.title} (${x.status})`).join(' · ')}` : ''
            ].join('')
          })
        ]
      }) : null,
      run ? jsxs('div', {
        children: [
          jsx('div', { className: 'ub-subhead', children: `LAST RUN · ${String(run.outcome || run.status || '').toUpperCase()}` }),
          jsx('div', { className: 'ub-note', children: [run.summary, run.error].filter(Boolean).join('\n') || 'No summary.' })
        ]
      }) : null,
      data.attachments?.length ? jsxs('div', {
        children: [
          jsx('div', { className: 'ub-subhead', children: `ATTACHMENTS · ${data.attachments.length}` }),
          jsx('div', { className: 'ub-note', children: data.attachments.map(a => `${a.filename} (${bytes(a.size)})`).join('\n') })
        ]
      }) : null,
      data.comments?.length ? jsxs('div', {
        children: [
          jsx('div', { className: 'ub-subhead', children: `COMMENTS · ${data.comments.length}` }),
          ...data.comments.slice(-12).map(c => jsxs('div', {
            className: 'ub-cmt',
            children: [
              jsxs('div', { className: 'h', children: [jsx('em', { children: c.author }), ` · ${ts(c.created_at)}`] }),
              jsx('p', { children: c.body })
            ]
          }, c.id))
        ]
      }) : null,
      data.events?.length ? jsxs('div', {
        children: [
          jsx('div', { className: 'ub-subhead', children: 'RECENT EVENTS' }),
          ...data.events.slice(-8).map(e => jsxs('div', {
            className: 'ub-ev',
            children: [
              jsx('em', { children: e.kind }),
              jsx('span', { children: [ts(e.created_at), e.payload?.reason ? ` · ${String(e.payload.reason).slice(0, 180)}` : ''].join('') })
            ]
          }, e.id))
        ]
      }) : null
    ]
  })
}

function Item({ ctx, item, n, refresh, askEnabled }) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [done, setDone] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [askOpen, setAskOpen] = useState(false)
  const [askMsgs, setAskMsgs] = useState([])
  const [askInput, setAskInput] = useState('')
  const [askBusy, setAskBusy] = useState(false)
  const [askError, setAskError] = useState('')
  const askEnd = useRef(null)
  const isKanban = item.source === 'kanban'
  const triage = isKanban && item.status === 'triage'

  useEffect(() => {
    if (!detailOpen) return
    if (detail && detail.__version === item.version) return
    let alive = true
    setDetailLoading(true)
    setDetailError('')
    ctx.rest(`/card?id=${encodeURIComponent(item.id)}`)
      .then(d => { if (alive) setDetail({ ...d, __version: item.version }) })
      .catch(e => { if (alive) setDetailError(String(e?.message || e)) })
      .finally(() => setDetailLoading(false))
    return () => { alive = false }
  }, [item.version, detailOpen])
  useEffect(() => {
    if (askEnd.current) askEnd.current.scrollIntoView({ block: 'nearest' })
  }, [askMsgs, askBusy])

  const loadDetail = async () => {
    if (detail && detail.__version === item.version) return detail
    if (detailLoading) return null
    setDetailLoading(true)
    setDetailError('')
    try {
      const d = await ctx.rest(`/card?id=${encodeURIComponent(item.id)}`)
      const full = { ...d, __version: item.version }
      setDetail(full)
      return full
    } catch (e) {
      setDetailError(String(e?.message || e))
      return null
    } finally {
      setDetailLoading(false)
    }
  }
  const toggleDetail = () => setDetailOpen(!detailOpen)
  const toggleAsk = () => {
    const next = !askOpen
    setAskOpen(next)
    if (next && !detailOpen) loadDetail()
  }

  const run = async (fn, okFor, cue = 'ok') => {
    setBusy(true)
    try {
      const r = await fn()
      sfx.play(cue)
      if (okFor) {
        petEmit('joy')
        setDone(r && r.released ? 'up' : 'kept')
        host.notify({ kind: 'success', message: okFor(r) })
        await new Promise(res => setTimeout(res, reducedMotion() ? 0 : 650))
      }
      await refresh()
      return r
    } catch (e) {
      sfx.play('err')
      petEmit('err')
      host.notifyError(e, 'UNBL0CK: that did not go through')
      return null
    } finally {
      setBusy(false)
    }
  }
  const send = release =>
    run(
      () => ctx.rest('/kanban/answer', {
        method: 'POST',
        body: { board: item.board, task_id: item.task_id, text: answer, release }
      }),
      r => (triage
        ? `Answered ${item.task_id} (triage)`
        : r && r.released
          ? `Answered and unblocked ${item.task_id}`
          : `Answered ${item.task_id} (kept blocked)`)
    )
  const sendPrimary = () => send(!triage)
  const hide = async () => {
    const r = await run(() => ctx.rest('/hide', { method: 'POST', body: { id: item.id, version: item.version } }), null, 'warn')
    if (r) $undo.set({ id: item.id, title: item.title, at: Date.now() })
  }
  const ready = !busy && answer.trim().length > 0
  const saysKeep = !triage && KEEP_BLOCKED.test(answer)

  const askSend = async (retry = false) => {
    const q = askInput.trim()
    if (askBusy || (!retry && !q)) return
    const next = retry ? askMsgs : [...askMsgs, { role: 'user', content: q }]
    if (!next.length || next[next.length - 1].role !== 'user') return
    setAskMsgs(next)
    if (!retry) setAskInput('')
    setAskBusy(true)
    setAskError('')
    try {
      let r = await ctx.rest('/card/ask', { method: 'POST', body: { id: item.id, messages: next } })
      const t0 = Date.now()   // the model can outlast the desktop's 30 s request limit: poll the background job
      while (r && r.pending && r.job) {
        if (Date.now() - t0 > 150000) throw new Error('The model took too long (over 150 s)')
        await new Promise(res => setTimeout(res, 1500))
        r = await ctx.rest('/card/ask/' + encodeURIComponent(r.job))
      }
      const reply = String(r?.reply || '').trim()
      if (!reply) throw new Error('The model returned an empty answer')
      setAskMsgs([...next, { role: 'assistant', content: reply }])
      sfx.play('ok')
    } catch (e) {
      sfx.play('err')
      const msg = String(e?.message || e)
      // An unknown job means the gateway restarted mid-answer: the work is gone, not slow.
      setAskError(/unknown or expired ask job/.test(msg) ? 'The gateway restarted while the model was answering. Retry to ask again.' : msg)
    } finally {
      setAskBusy(false)
    }
  }
  const lastAssistant = askMsgs.reduce((index, m, i) => (m.role === 'assistant' ? i : index), -1)
  const lastSuggestion = lastAssistant >= 0 ? askMsgs[lastAssistant].content : ''
  const useAsAnswer = () => {
    if (!lastSuggestion) return
    setAnswer(prev => (prev.trim() ? `${prev.trim()}\n\n${lastSuggestion}` : lastSuggestion))
    sfx.play('move')
  }
  const discussInChat = async () => {
    try {
      const d = detail || await loadDetail()
      const draft = seedDraft(item, d)
      host.newChat(null)
      await new Promise(res => setTimeout(res, reducedMotion() ? 0 : 200))
      let ok = false
      if (host.composer?.setDraft) {
        ok = await host.composer.setDraft('new', draft)
        if (!ok) ok = await host.composer.setDraft(null, draft)
      }
      if (!ok) throw new Error('the new chat did not accept the draft')
      sfx.play('move')
    } catch (e) {
      host.notifyError(e, 'UNBL0CK: could not open chat')
    }
  }

  const meta = isKanban
    ? jsxs('div', {
        className: 'ub-meta',
        children: [
          'SOURCE // ', jsx('em', { children: item.task_id }), ` · ${item.board}`,
          item.owner ? ` · FROM ${item.owner}` : '',
          item.priority != null ? ` · P${item.priority}` : '',
          item.block_kind ? ` · ${String(item.block_kind).toUpperCase()}` : '',
          item.block_recurrences > 1 ? ` · LOOP ${item.block_recurrences}` : '',
          triage ? ' · TRIAGE' : ''
        ]
      })
    : jsxs('div', {
        className: 'ub-meta',
        children: ['BOT CHAT // ', jsx('em', { children: item.owner || item.profile }), item.profile && item.owner !== item.profile ? ` · ${item.profile}` : '']
      })

  const answerButtons = triage
    ? [button({ kind: 'primary', keycap: '⏎', disabled: !ready, onClick: sendPrimary, title: '⌘/Ctrl + Enter · triage cards stay in triage', children: 'Answer (stays in triage)' })]
    : [
        button({ kind: 'primary', keycap: '⏎', disabled: !ready, onClick: sendPrimary, title: saysKeep ? 'Your answer says to keep it blocked' : '⌘/Ctrl + Enter', children: 'Answer + unbl0ck' }),
        button({ disabled: !ready, onClick: () => send(false), children: 'Answer, keep blocked' })
      ]
  const tools = [
    button({ kind: 'ghost', disabled: busy, onClick: toggleDetail, 'aria-expanded': detailOpen, children: detailOpen ? 'Hide context' : 'Context' }),
    askEnabled ? button({ kind: 'ghost', disabled: busy, onClick: toggleAsk, 'aria-expanded': askOpen, children: askOpen ? 'Hide ask' : 'Ask about this card' }) : null,
    button({ kind: 'ghost', disabled: busy, onClick: discussInChat, children: 'Discuss in chat' }),
    button({ kind: 'ghost', disabled: busy, onClick: () => host.navigate('/kanban'), children: 'Open board' }),
    button({ kind: 'ghost', disabled: busy, onClick: hide, children: 'Not for me' })
  ].filter(Boolean)

  const actions = isKanban
    ? [
        jsx('textarea', {
          className: answer ? 'ub-ta has' : 'ub-ta',
          value: answer,
          rows: 2,
          disabled: busy,
          placeholder: 'Your answer. It is posted on the card as you.',
          'aria-label': `Answer for ${item.task_id}`,
          onChange: e => setAnswer(e.target.value),
          onKeyDown: e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && ready) { e.preventDefault(); saysKeep ? send(false) : sendPrimary() }
          }
        }),
        saysKeep ? jsx('div', { className: 'ub-hint', role: 'status', children: 'Your answer says to keep it blocked. Did you mean ANSWER, KEEP BLOCKED?' }) : null,
        jsxs('div', { className: 'ub-btns', children: answerButtons }),
        jsxs('div', { className: 'ub-btns tools', children: tools })
      ]
    : [
        jsxs('div', {
          className: 'ub-btns',
          children: [
            button({ kind: 'primary', keycap: '→', disabled: busy, onClick: () => host.openSession(item.session_id, { profile: item.profile }), children: 'Open chat + reply' }),
            button({ kind: 'ghost', disabled: busy, onClick: hide, children: 'Not for me' })
          ]
        })
      ]

  const detailNode = detailOpen
    ? jsx(CardDetail, { data: detail, loading: detailLoading, error: detailError })
    : null
  const askNode = askOpen && askEnabled
    ? jsxs('div', {
        className: 'ub-askwrap',
        children: [
          jsxs('div', {
            className: 'hd',
            children: [jsx('span', { children: 'ASK ABOUT THIS CARD' }), jsx('span', { children: 'READ-ONLY · NEVER ACTS' })]
          }),
          jsxs('div', {
            className: 'ub-msgs',
            children: [
              askMsgs.length
                ? askMsgs.map((m, i) => jsxs('div', {
                    className: `ub-msg ${m.role}`,
                    children: [
                      jsx('div', { className: 'who', children: m.role === 'user' ? 'YOU' : 'UNBL0CK' }),
                      jsx('p', { children: m.content }),
                      m.role === 'assistant' && i === lastAssistant
                        ? jsx('div', { className: 'use', children: button({ kind: 'ghost', onClick: useAsAnswer, children: 'Use as answer' }) })
                        : null
                    ]
                  }, i))
                : jsx('div', { className: 'ub-note', children: 'Ask what the card means, what is being asked, or what to pick. The model sees this card only; it cannot change anything.' }),
              askBusy ? jsx('div', { className: 'ub-thinking', children: 'Thinking' }) : null,
              jsx('div', { ref: askEnd })
            ]
          }),
          askError
            ? jsxs('div', {
                style: { padding: '0 12px 8px', display: 'flex', gap: 12, alignItems: 'baseline', flexWrap: 'wrap' },
                children: [
                  jsx('div', { className: 'ub-fault', style: { flex: '1 1 240px' }, children: `ASK FAULT // ${askError}` }),
                  button({ kind: 'ghost', disabled: askBusy, onClick: () => askSend(true), children: 'Retry' })
                ]
              })
            : null,
          jsxs('div', {
            className: 'ub-chatbar',
            children: [
              jsx('textarea', {
                value: askInput,
                rows: 1,
                disabled: askBusy,
                placeholder: 'Ask about this card… (Enter sends, Shift+Enter adds a line)',
                onChange: e => setAskInput(e.target.value),
                onKeyDown: e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); askSend() } }
              }),
              button({ kind: 'primary', disabled: askBusy || !askInput.trim(), onClick: () => askSend(), children: 'Send' })
            ]
          })
        ]
      })
    : null

  return jsx('section', {
    className: `ub-card ${isKanban ? 'kanban' : 'chat'}${done ? ' done' : ''}${done === 'kept' ? ' kept' : ''}`,
    style: { '--i': n - 1 },
    tabIndex: -1,
    onPointerEnter: () => sfx.play('move'),
    onKeyDown: e => {
      if (e.key !== 'Escape') return
      if (askOpen) setAskOpen(false)
      else if (detailOpen) setDetailOpen(false)
      else if (open) setOpen(false)
      else return
      e.stopPropagation()
      // The focused control is about to unmount: move focus to the card now, so the next
      // Escape lands here even when pressed before the next frame.
      const card = e.currentTarget
      if (e.target !== card && e.target.closest('.ub-askwrap, .ub-detail, .ub-full')) card.focus({ preventScroll: true })
      requestAnimationFrame(() => { if (!card.contains(document.activeElement)) card.focus({ preventScroll: true }) })
    },
    onFocus: e => { if (e.target === e.currentTarget.querySelector('textarea, button')) sfx.play('move') },
    children: jsxs('div', {
      className: 'in',
      children: [
        jsx('div', { className: 'ub-idx', children: pad(n) }),
        jsxs('div', {
          className: 'ub-top',
          children: [
            jsx('h3', { className: 'ub-h', children: item.title }),
            jsx('span', { className: ageClass(item.since), children: ago(item.since) })
          ]
        }),
        meta,
        ...(item.bluf
          ? [
              jsx('p', { className: 'ub-bluf', children: item.bluf }),
              item.bluf_why ? jsxs('div', { className: 'ub-why', children: [jsx('b', { children: 'WHY' }), item.bluf_why] }) : null,
              open ? jsx('p', { className: 'ub-ask open', children: item.ask }) : null,
              jsx('button', { type: 'button', className: 'ub-full', 'aria-expanded': open, onClick: () => setOpen(!open), children: open ? '− HIDE FULL TEXT' : '+ FULL TEXT' })
            ]
          : [
              item.bluf_pending ? jsx('div', { className: 'ub-pending', children: '◇ SUMMARIZING' }) : null,
              jsx('p', { className: open ? 'ub-ask open' : 'ub-ask', title: open ? undefined : 'Click to read all', onClick: () => setOpen(true), children: item.ask }),
              item.why && item.why !== item.ask ? jsxs('div', { className: 'ub-why', children: [jsx('b', { children: 'WHY' }), item.why] }) : null
            ]),
        jsx('div', { className: 'ub-act', children: actions }),
        detailNode || askNode ? jsxs('div', { className: 'ub-detail', children: [detailNode, askNode] }) : null
      ]
    })
  })
}

function Page({ ctx, refresh }) {
  const q = useValue($queue)
  useEffect(() => { if (Date.now() - (q.at || 0) > 10000) refresh() }, [])   // opening the page shows fresh data
  const fx = useValue($fxOn)
  const undo = useValue($undo)
  useEffect(() => {
    if (!undo) return
    const t = setTimeout(() => $undo.set(null), Math.max(0, UNDO_MS - (Date.now() - undo.at)))
    return () => clearTimeout(t)
  }, [undo])
  const undoHide = async () => {
    const u = undo
    $undo.set(null)
    try {
      await ctx.rest('/unhide', { method: 'POST', body: { id: u.id } })
      sfx.play('move')
      await refresh()
    } catch (e) {
      host.notifyError(e, 'UNBL0CK: could not undo')
    }
  }
  const kanban = q.items.filter(i => i.source === 'kanban')
  const chats = q.items.filter(i => i.source === 'chat')
  const oldest = q.items.reduce((m, i) => (i.since && (!m || i.since < m) ? i.since : m), 0)
  const oldestH = oldest ? (Date.now() / 1000 - oldest) / 3600 : 0
  let n = 0
  const group = (label, items) =>
    items.length
      ? [
          jsxs('div', {
            className: 'ub-sec',
            children: [jsx('span', { className: 'ub-eyebrow', children: `// ${label} · ${pad(items.length)}` }), jsx('hr', {})]
          }),
          jsx('div', { className: 'ub-list', children: items.map(item => jsx(Item, { ctx, item, n: ++n, refresh, askEnabled: q.ask_enabled !== false }, item.id)) })
        ]
      : []

  const tile = (label, value, tone = '') => jsxs('div', { className: `ub-tile ${tone}`, children: [jsx('b', { children: value }), jsx('span', { children: label })] })

  return jsxs('div', {
    className: fx ? 'ub' : 'ub fx-off',
    onPointerDown: () => sfx.boot(),
    onKeyDown: () => sfx.boot(),
    children: [
      jsx(Field, { fx }),
      jsx('div', { className: 'ub-fx' }),
      jsx('div', { className: 'ub-grain', style: { backgroundImage: grainUrl() } }),
      jsxs('div', { className: 'ub-scroll', children: [
      jsx('i', { className: 'ub-corner tl' }),
      jsx('i', { className: 'ub-corner tr' }),
      jsxs('div', {
        className: 'ub-wrap',
        children: [
          jsxs('header', {
            className: 'ub-head',
            style: { display: 'block' },
            children: [
              jsxs('div', {
                className: 'ub-hero',
                children: [
                  jsxs('div', {
                    style: { position: 'relative', minWidth: 0 },
                    children: [
                      jsx(Watermark, {}),
                      jsx('div', { className: 'ub-ticks', children: ['#00f5ff', '#ff2bd6', '#f0a202', '#45b280'].map(c => jsx('i', { style: { background: c } }, c)) }),
                      jsxs('div', { className: 'ub-eyebrow', children: ['// NEEDS.YOU  /  LIVE QUEUE', q.at ? jsx('span', { style: { whiteSpace: 'nowrap' }, children: ` · SYNC ${new Date(q.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` }) : null] }),
                      jsx(Banner, {}),
                      jsxs('div', {
                        className: 'ub-sub',
                        children: [
                          q.loading
                            ? 'Scanning the boards and Bot Chats'
                            : q.fault
                              ? 'Cannot reach the queue; showing the last known list.'
                              : (q.errors || []).length
                                ? 'A source failed to load; the list may be incomplete.'
                                : q.count
                                ? 'Everything that is waiting on your word.'
                                : 'Nothing is blocked on you.',
                          jsx('i', { className: 'ub-cursor' })
                        ]
                      })
                    ]
                  }),
                  jsx(Pet, { count: q.count, oldestH, loading: q.loading, fx })
                ]
              }),
              jsxs('div', {
                className: 'ub-strip',
                children: [
                  tile('KANBAN', q.loading ? '··' : pad(kanban.length)),
                  tile('CHATS', q.loading ? '··' : pad(chats.length)),
                  tile('OLDEST', oldest ? ago(oldest) : '—', oldestH > 24 ? 'warn' : ''),
                  jsx('div', { style: { flex: 1 } }),
                  jsx('div', { style: { alignSelf: 'center' }, children: jsx(FxToggle, {}) }),
                  jsx('div', { style: { alignSelf: 'center' }, children: jsx(SfxToggle, {}) }),
                  jsx('div', { style: { alignSelf: 'center' }, children: button({ onClick: () => { sfx.play('move'); refresh() }, children: 'Refresh' }) })
                ]
              }),
              jsx(Ticker, { items: q.items })
            ]
          }),
          q.fault ? jsxs('div', { className: 'ub-err', role: 'alert', children: [jsx('b', { children: 'LINK FAULT //' }), q.fault] }) : null,
          ...(q.errors || []).map(err => jsxs('div', { className: 'ub-err', role: 'alert', children: [jsx('b', { children: 'SOURCE FAULT //' }), err] }, err)),
          undo
            ? jsxs('div', {
                className: 'ub-undo',
                role: 'status',
                children: [
                  jsxs('span', { children: [jsx('b', { children: 'HIDDEN //' }), undo.title] }),
                  button({ kind: 'ghost', onClick: undoHide, children: 'Undo' })
                ]
              }, undo.at)
            : null,
          kanban.length ? jsx('div', { className: 'ub-hazard' }) : null,
          ...group('KANBAN.CARDS', kanban),
          ...group('BOT.CHATS', chats),
          !q.loading && !q.count && !q.fault && !(q.errors || []).length
            ? jsxs('div', { className: 'ub-clear', children: [jsx('b', { children: '● ALL CLEAR' }), jsx('span', { children: 'No card or chat is waiting on you.' })] })
            : null
        ]
      })
      ] })
    ]
  })
}

function Chip() {
  const q = useValue($queue)
  if (!q.count) return null
  return jsx('button', {
    type: 'button',
    className: q.fresh ? 'ub-chip new' : 'ub-chip',
    onClick: () => host.navigate(PATH),
    title: 'Open UNBL0CK',
    children: `◆ ${q.count} ${q.count === 1 ? 'NEEDS' : 'NEED'} YOU`
  })
}

export default {
  id: 'unbl0ck',
  name: 'UNBL0CK',
  register(ctx) {
    const style = document.createElement('style')
    style.dataset.plugin = 'unbl0ck'
    style.textContent = CSS
    document.head.appendChild(style)

    let lastKey = ''
    const paintBadges = items => {
      const ids = new Map(items.filter(i => i.source === 'kanban').map(i => [i.task_id, i.bluf || i.why]))
      const key = [...ids].map(kv => kv.join('=')).sort().join('|')
      if (key === lastKey) return
      lastKey = key
      ctx.register({
        id: 'needs-you',
        area: KANBAN_CARD_AREAS.badges,
        data: {
          decorate: card =>
            card.status === 'blocked' && ids.has(card.id) ? { label: 'Needs you', tone: 'red', tooltip: ids.get(card.id) } : null
        }
      })
    }

    let seen = null
    let follow = 0
    let lastErr = ''
    let inflight = null
    const refresh = () => (inflight ||= load().finally(() => { inflight = null }))
    const load = async () => {
      clearTimeout(follow)
      try {
        const q = await ctx.rest('/queue')
        // Summaries land a few seconds after a new item appears: look again soon instead of
        // making the user wait a whole poll interval.
        if ((q.items || []).some(i => i.bluf_pending)) follow = setTimeout(refresh, 6000)
        const ids = new Set((q.items || []).map(i => i.id))
        const fresh = seen !== null && [...ids].some(id => !seen.has(id))
        seen = ids
        if (fresh) { sfx.play('warn'); petEmit('new') }
        const errKey = (q.errors || []).join('|')
        if (errKey && errKey !== lastErr) { sfx.play('err'); petEmit('err') }   // once per new fault, not every poll
        lastErr = errKey
        $queue.set({ ...q, fresh, fault: '', loading: false, at: Date.now() })
        paintBadges(q.items || [])
      } catch (e) {
        // Keep the last good queue: a transient fetch fault must not wipe the list
        // or tell the user that nothing is blocked.
        const prev = $queue.get() || {}
        if (!prev.fault) { sfx.play('err'); petEmit('err') }
        $queue.set({ ...prev, fault: String(e?.message || e), loading: false })
      }
    }

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, { ctx, refresh }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'UNBL0CK', codicon: 'debug-continue' } },
      { id: 'count', area: STATUSBAR_AREAS.right, order: 115, render: () => jsx(Chip, {}) }
    ])

    refresh()
    const timer = setInterval(() => { if (!document.hidden) refresh() }, POLL_MS)
    // Coming back to the window: refresh at once rather than showing a list up to 45 s old.
    const onVisible = () => { if (!document.hidden && Date.now() - ($queue.get().at || 0) > 10000) refresh() }
    document.addEventListener('visibilitychange', onVisible)
    ctx.onDispose(() => {
      clearInterval(timer)
      clearTimeout(follow)
      document.removeEventListener('visibilitychange', onVisible)
      sfx.close()
      style.remove()
    })
  }
}
