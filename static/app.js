const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  pendingTemp: null,
  pendingSent: null,
  pendingDeadline: 0,
  cfg: null,
};

function fmtTemp(v, unit) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return unit === "C" ? String(Math.round(v * 10) / 10) : String(Math.round(v));
}

function pretty(s) {
  return String(s || "—").replaceAll("_", " ");
}

const LABELS = {
  off: "Off",
  on: "On",
  low: "Low",
  high: "High",
  ready: "Ready",
  rest: "Rest",
  ready_in_rest: "Ready in rest",
  heating: "Heating",
  heat_waiting: "Heat waiting",
};

function speedsOf(item) {
  const raw = state.cfg ? state.cfg[`${item}_speeds`] : undefined;
  const fallback = item === "pump1" ? 2 : 1;
  return Number(raw ?? fallback) === 2 ? 2 : 1;
}

function stateText(item, val) {
  if (item === "blower" || (item.startsWith("pump") && speedsOf(item) === 1)) {
    return val === "off" ? "Off" : "On";
  }
  if (item.startsWith("pump")) {
    if (val === "low") return "Speed 1";
    if (val === "high") return "Speed 2";
    return "Off";
  }
  return LABELS[val] || pretty(val);
}

function fmtClock(st) {
  if (!st.connected && !st.last_update) return "—";
  const minute = String(st.minute ?? 0).padStart(2, "0");
  let hour = Number(st.hour) || 0;
  if (st.time_24h) return `${String(hour).padStart(2, "0")}:${minute}`;
  const suffix = hour >= 12 ? "PM" : "AM";
  hour %= 12;
  if (hour === 0) hour = 12;
  return `${hour}:${minute} ${suffix}`;
}

function rangeTip(st) {
  const unit = st.unit || "F";
  const band = `${st.temp_min}–${st.temp_max}°${unit}`;
  if (st.temp_range === "low") {
    return `Low range is ${band}, for when nobody is using the tub. High is the hotter soaking range. Tap to switch.`;
  }
  return `High range is ${band}, the normal soaking band. Low is cooler, for when the tub is not in use. Tap to switch.`;
}

function applyPumpTips() {
  for (const item of ["pump1", "pump2", "pump3"]) {
    const tip = document.querySelector(`[data-item="${item}"] .tip`);
    if (!tip) continue;
    tip.dataset.tip = speedsOf(item) === 2
      ? "Each tap moves one step: Off, Speed 1, Speed 2, then back to Off."
      : "Each tap turns this pump on or off.";
  }
}

function settlePending(st) {
  if (state.pendingSent == null) return;
  const unit = st.unit || "F";
  const tol = unit === "C" ? 0.26 : 0.51;
  const arrived = st.set_temp != null && Math.abs(st.set_temp - state.pendingSent) <= tol;
  const expired = Date.now() > state.pendingDeadline;
  if (!arrived && !expired) return;
  if (state.pendingTemp === state.pendingSent) state.pendingTemp = null;
  state.pendingSent = null;
}

function controlOn(item, val) {
  if (item === "temp_range") return val === "high";
  if (item === "heat_mode") return val !== "rest";
  if (item === "light" || item === "hold") return val === "on";
  return Boolean(val) && val !== "off";
}

function render(st) {
  state.status = st;
  settlePending(st);
  const unit = st.unit || "F";
  $("label").textContent = st.label || "Spa";
  $("endpoint").textContent = `${st.mode || "configured_ip"} · ${st.host}:${st.port}`;
  const current = $("current");
  if (st.hold) {
    current.textContent = "Hold";
    current.classList.add("held");
  } else {
    current.classList.remove("held");
    current.innerHTML = `${fmtTemp(st.current_temp, unit)}<small>°${unit}</small>`;
  }
  const shown = state.pendingTemp ?? st.set_temp;
  $("setpoint").textContent = `${fmtTemp(shown, unit)}°`;
  $("clock").innerHTML = `Spa time <b>${fmtClock(st)}</b>`;
  $("heat").textContent = stateText("heat", st.heat_state);
  $("mode").textContent = stateText("mode", st.heat_mode);
  $("range").textContent = stateText("range", st.temp_range);
  const circKnown = !!(st.connected || st.last_update);
  $("circ-state").textContent = circKnown ? stateText("circ", st.circ || "off") : "—";
  $("circ").classList.toggle("on", circKnown && st.circ === "on");
  const rangeText = rangeTip(st);
  $("range-tip").dataset.tip = rangeText;
  $("range-ctrl-tip").dataset.tip = rangeText;

  const connected = !!st.connected;
  $("dot").className = "dot " + (connected ? "ok" : st.last_error ? "err" : "warn");
  $("link").textContent = connected
    ? (st.heat_state === "heating" ? "heating" : "linked")
    : (st.last_error ? "offline" : "connecting");
  $("error").textContent = st.last_error || "";

  const notes = [];
  if (st.priming) notes.push("Priming — pumps are running on their own");
  if (st.spa_state && !["running", "unknown", "hold"].includes(st.spa_state)) {
    notes.push(pretty(st.spa_state));
  }
  const notice = $("notice");
  notice.textContent = notes.join(" · ");
  notice.classList.toggle("hidden", notes.length === 0);

  const map = {
    pump1: st.pump1,
    pump2: st.pump2,
    pump3: st.pump3,
    light: st.light,
    blower: st.blower,
    heat_mode: st.heat_mode,
    temp_range: st.temp_range,
    hold: st.hold ? "on" : "off",
  };
  document.querySelectorAll(".ctrl[data-item]").forEach((btn) => {
    const item = btn.dataset.item;
    const val = map[item] || "off";
    const slot = btn.querySelector("[data-state]");
    if (slot) slot.textContent = stateText(item, val);
    btn.classList.toggle("on", controlOn(item, val));
  });
  applyPumpTips();
  const tou = st.tou || {};
  const line = $("tou-line");
  line.textContent = tou.summary || "";
  line.classList.toggle("hidden", !tou.summary);
  const soaking = !!tou.override_until;
  $("tou-use").classList.toggle("hidden", !(tou.enabled || soaking));
  $("tou-override-end").classList.toggle("hidden", !soaking);
  const holdTip = document.querySelector('[data-item="hold"] .tip');
  if (holdTip) {
    holdTip.dataset.tip = soaking
      ? "A soak timer is running, so the schedule will not turn hold back on until it ends."
      : tou.enabled && tou.active
        ? "The rate schedule is keeping hold on through these hours, so the heater and pumps stay off. Turning hold off here turns it back on at the next check. Use the tub to pause that for 20, 40, or 60 minutes."
        : "Pauses heating and filtration. Tap again to resume. The water will cool until you turn hold off.";
  }
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

async function command(action, extra = {}) {
  await api("/api/command", {
    method: "POST",
    body: JSON.stringify({ action, ...extra }),
  });
}

let wsTimer = null;

function connectWs() {
  if (wsTimer) {
    clearInterval(wsTimer);
    wsTimer = null;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (_) {
      return;
    }
    if (msg.type === "status") render(msg.data);
  };
  ws.onclose = () => {
    if (wsTimer) clearInterval(wsTimer);
    wsTimer = null;
    setTimeout(connectWs, 1500);
  };
  wsTimer = setInterval(() => {
    if (ws.readyState === 1) ws.send("ping");
  }, 20000);
}

function nudge(delta) {
  const st = state.status;
  if (!st) return;
  const unit = st.unit || "F";
  const step = unit === "C" ? 0.5 : 1;
  const base = state.pendingTemp ?? st.set_temp ?? (unit === "C" ? 38 : 100);
  const next = Math.min(st.temp_max || 104, Math.max(st.temp_min || 50, base + delta * step));
  state.pendingTemp = next;
  $("setpoint").textContent = `${fmtTemp(next, unit)}°`;
  clearTimeout(nudge._t);
  nudge._t = setTimeout(async () => {
    const sent = state.pendingTemp;
    try {
      await command("set_temp", { value: sent });
      state.pendingSent = sent;
      state.pendingDeadline = Date.now() + 5000;
    } catch (err) {
      if (state.pendingTemp === sent) state.pendingTemp = null;
      state.pendingSent = null;
      $("error").textContent = err.message;
    }
  }, 350);
}

async function loadConfig() {
  const cfg = await api("/api/config");
  state.cfg = cfg;
  $("host").value = cfg.host || "";
  $("port").value = cfg.port || 4257;
  $("mac").value = cfg.mac || "";
  $("name").value = cfg.label || "";
  $("mock").checked = !!cfg.mock;
  $("show-p3").checked = cfg.show_pump3 !== false;
  $("show-blower").checked = cfg.show_blower !== false;
  $("p1-speeds").value = String(cfg.pump1_speeds === 1 ? 1 : 2);
  $("p2-speeds").value = String(cfg.pump2_speeds === 2 ? 2 : 1);
  $("p3-speeds").value = String(cfg.pump3_speeds === 2 ? 2 : 1);
  $("pump3").classList.toggle("hidden", cfg.show_pump3 === false);
  $("blower").classList.toggle("hidden", cfg.show_blower === false);
  $("tou-enabled").checked = !!cfg.tou_enabled;
  renderMonths(cfg.tou_months);
  renderWindows(cfg.tou_windows || []);
  applyPumpTips();
  return cfg;
}

const DAY_LABELS = [
  ["M", "Monday"],
  ["T", "Tuesday"],
  ["W", "Wednesday"],
  ["T", "Thursday"],
  ["F", "Friday"],
  ["S", "Saturday"],
  ["S", "Sunday"],
];

const MONTH_LABELS = [
  ["Jan", "January"],
  ["Feb", "February"],
  ["Mar", "March"],
  ["Apr", "April"],
  ["May", "May"],
  ["Jun", "June"],
  ["Jul", "July"],
  ["Aug", "August"],
  ["Sep", "September"],
  ["Oct", "October"],
  ["Nov", "November"],
  ["Dec", "December"],
];
const ALL_MONTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
const SUMMER_MONTHS = [5, 6, 7, 8, 9];

function renderMonths(months) {
  const selected = new Set(
    (Array.isArray(months) && months.length ? months : ALL_MONTHS).map(Number)
  );
  const root = $("tou-months");
  root.replaceChildren();
  MONTH_LABELS.forEach(([label, name], index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "month";
    button.dataset.month = String(month);
    button.setAttribute("aria-pressed", selected.has(month) ? "true" : "false");
    button.setAttribute("aria-label", name);
    button.textContent = label;
    root.appendChild(button);
  });
}

function readMonths() {
  return [...document.querySelectorAll("#tou-months .month[aria-pressed='true']")].map(
    (button) => Number(button.dataset.month)
  );
}

function setMonths(months) {
  const selected = new Set(months);
  document.querySelectorAll("#tou-months .month").forEach((button) => {
    const on = selected.has(Number(button.dataset.month));
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function renderWindows(windows) {
  const root = $("tou-windows");
  root.replaceChildren();
  windows.forEach((entry) => root.appendChild(windowRow(entry)));
}

function timeField(labelText, className, value) {
  const wrap = document.createElement("div");
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.className = className;
  input.type = "time";
  input.required = true;
  input.value = value || "16:00";
  wrap.append(label, input);
  return wrap;
}

function windowRow(entry) {
  const row = document.createElement("div");
  row.className = "tou-window";
  const days = new Set((entry.days || [0, 1, 2, 3, 4, 5, 6]).map(Number));
  const times = document.createElement("div");
  times.className = "tou-times";
  times.append(timeField("From", "tou-start", entry.start), timeField("Until", "tou-end", entry.end));
  const dayRow = document.createElement("div");
  dayRow.className = "tou-days";
  dayRow.innerHTML = DAY_LABELS.map(([label, name], day) => {
    const pressed = days.has(day) ? "true" : "false";
    return `<button type="button" class="day" data-day="${day}" aria-pressed="${pressed}" aria-label="${name}">${label}</button>`;
  }).join("");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "ghost tou-remove";
  remove.textContent = "Remove";
  // A focused time field on iOS spends the first tap dismissing itself.
  // Handling the touch here makes Remove run on that tap.
  remove.addEventListener("touchend", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    removeWindow(row);
  });
  remove.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    removeWindow(row);
  });
  row.append(times, dayRow, remove);
  return row;
}

function readWindows() {
  return [...document.querySelectorAll(".tou-window")].map((row) => ({
    start: row.querySelector(".tou-start").value,
    end: row.querySelector(".tou-end").value,
    days: [...row.querySelectorAll(".day[aria-pressed='true']")].map((button) => Number(button.dataset.day)),
  }));
}

async function boot() {
  try {
    const cfg = await loadConfig();
    if (cfg.pin_set && !sessionStorage.getItem("spa-token")) {
      $("app").classList.add("hidden");
      $("gate").classList.remove("hidden");
      $("unlock").onclick = async () => {
        try {
          const res = await api("/api/login", {
            method: "POST",
            body: JSON.stringify({ pin: $("pin").value }),
          });
          sessionStorage.setItem("spa-token", res.token);
          $("gate").classList.add("hidden");
          $("app").classList.remove("hidden");
          render(await api("/api/status"));
          connectWs();
        } catch (err) {
          $("gate-err").textContent = err.message;
        }
      };
      return;
    }
  } catch (err) {
    $("error").textContent = err.message;
  }
  try {
    render(await api("/api/status"));
  } catch (_) {
    /* ws will fill in */
  }
  connectWs();
}

$("reload").onclick = () => location.reload();
$("temp-up").onclick = () => nudge(1);
$("temp-down").onclick = () => nudge(-1);
$("controls").onclick = async (ev) => {
  if (ev.target.closest(".tip")) return;
  const btn = ev.target.closest("[data-item]");
  if (!btn) return;
  try {
    await command("toggle", { item: btn.dataset.item });
  } catch (err) {
    $("error").textContent = err.message;
  }
};
$("tou-override-start").onclick = () => command("tou_override", {
  minutes: Number($("tou-override-minutes").value),
}).catch((e) => ($("error").textContent = e.message));
$("tou-override-end").onclick = () => command("tou_override_end").catch((e) => ($("error").textContent = e.message));
$("sync-time").onclick = () => command("set_time").catch((e) => ($("error").textContent = e.message));
$("reconnect").onclick = () => command("reconnect").catch((e) => ($("error").textContent = e.message));
$("save").onclick = async () => {
  $("settings-msg").textContent = "Saving…";
  try {
    await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({
        host: $("host").value.trim(),
        port: Number($("port").value),
        mac: $("mac").value.trim(),
        label: $("name").value.trim(),
        mock: $("mock").checked,
        show_pump3: $("show-p3").checked,
        show_blower: $("show-blower").checked,
        pump1_speeds: Number($("p1-speeds").value),
        pump2_speeds: Number($("p2-speeds").value),
        pump3_speeds: Number($("p3-speeds").value),
      }),
    });
    await loadConfig();
    $("settings-msg").textContent = "Saved. Reconnecting to configured IP.";
  } catch (err) {
    $("settings-msg").textContent = err.message;
  }
};
let scheduleWrite = Promise.resolve();

function enqueueScheduleWrite(task) {
  const run = scheduleWrite.then(task, task);
  scheduleWrite = run.then(() => {}, () => {});
  return run;
}

function removeWindow(row) {
  if (!row.isConnected) return;
  row.remove();
  // Saved immediately. Leaving this until the next Save let a reload
  // rebuild the row, which is what made a new block look undeletable.
  // Months ride along so the chips on screen are the ones stored.
  // The on/off checkbox does not: Remove must not arm the schedule.
  const windows = readWindows();
  const months = readMonths();
  return enqueueScheduleWrite(async () => {
    $("tou-msg").textContent = "Saving…";
    try {
      const saved = await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({ tou_windows: windows, tou_months: months }),
      });
      if (state.cfg) {
        state.cfg.tou_windows = saved.tou_windows;
        state.cfg.tou_months = saved.tou_months;
      }
      renderWindows(saved.tou_windows || []);
      $("tou-msg").textContent = "Removed.";
    } catch (err) {
      try {
        await loadConfig();
      } catch (_) {
        /* keep the save error */
      }
      $("tou-msg").textContent = err.message;
    }
  });
}

$("tou-months").onclick = (ev) => {
  const target = ev.target instanceof Element ? ev.target : ev.target.parentElement;
  const month = target && target.closest(".month");
  if (!month || !$("tou-months").contains(month)) return;
  const turningOff = month.getAttribute("aria-pressed") === "true";
  if (turningOff && readMonths().length === 1) {
    $("tou-msg").textContent = "Leave at least one month on. Turn the schedule off if it should never run.";
    return;
  }
  month.setAttribute("aria-pressed", turningOff ? "false" : "true");
};
$("tou-summer").onclick = () => setMonths(SUMMER_MONTHS);
$("tou-all-year").onclick = () => setMonths(ALL_MONTHS);
$("tou-windows").onclick = (ev) => {
  const target = ev.target instanceof Element ? ev.target : ev.target.parentElement;
  const day = target && target.closest(".day");
  if (!day || !$("tou-windows").contains(day)) return;
  day.setAttribute("aria-pressed", day.getAttribute("aria-pressed") === "true" ? "false" : "true");
};
$("tou-add").onclick = () => {
  if (document.querySelectorAll(".tou-window").length >= 8) {
    $("tou-msg").textContent = "Eight windows is the limit.";
    return;
  }
  $("tou-windows").appendChild(windowRow({
    start: "16:00",
    end: "21:00",
    days: [0, 1, 2, 3, 4],
  }));
};
$("tou-save").onclick = () => {
  const payload = {
    tou_enabled: $("tou-enabled").checked,
    tou_windows: readWindows(),
    tou_months: readMonths(),
  };
  return enqueueScheduleWrite(async () => {
    $("tou-msg").textContent = "Saving…";
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await loadConfig();
      $("tou-msg").textContent = "Saved. The schedule uses this machine's clock.";
    } catch (err) {
      $("tou-msg").textContent = err.message;
    }
  });
};
$("scan").onclick = async () => {
  $("settings-msg").textContent = "Broadcasting UDP 30303…";
  try {
    const res = await api("/api/scan", { method: "POST", body: "{}" });
    if (!res.found.length) {
      $("settings-msg").textContent = "No Balboa module answered. Configured IP is unchanged.";
      return;
    }
    const lines = res.found.map((x) => `${x.host}  ${x.mac}  ${x.name}`).join(" · ");
    $("settings-msg").textContent = `Heard: ${lines}. Not switching automatically — paste an IP above if you want.`;
  } catch (err) {
    $("settings-msg").textContent = err.message;
  }
};

const tipPop = $("tip-pop");
let tipAnchor = null;

function hideTip() {
  tipAnchor = null;
  tipPop.classList.add("hidden");
}

function showTip(anchor) {
  const text = anchor.dataset.tip || "";
  if (tipAnchor === anchor && !tipPop.classList.contains("hidden")) {
    hideTip();
    return;
  }
  tipAnchor = anchor;
  tipPop.textContent = text;
  tipPop.classList.remove("hidden");
  const rect = anchor.getBoundingClientRect();
  const width = tipPop.offsetWidth;
  const height = tipPop.offsetHeight;
  let left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - 12) top = Math.max(12, rect.top - height - 8);
  tipPop.style.left = `${left}px`;
  tipPop.style.top = `${top}px`;
}

document.addEventListener("click", (ev) => {
  const tip = ev.target.closest(".tip");
  if (tip) {
    ev.preventDefault();
    showTip(tip);
    return;
  }
  if (!ev.target.closest("#tip-pop")) hideTip();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const tip = ev.target.closest && ev.target.closest(".tip");
  if (!tip) return;
  ev.preventDefault();
  showTip(tip);
});

boot();
