// Linda browser client. local mode (server.local_ears=true): the server listens and speaks, the browser displays.
// classic mode: Chrome speech recognition + wake word + audio playback in the browser.
(() => {
"use strict";
const $ = id => document.getElementById(id);
const LANGS = { en: "en-US", ru: "ru-RU", uz: "uz-UZ" };
const STOP = /^[^\p{L}\p{N}]*(stop|be quiet|shut up|стоп|хватит|замолчи|to'?xta|jim)[^\p{L}\p{N}]*$/iu;
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const COLORS = { notes: "#8fa8ff", papers: "#ffa98a", projects: "#7affc4", journal: "#d9a8ff" };

let ws, cfg = { window: 20, wake_pattern: "(linda)" }, lang = localStorage.getItem("lang") || "en";
let wakeRe = null, windowUntil = 0, session = null, speaking = false, queue = [], current = null, tFinal = 0, recentSpeech = [];
let rec = null, recWanted = false, micPaused = false, chess = { active: false, sel: null };
let localEars = false, localMuted = false, speakReply = true, freshTurn = false, memTab = "profile", memData = {};

const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const fill = (id, items, build, empty) => { const ul = $(id); ul.replaceChildren(); if (!items || !items.length) { ul.append(el("li", empty, "empty")); return; } items.forEach(i => ul.append(build(i))); };
const send = o => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
const idleState = () => (Date.now() < windowUntil || session) ? "listening" : "idle";
const setState = s => { $("orb").dataset.state = s; $("status").textContent = s; };

function greet() {
  const h = new Date().getHours();
  $("gh").textContent = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  $("gd").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { $("dot").className = "on"; };
  ws.onclose = () => { $("dot").className = ""; setTimeout(connect, 1500); };
  ws.onmessage = ev => onMsg(JSON.parse(ev.data));
}
const buildWake = p => new RegExp("^[^\\p{L}\\p{N}]*(?:(?:hey|hi|ok|okay|эй|привет)[,\\s]+)?(?:" + p + ")(?![\\p{L}\\p{N}])[,.!?:\\s]*(?<cmd>.*)$", "isu");

function onMsg(m) {
  switch (m.type) {
    case "hello":
      cfg = m; wakeRe = buildWake(m.wake_pattern); localEars = !!m.local_ears;
      if (!localStorage.getItem("lang")) lang = m.default_language;
      $("lang").value = lang;
      if (localEars) { send({ type: "lang", lang }); $("start").classList.add("hidden"); }
      $("mode").textContent = localEars ? "local ears" : "browser ears"; break;
    case "state": setState(m.state); break;
    case "transcript": $("transcript").textContent = m.text; break;
    case "latency_ms": $("lat").textContent = (m.ms / 1000).toFixed(1) + " s"; break;
    case "audio": case "speak_local":
      if (localEars || !speakReply) { showAnswer(m.text || ""); break; }
      queue.push(m); if (!current) playNext(); break;
    case "stop_audio": stopAudio(); break;
    case "turn_done": if (!localEars && !current && !queue.length) setState(idleState()); break;
    case "open_window": if (localEars) break; windowUntil = Date.now() + m.seconds * 1000; if (!speaking) setState("listening"); break;
    case "mic": if (localEars) break; micPaused = !m.enabled; if (!m.enabled) stopRec(); else if (recWanted) startRec(); break;
    case "set_lang": lang = m.lang; localStorage.setItem("lang", lang); $("lang").value = lang; if (!localEars && recWanted) restartRec(); break;
    case "panels": renderPanels(m); break;
    case "chess": renderChess(m); break;
    case "galaxy_hits": m.paths.forEach(p => { G.hits[p] = Date.now(); }); break;
  }
}

// ---- audio out (classic mode)
function playNext() {
  if (!queue.length) { current = null; speaking = false; send({ type: "playback", state: "idle" }); windowUntil = Date.now() + cfg.window * 1000; setState(idleState()); return; }
  const it = queue.shift(); current = it;
  if (!speaking) { speaking = true; send({ type: "playback", state: "playing" }); }
  setState("speaking"); $("answer").textContent = it.text;
  if (tFinal) { const ms = Math.round(performance.now() - tFinal); $("lat").textContent = (ms / 1000).toFixed(1) + " s"; send({ type: "latency", client_ms: ms }); tFinal = 0; }
  const done = () => { if (current === it) { rememberSpeech(it.text); playNext(); } };
  if (it.type === "audio") {
    const a = new Audio("data:" + it.mime + ";base64," + it.b64); it.el = a; a.onended = a.onerror = done; a.play().catch(() => { if (current === it) playNext(); });
  } else { const u = new SpeechSynthesisUtterance(it.text); u.lang = LANGS[it.lang] || "en-US"; u.onend = u.onerror = done; speechSynthesis.speak(u); }
}
function stopAudio() {
  queue = []; const c = current; current = null;
  if (c) { rememberSpeech(c.text); if (c.el) c.el.pause(); }
  speechSynthesis.cancel(); speaking = false; if (!localEars) send({ type: "playback", state: "idle" });
}
const interrupt = () => { stopAudio(); send({ type: "interrupt" }); setState(idleState()); };
function beep() { try { const ac = new AudioContext(), o = ac.createOscillator(), g = ac.createGain(); o.frequency.value = 880; g.gain.value = 0.05; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.08); } catch (e) {} }

// ---- commands, browser speech recognition, wake word
function command(text, typed = false) {
  speakReply = !typed || $("speakToggle").checked; freshTurn = true; setState("thinking");
  $("transcript").textContent = text;
  if (!localEars) { tFinal = speakReply ? performance.now() : 0; windowUntil = Date.now() + cfg.window * 1000; }
  send({ type: "command", text, lang, speak: speakReply });
}
const speechTokens = t => t.toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
function rememberSpeech(text) { recentSpeech.push({ tokens: speechTokens(text), at: Date.now() }); recentSpeech = recentSpeech.filter(x => Date.now() - x.at < 12000).slice(-8); }
function isRecentEcho(text) {
  const heard = speechTokens(text); if (heard.length < 2) return false;
  recentSpeech = recentSpeech.filter(x => Date.now() - x.at < 12000);
  return recentSpeech.some(l => { const w = new Set(l.tokens); return heard.filter(x => w.has(x)).length / heard.length >= 0.65; });
}
function showAnswer(t) { const a = $("answer"); if (freshTurn) { a.textContent = t; freshTurn = false; } else a.textContent += " " + t; }
function onFinal(t) {
  if (!t) return; $("transcript").textContent = t;
  const m = wakeRe && wakeRe.exec(t), rest = m ? m.groups.cmd.trim() : "";
  if (speaking) { if (m || STOP.test(t)) { interrupt(); if (rest) command(rest); else beep(); } return; }
  if (!m && isRecentEcho(t)) return;
  if (m) { if (rest) command(rest); else { beep(); windowUntil = Date.now() + cfg.window * 1000; setState("listening"); } return; }
  if (Date.now() < windowUntil || session) command(t);
}
function onInterim(t) { if (!t) return; $("transcript").textContent = t; if (wakeRe && wakeRe.test(t)) { if (speaking) interrupt(); else if (!current) setState("listening"); } }
function startRec() {
  if (localEars) return;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { $("transcript").textContent = "This browser has no speech recognition. Use Chrome."; return; }
  if (rec) { rec.onend = null; try { rec.abort(); } catch (e) {} }
  rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.lang = LANGS[lang]; rec.maxAlternatives = 1;
  rec.onresult = e => { let interim = ""; for (let i = e.resultIndex; i < e.results.length; i++) { const r = e.results[i], t = r[0].transcript.trim(); if (r.isFinal) onFinal(t); else interim += t + " "; } onInterim(interim.trim()); };
  rec.onend = () => { if (recWanted && !micPaused) setTimeout(() => { try { rec.start(); } catch (e) {} }, 150); };
  rec.onerror = e => { if (e.error === "not-allowed" || e.error === "service-not-allowed") { recWanted = false; $("transcript").textContent = "Microphone blocked: allow it in the address bar, then reload."; } };
  recWanted = true; try { rec.start(); } catch (e) {}
}
function stopRec() { if (rec) { rec.onend = null; try { rec.abort(); } catch (e) {} } }
const restartRec = () => { stopRec(); startRec(); };

let bargeReady = false;
async function setupBarge() {
  if (bargeReady) return; bargeReady = true;
  const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false } });
  const ac = new AudioContext(), an = ac.createAnalyser(); an.fftSize = 1024; ac.createMediaStreamSource(s).connect(an);
  const buf = new Float32Array(1024); let loud = 0;
  setInterval(() => {
    if (!$("barge").checked || !speaking) { loud = 0; return; }
    an.getFloatTimeDomainData(buf); let sum = 0; for (const v of buf) sum += v * v;
    loud = Math.sqrt(sum / buf.length) > 0.06 ? loud + 1 : 0; if (loud >= 4) { loud = 0; interrupt(); }
  }, 80);
}

// ---- panels
function renderPanels(p) {
  session = p.session;
  fill("calendar", p.calendar, e => { const li = el("li"); li.append(el("span", e.time || "today", "t"), document.createTextNode(e.title)); return li; }, "Nothing planned");
  fill("tasks", p.tasks, t => { const li = el("li", t.title); if (t.due) li.append(el("small", t.due)); return li; }, "No open tasks");
  fill("birthdays", p.birthdays, b => { const li = el("li", b.name); li.append(el("small", b.n === 0 ? "today" : b.n === 1 ? "tomorrow" : "in " + b.n + " days")); return li; }, "None in the next 30 days");
  fill("plan", p.plan, i => { const li = el("li", `${i.title || "Free practice"} · ${i.minutes} min`); const why = [].concat(i.why || []).join(" · "); if (why) li.append(el("small", why)); return li; }, "No plan yet");
  const pr = $("practice"); pr.replaceChildren();
  const mins = p.practice.total_minutes, pct = Math.min(100, Math.round(100 * mins / p.goal_minutes));
  pr.append(el("div", mins + " min", "big"), el("small", `of ${p.goal_minutes} min goal`));
  const bar = el("div", "", "bar"), i = el("i"); i.style.width = pct + "%"; bar.append(i); pr.append(bar);
  Object.entries(p.practice.by_instrument || {}).forEach(([k, v]) => pr.append(el("small", `${k}: ${v} min`)));
  const q = p.quiz; $("quizCard").classList.toggle("hidden", !q);
  if (q) {
    $("quizHead").textContent = `Question ${q.i} of ${q.n} · correct so far ${q.ok}`; $("quizQ").textContent = q.question;
    const box = $("quizOpts"); box.replaceChildren();
    q.options.forEach((o, k) => { const b = el("button", `${k + 1}. ${o}`); b.onclick = () => command(String(k + 1)); box.append(b); });
  }
  if (p.memory) memData = p.memory; renderMemory();
  if (!localEars && !speaking && !current) setState(idleState());
}
const renderMemory = () => fill("memList", memData[memTab], i => el("li", i.text, i.done ? "done" : ""), "Nothing saved yet");

// ---- chess
function parseFen(fen) { const out = {}; fen.split(" ")[0].split("/").forEach((row, i) => { let f = 0; for (const ch of row) { if (/\d/.test(ch)) f += +ch; else { out[f + "," + (7 - i)] = ch; f++; } } }); return out; }
function renderChess(m) {
  $("chessWrap").classList.toggle("hidden", !m.fen); $("stage").classList.toggle("chess", !!m.fen); if (!m.fen) return;
  chess.active = m.active; chess.sel = null;
  const pieces = parseFen(m.fen), last = m.last ? [m.last.slice(0, 2), m.last.slice(2, 4)] : [], board = $("board"); board.replaceChildren();
  for (let row = 0; row < 8; row++) for (let col = 0; col < 8; col++) {
    const f = m.flip ? 7 - col : col, r = m.flip ? row : 7 - row, name = "abcdefgh"[f] + (r + 1);
    const sq = el("div", "", "sq " + ((f + r) % 2 ? "l" : "d") + (last.includes(name) ? " last" : ""));
    if (row === 7) sq.dataset.file = "abcdefgh"[f]; if (col === 0) sq.dataset.rank = r + 1;
    const p = pieces[f + "," + r]; if (p) sq.append(el("span", GLYPH[p.toLowerCase()], p === p.toUpperCase() ? "w" : "b"));
    sq.onclick = () => {
      if (!chess.active) return;
      if (!chess.sel) { if (!p) return; chess.sel = name; sq.classList.add("sel"); }
      else if (chess.sel === name) { chess.sel = null; sq.classList.remove("sel"); }
      else { const from = chess.sel; chess.sel = null; command(from + " to " + name); }
    };
    board.append(sq);
  }
  $("chessStatus").textContent = m.status || "";
}

// ---- notes galaxy
let G = { nodes: [], links: [], byId: {}, hits: {} };
async function loadGalaxy() {
  try {
    const d = await (await fetch("/api/galaxy")).json(), kinds = [...new Set(d.nodes.map(n => n.kind))];
    d.nodes.forEach(n => { const a = (kinds.indexOf(n.kind) / Math.max(1, kinds.length)) * 6.283, h = Math.random() * 6.283; n.x = 160 + Math.cos(a) * 70 + Math.cos(h) * 30; n.y = 120 + Math.sin(a) * 50 + Math.sin(h) * 30; n.vx = n.vy = 0; n.cx = 160 + Math.cos(a) * 60; n.cy = 120 + Math.sin(a) * 45; });
    G = { nodes: d.nodes, links: d.links, byId: Object.fromEntries(d.nodes.map(n => [n.id, n])), hits: {} };
  } catch (e) {}
}
function tickGalaxy() {
  const N = G.nodes;
  for (const a of N) { a.vx += (a.cx - a.x) * 0.002; a.vy += (a.cy - a.y) * 0.002; for (const b of N) if (a !== b) { const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 30; a.vx += dx / d2 * 6; a.vy += dy / d2 * 6; } }
  for (const l of G.links) { const a = G.byId[l.source], b = G.byId[l.target]; if (!a || !b) continue; const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, f = (d - 55) * 0.004; a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f; }
  for (const n of N) { n.vx *= 0.85; n.vy *= 0.85; n.x = Math.max(8, Math.min(312, n.x + n.vx)); n.y = Math.max(8, Math.min(232, n.y + n.vy)); }
}
function drawGalaxy() {
  const x = $("galaxy").getContext("2d"), now = Date.now(); x.clearRect(0, 0, 320, 240); tickGalaxy(); x.lineWidth = 1;
  for (const l of G.links) { const a = G.byId[l.source], b = G.byId[l.target]; if (!a || !b) continue; x.strokeStyle = "rgba(140,160,230," + (0.1 + l.w * 0.25) + ")"; x.beginPath(); x.moveTo(a.x, a.y); x.lineTo(b.x, b.y); x.stroke(); }
  for (const n of G.nodes) {
    const hit = G.hits[n.id] && now - G.hits[n.id] < 6000, r = 2.5 + Math.min(5, Math.sqrt(n.size)) + (hit ? 3 + 2 * Math.sin(now / 120) : 0);
    if (hit) { x.strokeStyle = "rgba(255,255,255,.5)"; x.beginPath(); x.moveTo(160, 120); x.lineTo(n.x, n.y); x.stroke(); }
    x.fillStyle = COLORS[n.kind] || "#aaa"; x.shadowColor = x.fillStyle; x.shadowBlur = hit ? 18 : 6; x.beginPath(); x.arc(n.x, n.y, r, 0, 6.283); x.fill();
  }
  x.shadowBlur = 0; requestAnimationFrame(drawGalaxy);
}
$("galaxy").addEventListener("mousemove", e => {
  const r = e.target.getBoundingClientRect(), mx = (e.clientX - r.left) * 320 / r.width, my = (e.clientY - r.top) * 240 / r.height;
  const n = G.nodes.find(n => Math.hypot(n.x - mx, n.y - my) < 8); $("galaxyLabel").textContent = n ? `${n.kind} · ${n.label}` : "";
});

// ---- boot
$("lang").value = lang;
$("lang").onchange = e => { lang = e.target.value; localStorage.setItem("lang", lang); send({ type: "lang", lang }); if (!localEars && recWanted) restartRec(); };
$("barge").onchange = e => { if (localEars) { send({ type: "barge", enabled: e.target.checked }); return; } if (e.target.checked) setupBarge().catch(() => { e.target.checked = false; }); };
$("micBtn").onclick = () => {
  if (localEars) { localMuted = !localMuted; send({ type: "mic_toggle", enabled: !localMuted }); $("micBtn").textContent = localMuted ? "🎙 muted" : "🎙 on"; if (localMuted) { setState("idle"); $("status").textContent = "muted"; } return; }
  if (recWanted) { recWanted = false; stopRec(); $("micBtn").textContent = "🎙 muted"; setState("idle"); $("status").textContent = "muted"; }
  else { startRec(); $("micBtn").textContent = "🎙 on"; setState(idleState()); }
};
$("startBtn").onclick = () => { $("start").classList.add("hidden"); if (localEars) { setState("idle"); return; } startRec(); setState("listening"); windowUntil = 0; };
document.querySelectorAll("#memTabs button").forEach(b => b.onclick = () => { memTab = b.dataset.tab; document.querySelectorAll("#memTabs button").forEach(x => x.classList.toggle("on", x === b)); renderMemory(); });
$("speakToggle").checked = localStorage.getItem("speak") !== "0";
$("speakToggle").onchange = e => localStorage.setItem("speak", e.target.checked ? "1" : "0");
const ask = () => { const t = $("askInput").value.trim(); if (!t) return; $("askInput").value = ""; if (speaking || current) interrupt(); command(t, true); };
$("askBtn").onclick = ask;
$("askInput").onkeydown = e => { if (e.key === "Enter" && !e.isComposing) ask(); };
greet(); setInterval(greet, 60000);
connect(); loadGalaxy(); setInterval(loadGalaxy, 120000); requestAnimationFrame(drawGalaxy);
})();