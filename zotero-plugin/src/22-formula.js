// ===== 公式就地纠错：阅读器右键菜单 → 定位 → 阅读器列里的编辑区 → 回写 =====
//
// 设计口径（2026-10-12 用户拍板）：
//   · **不做 OCR**（本地公式识别模型试过，pix2tex 明显劣于 MinerU、PP-FormulaNet
//     只与 MinerU 打平，按预登记判据停手不落地）；
//   · 编辑方式用 **MathType 剪贴板往返**（会 MathType 的人零学习成本）；
//     未装 MathType 时按钮**不隐藏、只置灰**；
//   · 写回走既有的唯一写路径（服务端 POST /formula-apply → 修订层 + 重切重向量）。
//
// 两个右键事件的分工（真机探针实测，见 20-kbview.js 的同名注释）：
//   · createViewContextMenu        params={x,y,overlay,position}
//       position={pageIndex,rects} 是**点击处在页面上的位置**（左下原点 PDF 点）
//       —— 所以右键公式**不必先画标注**；
//   · createAnnotationContextMenu  params={ids,x,y,view}
//       ids 是标注 item key → 从 annotationPosition 拿 pageIndex/rects。
//   两者互斥：右键已有标注时**只发** annotation 那个。

ZoteroKB.kbFormulaInit = function () {
  var self = ZoteroKB;
  if (self._kbFormulaMenus) return true;
  if (!Zotero.Reader || typeof Zotero.Reader.registerEventListener !== "function") {
    return false;
  }
  self._kbFormulaMenus = function (event) {
    try { self.kbFormulaAddMenuItem(event); }
    catch (e) { Zotero.debug("[zotero-kb] 加阅读器右键菜单失败：" + e); }
  };
  try {
    Zotero.Reader.registerEventListener(
      "createViewContextMenu", self._kbFormulaMenus, self.id);
    Zotero.Reader.registerEventListener(
      "createAnnotationContextMenu", self._kbFormulaMenus, self.id);
    self.writeStatusFile({ kbFormulaMenus: "ok" });
    return true;
  } catch (e) {
    self._kbFormulaMenus = null;
    self.writeStatusFile({ kbFormulaMenus: "FAIL: " + e });
    return false;
  }
};

/** 往右键菜单里塞一项。⚠ 必须**同步** append（同工具栏那条规矩）。 */
ZoteroKB.kbFormulaAddMenuItem = function (event) {
  var self = ZoteroKB;
  if (!event || typeof event.append !== "function") return false;
  // 菜单名保持固定：不管能不能定位，先给入口，点了再由服务端说"没找到公式"。
  event.append({
    label: "知识库：修正公式",
    onCommand: function () { self.kbFormulaStart(event); },
  });
  return true;
};

/**
 * 从事件里**同步**取出「哪一篇、哪一页、哪块区域」。
 *
 * ⚠ 条目 key 必须取**顶层条目**：阅读器的 reader.itemID 是**附件**，
 *   附件 key 在知识库里永远查不到（本机踩过）。
 */
ZoteroKB.kbFormulaTargetInfo = function (event) {
  const reader = event && event.reader;
  const p = (event && event.params) || {};
  let key = "";
  try {
    const id = reader && reader.itemID;
    let item = id ? Zotero.Items.get(id) : null;
    if (item && item.parentID) item = Zotero.Items.get(item.parentID) || item;
    key = item && item.key ? item.key : "";
  } catch (e) { key = ""; }
  let page = 0;
  let rects = null;
  if (p.position && typeof p.position.pageIndex === "number") {
    page = p.position.pageIndex;
    rects = p.position.rects || null;
  }
  if (p.ids && p.ids.length) {
    try {
      const it = Zotero.Items.get(p.ids[0]);
      const pos = it && it.annotationPosition
        ? JSON.parse(it.annotationPosition) : null;
      if (pos) {
        if (typeof pos.pageIndex === "number") page = pos.pageIndex;
        rects = pos.rects || rects;
      }
    } catch (e2) { /* 用点击位置那份 */ }
  }
  return { key: key, page: page, rects: rects };
};

ZoteroKB.kbFormulaAlert = function (text) {
  var self = ZoteroKB;
  self.writeStatusFile({ kbFormulaLast: String(text).slice(0, 300) });
  let win = null;
  try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
  try {
    Services.prompt.alert(win, "知识库：修正公式", String(text));
    return;
  } catch (e2) { /* 退下一档 */ }
  try { if (win && win.alert) win.alert(String(text)); } catch (e3) { /* 只能写状态文件 */ }
};

/** 右键 → 定位 → 打开阅读器列 → 把 payload 送进页面。 */
ZoteroKB.kbFormulaStart = function (event) {
  var self = ZoteroKB;
  const info = self.kbFormulaTargetInfo(event);
  let win = null;
  try { win = Zotero.getMainWindow(); } catch (e) { win = null; }
  if (!win) return;
  if (!info.key) {
    self.kbFormulaAlert("这一条不在知识库里（取不到顶层条目 key）。\n"
      + "先给这篇建知识库，再来改公式。");
    return;
  }
  let url = "/formula-target?key=" + encodeURIComponent(info.key)
    + "&page=" + Math.max(0, info.page | 0);
  if (info.rects && info.rects.length) {
    url += "&rects=" + encodeURIComponent(JSON.stringify(info.rects));
  }
  self.writeStatusFile({ kbFormulaLast: "定位 " + info.key + " p" + info.page });
  // W3 线2.5（2026-10-14）：桥优先（`formula.target`，与 HTTP `GET /formula-target`
  // 同一份实现：rects 走查询串、是 JSON 字符串），拿不到再退回老 HTTP。
  const ask = (async function () {
    try { return await self.kbAdminHostRoute("GET", url); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥定位公式失败（退回 HTTP）：" + e0);
      return await self.request("GET", url);
    }
  })();
  ask.then(function (res) {
    if (!res || res.ok !== true) {
      self.kbFormulaAlert("没找到可改的公式："
        + ((res && (res.error || res.why)) || "本机服务没返回原因"));
      return;
    }
    if (!self.kbviewIsOpen(win, "reader")) self.kbviewOpen(win, "reader");
    self.kbFormulaSendWhenReady(win, res, 0);
  }).catch(function (e2) {
    self.kbFormulaAlert("连本机服务失败：" + e2);
  });
};

/** 页面可能还没就绪，轮询着送（最多 ~10 秒）。 */
ZoteroKB.kbFormulaSendWhenReady = function (win, payload, tries) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host && host.read;
  let sent = false;
  try { sent = dock ? self.kbviewSend(dock, { type: "formula", payload: payload }) : false; }
  catch (e) { sent = false; }
  if (sent) {
    self.writeStatusFile({ kbFormulaLast: "已送页面（" + (payload.key || "") + "）" });
    return;
  }
  if (tries > 40) {
    self.kbFormulaAlert("阅读器那一列没能就绪，没打开编辑区。\n"
      + "先在阅读器右侧打开知识库那一列（rail 上的图标），再右键公式。");
    return;
  }
  try {
    win.setTimeout(function () {
      self.kbFormulaSendWhenReady(win, payload, tries + 1);
    }, 250);
  } catch (e2) { /* 窗口没了就算了 */ }
};

/** 页面点了「应用」。 */
ZoteroKB.kbFormulaApply = function (win, kind, msg) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host ? (kind === "reader" ? host.read : host.lib) : null;
  const back = function (payload) {
    if (dock) self.kbviewSend(dock, payload);
  };
  // W3 线2.5（2026-10-14）：桥优先（`formula.apply`，**写路径**：写回该页第 ordinal 条
  // $$…$$ 并复用 doc.save 的重切重向量 —— 与 HTTP `POST /formula-apply` 同一份实现）。
  const payload = { key: msg.key, page: msg.page, latex: msg.latex,
                    ordinal: msg.ordinal, old_latex: msg.old_latex };
  const send = (async function () {
    try { return await self.kbAdminHostRoute("POST", "/formula-apply", payload); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥写回公式失败（退回 HTTP）：" + e0);
      return await self.request("POST", "/formula-apply", payload);
    }
  })();
  send.then(function (res) {
    if (!res || res.error) {
      back({ type: "formulaResult", ok: false,
             error: (res && res.error) || "写回失败" });
      return;
    }
    back({ type: "formulaResult", ok: true, job: res.job || "" });
    self.kbFormulaWaitJob(win, kind, res.job || "", 0);
  }).catch(function (e) {
    back({ type: "formulaResult", ok: false, error: String(e) });
  });
};

/** 页面点了「用 MathType 编辑」：服务端放剪贴板 + 启动 MathType。 */
ZoteroKB.kbFormulaMathType = function (win, kind, msg) {
  var self = ZoteroKB;
  const host = self.kbviewHost(win, false);
  const dock = host ? (kind === "reader" ? host.read : host.lib) : null;
  // W3 线2.5（2026-10-14）：桥优先（`formula.mathtype`：本机放剪贴板 + 起 MathType），
  // 失败退回老 HTTP。`dry_run` 只在自检里用，正常路径不传。
  const askMt = (async function () {
    try { return await self.kbAdminHostRoute("POST", "/formula-mathtype", { latex: msg.latex || "" }); }
    catch (e0) {
      Zotero.debug("[zotero-kb] 引擎桥起 MathType 失败（退回 HTTP）：" + e0);
      return await self.request("POST", "/formula-mathtype", { latex: msg.latex || "" });
    }
  })();
  askMt
    .then(function (res) {
      if (dock) {
        self.kbviewSend(dock, {
          type: "formulaMathTypeResult",
          ok: !!(res && res.ok), copied: !!(res && res.copied),
          launched: !!(res && res.launched),
          error: (res && res.why) || "",
        });
      }
    }).catch(function (e) {
      if (dock) {
        self.kbviewSend(dock, { type: "formulaMathTypeResult",
                                ok: false, error: String(e) });
      }
    });
};

/** 重建是异步任务，做完再刷新右侧正文。 */
ZoteroKB.kbFormulaWaitJob = function (win, kind, jobId, tries) {
  var self = ZoteroKB;
  if (!jobId) { self.kbviewPush(win, kind, null); return; }
  // W3 线2.5：轮询也走桥（`jobs.get`，与 HTTP `GET /jobs/<id>` 同形），失败退回 HTTP。
  const poll = (async function () {
    try { return await self.kbAdminHostRoute("GET", "/jobs/" + encodeURIComponent(jobId)); }
    catch (e0) {
      return await self.request("GET", "/jobs/" + encodeURIComponent(jobId));
    }
  })();
  poll.then(function (j) {
    const st = String((j && j.state) || "");
    if (st === "done" || st === "failed" || st === "cancelled" || tries > 120) {
      self.kbviewPush(win, kind, null);
      return;
    }
    win.setTimeout(function () {
      self.kbFormulaWaitJob(win, kind, jobId, tries + 1);
    }, 1000);
  }).catch(function () {
    win.setTimeout(function () {
      self.kbFormulaWaitJob(win, kind, jobId, tries + 1);
    }, 1500);
  });
};
