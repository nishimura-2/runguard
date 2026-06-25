const $ = (id) => document.getElementById(id);

async function api(path, method = "GET") {
  const r = await fetch(path, { method });
  return r.json();
}

const fmtPct = (x) => (x * 100).toFixed(0) + "%";

function spark(history) {
  const svg = $("spark");
  if (!history || !history.length) { svg.innerHTML = ""; return; }
  const n = history.length;
  const pts = history.map((h, i) => {
    const x = (n === 1 ? 0 : (i / (n - 1)) * 300);
    const y = 80 - Math.max(0, Math.min(1, h.error_rate)) * 78 - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const last = history[history.length - 1].error_rate;
  const col = last > 0.1 ? "#f85149" : "#2ea043";
  svg.innerHTML = `<polyline fill="none" stroke="${col}" stroke-width="2" points="${pts}"/>`;
}

function render(s) {
  $("mode").textContent = "SIM";
  $("llm").textContent = s.config.llm;
  const er = s.sim.error_rate;
  const erEl = $("errorRate");
  erEl.textContent = fmtPct(er);
  erEl.className = "big " + (er > s.config.error_rate_threshold ? "bad" : "ok");
  $("svc").textContent = s.sim.service;
  $("rev").innerHTML = `<code>${s.sim.current_revision}</code>`;
  $("status").innerHTML = s.sim.injected
    ? '<span class="bad">不調（障害注入中）</span>'
    : '<span class="ok">正常</span>';
  spark(s.sim.history);

  const inc = s.incidents[0];
  if (inc) {
    const d = inc.diagnosis, dec = inc.decision;
    $("agent").innerHTML =
      `<div class="kv"><span>診断</span><span><code>${d.category}</code> (確信度 ${(d.confidence * 100).toFixed(0)}%)</span></div>` +
      `<div class="kv"><span>アクション</span><span><code>${dec.action}</code></span></div>` +
      `<div class="kv"><span>結果</span><span>${inc.outcome || "—"}</span></div>` +
      `<div class="muted" style="margin-top:8px">${d.reasoning || ""}</div>`;
  }

  $("timeline").innerHTML = s.incidents.length
    ? s.incidents.map((i) => `<div class="inc ${i.outcome || ""}">
        <div><code>${i.diagnosis.category}</code> → <code>${i.decision.action}</code>
        <span class="muted">(${i.outcome || "—"})</span></div>
        <div class="muted">${i.timestamp}</div></div>`).join("")
    : '<span class="muted">まだインシデントはありません。</span>';

  $("playbook").textContent = s.playbook || "（まだ学習データなし）";
}

async function refresh() {
  try { render(await api("/api/state")); } catch (e) { /* noop */ }
}

$("inject").onclick = async () => { await api("/api/inject", "POST"); refresh(); };
$("tick").onclick = async () => { await api("/api/tick", "POST"); refresh(); };
$("reset").onclick = async () => { await api("/api/reset", "POST"); refresh(); };

setInterval(refresh, 1500);
setInterval(async () => {
  if ($("auto").checked) { await api("/api/tick", "POST"); refresh(); }
}, 2000);
refresh();
