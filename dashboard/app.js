const $ = (id) => document.getElementById(id);
async function api(path, method = "GET") {
  const r = await fetch(path, { method });
  return r.json();
}
const fmtPct = (x) => (x * 100).toFixed(0) + "%";
const fmtTime = (iso) => {
  try {
    return new Date(iso).toLocaleString("ja-JP", {
      timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }) + " JST";
  } catch (e) { return iso; }
};

// --- 日本語ラベル ---
const CAT_JA = {
  bad_deploy: "不正なデプロイ", feature_bug: "新機能のバグ", out_of_memory: "メモリ不足",
  dependency_5xx: "依存先の障害", crash_loop: "クラッシュループ",
  traffic_spike: "アクセス急増", unknown: "原因不明",
};
const ACT_JA = {
  rollback: "ロールバック（前リビジョンへ復帰）",
  self_heal: "🔧 AIコード修正（新機能は維持）",
  scale_memory: "🧠 メモリ上限を引き上げ",
  scale_instances: "📈 max-instances を増やす",
  restart: "🔄 再起動（新リビジョン）",
  escalate: "人へエスカレーション", none: "対応なし",
};
const OUT_JA = {
  resolved: "✅ 解決", not_resolved: "⚠️ 未解決", dry_run: "🟦 ドライラン（意図のみ）",
  escalated: "🔔 人へ通知", loop_guard: "🛡️ ループ保護で抑止", no_action: "—",
  awaiting_approval: "⏳ 承認待ち（AI修正案）", self_healed: "✅ 自己修復（コード修正）",
  not_resolved_rolled_back: "↩ 未解決→ロールバック退避",
};
const ja = (m, k) => m[k] || k || "—";

function renderDiff(diff) {
  if (!diff) return '<span class="muted">（差分なし）</span>';
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return diff.split("\n").map((l) => {
    let cls = "";
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("@@")) cls = "hdr";
    else if (l.startsWith("+")) cls = "add";
    else if (l.startsWith("-")) cls = "del";
    return `<div class="dl ${cls}">${esc(l) || " "}</div>`;
  }).join("");
}

function renderHeal(s) {
  const panel = $("healPanel");
  const heal = (s.incidents || []).find((i) => i.fix_diff);  // 最新の修正案/結果
  if (!heal) { panel.style.display = "none"; return; }
  panel.style.display = "block";
  const f = heal.fix || {};
  let pill = '<span class="pill warn">⏳ 承認待ち</span>';
  if (heal.outcome === "self_healed") pill = '<span class="pill ok">✅ デプロイ済み・復旧を確認</span>';
  else if (heal.outcome === "not_resolved_rolled_back") pill = '<span class="pill bad">⚠️ 修正後も未解決 → ロールバック退避</span>';
  $("healStatus").innerHTML = pill;
  $("healSummary").innerHTML =
    `<b>${f.summary || "コード修正案"}</b><br>${f.bug_explanation || ""}` +
    (f.kept_feature ? '<br><span class="ok">▶ 新機能は維持（ロールバックなら失われていた）</span>' : "");
  $("healDiff").innerHTML = renderDiff(heal.fix_diff);
  $("healControls").style.display = (heal.outcome === "awaiting_approval") ? "flex" : "none";
}

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
  svg.innerHTML = `<polyline fill="none" stroke="${last > 0.1 ? "#f85149" : "#2ea043"}" stroke-width="2" points="${pts}"/>`;
}

function revRole(b) {
  const cur = b.current_revision;
  if (cur && cur === b.healthy_revision) return '<span class="ok">正常版</span>';
  if (cur && cur === b.bad_revision) return '<span class="bad">不調版</span>';
  return "";
}

function render(s) {
  const b = s.backend, c = s.config;
  const modeEl = $("mode");
  modeEl.textContent = (c.mode === "real") ? "REAL（本物のCloud Run操作）" : "SIM（シミュレーション）";
  modeEl.className = "badge " + (c.mode === "real" ? "real" : "sim");
  $("llm").textContent = "診断: " + c.llm;

  const er = b.error_rate;
  const erEl = $("errorRate");
  erEl.textContent = fmtPct(er);
  erEl.className = "big " + (er > c.error_rate_threshold ? "bad" : "ok");
  $("svc").textContent = b.service;
  $("rev").innerHTML = `<code>${b.current_revision || "—"}</code> ${revRole(b)}`;
  $("status").innerHTML = b.injected ? '<span class="bad">不調（障害注入中）</span>' : '<span class="ok">正常</span>';
  const mem = b.memory_mib ? `${b.memory_mib}MiB` : "—";
  const inst = (b.max_instances != null) ? `${b.max_instances}` : "—";
  $("capacity").innerHTML = `<code>${mem}</code> / 最大 <code>${inst}</code>`;
  $("healthyRev").textContent = b.healthy_revision || "—";
  $("badRev").textContent = b.bad_revision || "—";
  spark(b.history);

  const inc = s.incidents[0];
  if (inc) {
    const d = inc.diagnosis, dec = inc.decision;
    let html =
      `<div class="kv"><span>診断</span><span><b>${ja(CAT_JA, d.category)}</b>（確信度 ${(d.confidence * 100).toFixed(0)}%）</span></div>` +
      `<div class="kv"><span>アクション</span><span>${ja(ACT_JA, dec.action)}</span></div>` +
      `<div class="kv"><span>結果</span><span>${ja(OUT_JA, inc.outcome)}</span></div>` +
      `<div class="muted" style="margin-top:8px">${d.reasoning || ""}</div>`;
    if (inc.context_used && inc.context_used.trim()) {
      html += `<div class="ctx"><b>📚 この診断が参照した過去の知見（学習→診断）</b>\n${inc.context_used}</div>`;
    } else {
      html += `<div class="muted" style="margin-top:6px">📚 参照した過去の知見: まだなし（最初のインシデント）</div>`;
    }
    $("agent").innerHTML = html;
  }

  $("timeline").innerHTML = s.incidents.length
    ? s.incidents.map((i) => `<div class="inc ${i.outcome || ""}">
        <div><b>${ja(CAT_JA, i.diagnosis.category)}</b> → ${ja(ACT_JA, i.decision.action)}
        <span class="muted">（${ja(OUT_JA, i.outcome)}）</span></div>
        <div class="muted">${fmtTime(i.timestamp)}</div></div>`).join("")
    : '<span class="muted">まだインシデントはありません。</span>';

  $("playbook").textContent = s.playbook || "（まだ学習データなし。インシデントを重ねると「この障害署名にはこの対応が効いた」が貯まり、次の診断に渡されます）";
  renderHeal(s);
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { /* noop */ } }

$("inject").onclick = async () => { await api("/api/inject", "POST"); refresh(); };
$("injectFeature").onclick = async () => { await api("/api/inject_feature", "POST"); refresh(); };
$("injectOom").onclick = async () => { await api("/api/inject_oom", "POST"); refresh(); };
$("injectTraffic").onclick = async () => { await api("/api/inject_traffic", "POST"); refresh(); };
$("tick").onclick = async () => { await api("/api/tick", "POST"); refresh(); };
$("reset").onclick = async () => { await api("/api/reset", "POST"); refresh(); };
$("approveFix").onclick = async () => {
  const btn = $("approveFix");
  btn.disabled = true; btn.textContent = "🔧 デプロイ中…（検証まで実行）";
  const auto = $("auto").checked; $("auto").checked = false;
  try { await api("/api/approve_fix", "POST"); } catch (e) { /* noop */ }
  $("auto").checked = auto; btn.disabled = false; btn.textContent = "✅ この修正を承認してデプロイ";
  refresh();
};

$("agentBtn").onclick = async () => {
  const btn = $("agentBtn"), out = $("agentOut");
  btn.disabled = true; const auto = $("auto").checked; $("auto").checked = false;
  out.style.display = "block"; out.textContent = "🤖 ADK エージェント実行中…（観測→判断→必要ならロールバック）";
  try {
    const r = await api("/api/agent", "POST");
    if (r.ok) {
      const steps = (r.steps || []).map((s) => `・${s.tool}(${Object.entries(s.args || {}).map(([k, v]) => `${k}=${v}`).join(", ")})`).join("\n");
      out.textContent = `🤖 ADK エージェントが実行した手順:\n${steps || "（ツール呼び出しなし）"}\n\n最終回答:\n${r.final || ""}`;
    } else {
      out.textContent = "エラー: " + r.error;
    }
  } catch (e) { out.textContent = "通信エラー: " + e; }
  btn.disabled = false; $("auto").checked = auto; refresh();
};

setInterval(refresh, 1500);
setInterval(async () => { if ($("auto").checked) { await api("/api/tick", "POST"); refresh(); } }, 2500);
refresh();
