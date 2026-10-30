/* wb2strm · 前端 */
(function () {
  "use strict";

  const TOKEN = new URLSearchParams(location.search).get("token") || "";
  const PAGE_SIZE = 60;
  const state = { cat: "", year: "", q: "", page: 1, total: 0 };
  let currentVideo = null;
  let hls = null;
  let jobTimer = null;

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 基础
  function withToken(url) {
    if (!TOKEN) return url;
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
  }

  async function api(path, params) {
    const u = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) u.searchParams.set(k, v);
    });
    if (TOKEN) u.searchParams.set("token", TOKEN);
    const r = await fetch(u.toString());
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
    return data;
  }

  async function post(path, body) {
    const u = new URL(path, location.origin);
    if (TOKEN) u.searchParams.set("token", TOKEN);
    const r = await fetch(u.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, data };
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---------------------------------------------------------------- 分类
  async function loadTypes() {
    let data;
    try {
      data = await api("/api/types");
    } catch (e) {
      return;
    }
    const chips = $("chips");
    chips.innerHTML = "";
    const total = (data.cats || []).reduce((a, c) => a + c.count, 0);
    const all = el("div", "chip" + (state.cat === "" ? " active" : ""));
    all.innerHTML = "全部<b>" + total + "</b>";
    all.onclick = () => setCat("");
    chips.appendChild(all);
    (data.cats || []).forEach((c) => {
      const d = el("div", "chip" + (state.cat === c.name ? " active" : ""));
      d.innerHTML = c.name + "<b>" + c.count + "</b>";
      d.onclick = () => setCat(c.name);
      chips.appendChild(d);
    });

    const yb = $("yearbar");
    yb.innerHTML = "";
    if ((data.years || []).length) {
      const sel = document.createElement("select");
      const o0 = document.createElement("option");
      o0.value = "";
      o0.textContent = "全部年份";
      sel.appendChild(o0);
      (data.years || []).slice(0, 20).forEach((y) => {
        const o = document.createElement("option");
        o.value = y.year;
        o.textContent = y.year + "（" + y.count + "）";
        sel.appendChild(o);
      });
      sel.value = state.year;
      sel.onchange = () => {
        state.year = sel.value;
        state.page = 1;
        loadList();
      };
      yb.appendChild(sel);
    }
  }

  function setCat(name) {
    state.cat = name;
    state.page = 1;
    document.querySelectorAll("#chips .chip").forEach((c) => c.classList.remove("active"));
    Array.from(document.querySelectorAll("#chips .chip")).forEach((c) => {
      if (c.textContent.replace(/\d+$/, "") === name || (name === "" && c.textContent.indexOf("全部") === 0)) {
        c.classList.add("active");
      }
    });
    loadList();
  }

  // ---------------------------------------------------------------- 列表
  function posterBox(item) {
    const box = el("div", "poster");
    box.appendChild(el("div", "ph", "▶"));
    if (item.pic) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.alt = item.name || "";
      img.onload = () => img.classList.add("loaded");
      img.onerror = () => img.remove();
      img.src = item.pic;
      box.appendChild(img);
    }
    if (item.remarks) {
      const b = el("div", "badge", item.remarks);
      if (/新|更新/.test(item.remarks)) b.classList.add("new");
      box.appendChild(b);
    }
    return box;
  }

  function card(item) {
    const c = el("div", "card");
    c.appendChild(posterBox(item));
    const info = el("div", "info");
    info.appendChild(el("div", "name", item.name || item.vod_id));
    const meta = el("div", "meta");
    meta.appendChild(el("span", "", item.year || "—"));
    meta.appendChild(el("span", "", "·"));
    meta.appendChild(el("span", "", (item.episode_count || 0) + "集"));
    info.appendChild(meta);
    c.appendChild(info);
    c.onclick = () => openDetail(item.vod_id);
    return c;
  }

  async function loadList() {
    const grid = $("grid");
    grid.innerHTML = "";
    const loading = el("div", "empty", "加载中…");
    loading.style.display = "block";
    grid.parentNode.insertBefore(loading, grid);
    try {
      const data = await api("/api/videos", {
        cat: state.cat,
        year: state.year,
        q: state.q,
        limit: PAGE_SIZE,
        offset: (state.page - 1) * PAGE_SIZE,
      });
      state.total = data.total || 0;
      (data.list || []).forEach((it) => grid.appendChild(card(it)));
      $("empty").style.display = state.total ? "none" : "block";
      const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
      $("pager").style.display = state.total > PAGE_SIZE ? "flex" : "none";
      $("pageinfo").textContent = "第 " + state.page + " / " + pages + " 页 · 共 " + state.total + " 部";
      $("prev").disabled = state.page <= 1;
      $("next").disabled = state.page >= pages;
    } catch (e) {
      toast("加载失败：" + e.message);
    } finally {
      loading.remove();
    }
  }

  // ---------------------------------------------------------------- 详情
  async function openDetail(vid) {
    let data;
    try {
      data = await api("/api/video/" + encodeURIComponent(vid));
    } catch (e) {
      toast("打开失败：" + e.message);
      return;
    }
    const v = data.video;
    currentVideo = v;
    $("dName").textContent = v.name || v.vod_id;

    const pic = $("dPic");
    pic.classList.remove("loaded");
    pic.removeAttribute("src");
    if (v.pic) {
      pic.referrerPolicy = "no-referrer";
      pic.onload = () => pic.classList.add("loaded");
      pic.src = v.pic;
    }

    const tags = $("dTags");
    tags.innerHTML = "";
    if (v.cat) tags.appendChild(el("span", "tag", v.cat));
    (v.type_name || "").split(",").filter(Boolean).forEach((t) => tags.appendChild(el("span", "tag", t.trim())));

    const kv = [];
    if (v.year) kv.push("<b>" + v.year + "</b>");
    if (v.area) kv.push(v.area);
    if (v.remarks) kv.push(v.remarks);
    kv.push("<b>" + (v.episodes || []).length + "</b> 集");
    $("dMeta").innerHTML = kv.join(" · ");
    $("dPlot").textContent = v.content || "暂无简介";

    const box = $("dEps");
    box.innerHTML = "";
    const eps = v.episodes || [];
    $("dEpTitle").textContent = eps.length ? "选集（" + eps.length + "）" : "选集";
    if (!eps.length) {
      box.appendChild(el("div", "kv", "这部剧还没抓分集，去「运维」页跑一次同步分集。"));
    }
    eps.forEach((ep) => {
      const b = el("button", "ep");
      const nm = String(ep.name || "").trim();
      b.textContent = /^\d+$/.test(nm) ? "第" + nm + "集" : (nm || "第" + ep.seq + "集");
      b.onclick = () => openPlayer(v, ep, b);
      box.appendChild(b);
    });

    $("detailMask").classList.add("show");
  }

  // ---------------------------------------------------------------- 播放
  function openPlayer(v, ep, btn) {
    document.querySelectorAll("#dEps .ep").forEach((b) => b.classList.remove("playing"));
    if (btn) btn.classList.add("playing");

    const label = /^\d+$/.test(String(ep.name || "")) ? "第" + ep.name + "集" : (ep.name || "第" + ep.seq + "集");
    $("pTitle").textContent = (v.name || "") + " · " + label;
    $("playerMask").classList.add("show");
    playWith(v.vod_id, ep.nid, 0);
  }

  function buildPlayUrl(vid, nid, q) {
    let u = location.origin + "/play/" + encodeURIComponent(vid) + "/" + encodeURIComponent(nid) + ".m3u8";
    const qs = [];
    if (q) qs.push("q=" + q);
    if (TOKEN) qs.push("token=" + encodeURIComponent(TOKEN));
    if (qs.length) u += "?" + qs.join("&");
    return u;
  }

  function renderQualities(vid, nid, active) {
    const bar = $("pQual");
    bar.innerHTML = "";
    [1080, 720, 480].forEach((q) => {
      const b = el("button", q === active ? "on" : "", q + "P");
      b.onclick = () => playWith(vid, nid, q);
      bar.appendChild(b);
    });
  }

  function playWith(vid, nid, q) {
    const url = buildPlayUrl(vid, nid, q);
    const video = $("video");
    renderQualities(vid, nid, q || 1080);
    $("pTip").innerHTML =
      '当前地址：<a href="' + url + '" target="_blank" rel="noreferrer">' + url + "</a>" +
      "　（服务端 302 到最新签名地址，有效期约 30 分钟）";

    if (hls) {
      try { hls.destroy(); } catch (e) {}
      hls = null;
    }
    video.removeAttribute("src");
    video.load();

    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({ lowLatencyMode: false, maxBufferLength: 30 });
      hls.on(window.Hls.Events.ERROR, (evt, data) => {
        if (data && data.fatal) {
          $("pTip").innerHTML = "播放失败：" + (data.details || data.type) + "　<a href=\"" + url + "\" target=\"_blank\">直接打开</a>";
        }
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => {});
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.play().catch(() => {});
    } else {
      $("pTip").innerHTML = '浏览器不支持 HLS，请用 <a href="' + url + '" target="_blank">直接打开</a> 或使用 Emby / PotPlayer。';
    }
  }

  function closePlayer() {
    if (hls) {
      try { hls.destroy(); } catch (e) {}
      hls = null;
    }
    const video = $("video");
    video.pause();
    video.removeAttribute("src");
    video.load();
    $("playerMask").classList.remove("show");
  }

  // ---------------------------------------------------------------- 运维
  let opsActive = false;
  let settings = {};

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p2 = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()) +
      " " + p2(d.getHours()) + ":" + p2(d.getMinutes());
  }

  function renderNextRun(ts, hours, last) {
    const box = $("nextRunInfo");
    if (!ts) {
      box.textContent = "下次更新：未安排";
      return;
    }
    box.textContent = "下次更新：" + fmtTime(ts) + "（每 " + hours + " 小时，上次完成 " + fmtTime(last) + "）";
  }

  async function loadStats() {
    let d;
    try {
      d = await api("/api/stats");
    } catch (e) {
      return;
    }
    const row = $("stats");
    row.innerHTML = "";
    [
      [d.videos, "入库剧集"],
      [d.episodes, "分集"],
      [d.strm_count, "STRM 文件"],
    ].forEach(([n, l]) => {
      const box = el("div", "stat");
      box.appendChild(el("div", "n", String(n)));
      box.appendChild(el("div", "l", l));
      row.appendChild(box);
    });
    renderNextRun(d.next_run, d.interval_hours, d.last_finish);
    $("modeText").textContent = d.catalog_complete
      ? "目录：已完整抓取，当前为增量模式"
      : "目录：首次全量抓取中（可随时中断，重启会接着抓）";
  }

  function renderJob(j) {
    const dot = $("jobDot");
    dot.classList.toggle("run", !!j.running);
    let txt = "空闲";
    if (j.running) {
      txt = j.kind + " 进行中… 已运行 " + j.elapsed + " 秒";
    } else if (j.finished) {
      txt = j.kind + " " + (j.ok ? "完成" : "失败") + "，耗时 " + j.elapsed + " 秒" +
        (j.error ? "（" + j.error + "）" : "");
    }
    $("jobState").textContent = txt;

    const p = j.progress || {};
    const total = Number(p.total || 0);
    const done = Number(p.done || 0);
    const pct = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
    $("progBar").style.width = pct + "%";
    $("progText").textContent = total > 0
      ? (p.phase || "") + "  " + done + " / " + total + "（" + pct + "%）"
      : (j.running ? "准备中…" : "");

    const box = $("logbox");
    if (j.logs && j.logs.length) {
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = "";
      j.logs.forEach((line) => {
        box.appendChild(el("div", /\[ERR\]|失败|错误|error/i.test(line) ? "err" : "", line));
      });
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
  }

  async function pollJob() {
    clearTimeout(jobTimer);
    let d;
    try {
      d = await api("/api/job");
    } catch (e) {
      jobTimer = setTimeout(pollJob, 4000);
      return;
    }
    renderJob(d.job);
    await loadStats();
    if (d.job.running) jobTimer = setTimeout(pollJob, 2000);
    else if (opsActive) jobTimer = setTimeout(pollJob, 10000);
  }

  async function loadSettings() {
    let d;
    try {
      d = await api("/api/settings");
    } catch (e) {
      return;
    }
    settings = d.settings || {};
    $("setPublicBase").value = settings.public_base || "";
  }

  async function saveRegen() {
    const r = await post("/api/settings", {
      public_base: $("setPublicBase").value.trim(),
      regen_strm: true,
    });
    if (!r.ok || !r.data.ok) {
      toast((r.data && r.data.error) || "保存失败");
      return;
    }
    toast(r.data.msg || "已保存");
    await loadSettings();
    pollJob();
  }

  // ---------------------------------------------------------------- 事件
  function bind() {
    document.querySelectorAll("nav button").forEach((b) => {
      b.onclick = () => {
        document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        const tab = b.dataset.tab;
        $("browse").style.display = tab === "browse" ? "" : "none";
        $("ops").style.display = tab === "ops" ? "" : "none";
        opsActive = tab === "ops";
        if (opsActive) {
          loadSettings();
          pollJob();
        } else clearTimeout(jobTimer);
      };
    });

    let t = null;
    $("q").addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(() => {
        state.q = e.target.value.trim();
        state.page = 1;
        loadList();
      }, 350);
    });

    $("prev").onclick = () => {
      if (state.page > 1) {
        state.page--;
        loadList();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    };
    $("next").onclick = () => {
      state.page++;
      loadList();
      window.scrollTo({ top: 0, behavior: "smooth" });
    };

    document.querySelectorAll("[data-close]").forEach((b) => {
      b.onclick = () => {
        const id = b.dataset.close;
        if (id === "playerMask") closePlayer();
        else $(id).classList.remove("show");
      };
    });
    document.querySelectorAll(".mask").forEach((m) => {
      m.onclick = (e) => {
        if (e.target === m) {
          if (m.id === "playerMask") closePlayer();
          else m.classList.remove("show");
        }
      };
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if ($("playerMask").classList.contains("show")) closePlayer();
        else $("detailMask").classList.remove("show");
      }
    });

    $("btnSaveRegen").onclick = saveRegen;
  }

  // ---------------------------------------------------------------- 启动
  bind();
  loadTypes().then(loadList);
  api("/api/stats").then((d) => {
    if (d.job && d.job.running) pollJob();
  }).catch(() => {});
})();
