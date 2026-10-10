/**
 * 21-kbcols.js —— 文献列表里的两列：「已建知识库」「分节纲要」
 *
 * 用户的需求（原话）："在 Zotero 的文献列表里一眼看出这篇建过知识库没有、
 * 有没有分节纲要，方便管理（现在只能靠面板一页页翻）"。
 *
 * 数据来源：本地服务 `GET /col-status`（**一次全量**）。
 * ⚠ 插件里**不读** index.db / views 目录 —— token、知识库路径、迁移全在服务端，
 *   插件只认 HTTP（与 16-weightcol.js 同一套分工）。
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ============================================================ 两列：建库 / 纲要

  /**
   * 拉一次全量列状态放进内存缓存。
   *
   * 为什么必须"全量 + 缓存"：ItemTree 的 dataProvider 是**同步**的、每一行
   * 都会被调用 —— 不可能在里面发网络请求（几百行会把本地服务打爆）。
   * 所以这里一次把 key → {in_kb, outline_sections} 拉进来，
   * dataProvider 只查表（见 kbColValue）。
   *
   * 拿不到服务时**静默降级**：不弹窗、不抛错（文献列表是高频 UI），
   * 只 Zotero.debug 一句，并且**保留上一次的数据** —— 服务重启几秒钟，
   * 不该让两列一起变空。
   */
  refreshKbCols: async function () {
    var self = ZoteroKB;
    try {
      const res = await this.request("GET", "/col-status");
      if (!res || !res.ok || !res.items) {
        Zotero.debug("[zotero-kb] 拉列状态失败（保持原样）："
                     + ((res && (res.error || res.hint)) || "响应里没有 items"));
        return false;
      }
      const sig = JSON.stringify(res.items);
      self.colStatusCache = res.items;
      // ready 的含义是"手上这份缓存是**完整**的"：只有完整的一份，
      // "映射里查不到"才能解释成"这篇还没建库"。没 ready 时一律留空，
      // 免得把"服务没数据"画成"每条都没建库"（那是两句完全不同的话）。
      self.colStatusReady = true;
      if (sig === self.colStatusSig) return true;   // 内容没变就不重画（同 refreshWeights）
      self.colStatusSig = sig;
      Zotero.debug("[zotero-kb] 列状态已更新：" + Object.keys(res.items).length
                   + " 条（有纲要 " + (res.with_outline || 0) + " 条）");
      this.redrawItemTree();
      return true;
    } catch (e) {
      Zotero.debug("[zotero-kb] 拉列状态异常（保持原样）：" + e);
      return false;
    }
  },


  /**
   * 注册两列。列定义照 16-weightcol.js 那套写：
   *   · `flex: 1` 而不是固定 width（内置列绝大多数是 flex，固定宽度会让列
   *     在虚拟表格里定位不自然 —— 本机踩过，用户原话"不像正常加进去的列"）；
   *   · zoteroPersist 带上 width / hidden / sortDirection。
   *
   * ⚠ 关于 `sortable`：16 里写了 `sortable: true`，但**Zotero 不读这个字段**
   *   —— 列定义的 schema（xpcom/pluginAPI/itemTreeManager.js 的
   *   optionTypeDefinition）里根本没有它；表头点击走的是虚拟表格自己的
   *   `_handleHeaderMouseUp → onColumnSort`（components/virtualized-table.js），
   *   对**所有**列都开着（只有 staticColumns 那一档例外）。
   *   所以这里不写它：排序照样能用，而多写一个没人读的字段，只会让后来的人
   *   以为"排序是靠它开的"。
   */
  registerKbColumns: function () {
    try {
      if (!Zotero.ItemTreeManager || !Zotero.ItemTreeManager.registerColumn) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 ItemTreeManager，跳过两列");
        return;
      }
      const self = ZoteroKB;
      self.kbColKeys = [];
      const add = (opt) => {
        try {
          const k = Zotero.ItemTreeManager.registerColumn(opt);
          if (k) self.kbColKeys.push(k);
          else Zotero.debug("[zotero-kb] 列没注册上（可能已存在）：" + opt.dataKey);
        } catch (e) {
          // 单列失败不该影响另一列，更不该影响插件启动
          Zotero.debug("[zotero-kb] 注册列 " + opt.dataKey + " 失败：" + e);
        }
      };

      const common = {
        pluginID: self.id,
        enabledTreeIDs: ["main"],            // 只加到主列表（不加到 feed 等）
        // ⚠ 新列**必须**声明 defaultIn，否则注册成功却"不出现"（本机实测踩到）：
        //   虚拟表格 _getColumns() 里，只要**任何**一列带 defaultIn
        //   （内置的 title / firstCreator / hasAttachment 都带），
        //   新列就会被算成 hidden=true：
        //       if (hasDefaultIn && this.collectionTreeRows.length)
        //         column.hidden = !(column.defaultIn && matchesViewType(...))
        //   现象是：列注册了（kbColKeys 有值、`_columns` 里也有、表头 DOM 里却
        //   没有它），列表上完全看不见 —— 只能自己去右键"列"菜单里勾出来。
        //   ["*"] = 所有视图类型都默认显示（_matchesViewType 对 '*' 直接返回 true），
        //   与内置 title / hasAttachment 的写法一致。
        //   用户之后自己在列菜单里取消勾选，那个选择会存进 profile 的
        //   treePrefs.json，并在 _getColumns 里覆盖这一行 —— 不会被我们顶掉。
        //   （ItemTreeManager 会对 defaultIn 打一句"已废弃"的 debug 提示，但它与
        //    enabledTreeIDs 是两件事：后者管"进哪些列表"，前者管"默认显示不显示"
        //    —— 内置列至今仍在用它。）
        defaultIn: ["*"],
        flex: 1,
        minWidth: 70,
        zoteroPersist: ["width", "hidden", "sortDirection"],
      };
      add(Object.assign({}, common, {
        dataKey: "kbBuilt",
        label: "已建知识库",
        dataProvider: function (item) { return self.kbColValue(item, "built"); },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          return self.kbColCell(doc, column, data, "built");
        },
      }));
      add(Object.assign({}, common, {
        dataKey: "kbOutline",
        label: "分节纲要",
        dataProvider: function (item) { return self.kbColValue(item, "outline"); },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          return self.kbColCell(doc, column, data, "outline");
        },
      }));
      Zotero.debug("[zotero-kb] 已注册知识库列：" + (self.kbColKeys.join(", ") || "无"));
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /** 注销两列（插件卸载 / 禁用时调用，避免留下悬空列）。 */
  unregisterKbColumns: function () {
    var self = ZoteroKB;
    try {
      if (self.kbColKeys && self.kbColKeys.length && Zotero.ItemTreeManager
          && Zotero.ItemTreeManager.unregisterColumn) {
        self.kbColKeys.forEach((k) => {
          try { Zotero.ItemTreeManager.unregisterColumn(k); }
          catch (e) { /* 单列失败也要把另一列摘掉 */ }
        });
        Zotero.debug("[zotero-kb] 知识库两列已注销");
      }
    } catch (e) { Zotero.logError(e); }
    self.kbColKeys = null;
  },


  /**
   * 某一列某一行该显示什么（dataProvider 的实现，**同步、只查表**）。
   *
   * which = "built"   → "✓" / "✗"
   * which = "outline" → 补零的节数（如 "012"，见下面的说明）/ "✗"
   * 一次都没拉到过服务 → ""（留空）
   */
  kbColValue: function (item, which) {
    var self = ZoteroKB;
    try {
      if (!item || !item.key) return "";
      // 从没成功拉到过（服务没跑 / 知识库还没建）→ **留空**。
      // 不能画成"✗"：那等于替用户断言"这篇没建库"，而事实是"我不知道"。
      if (!self.colStatusReady) return "";
      const c = self.colStatusCache[item.key];
      const inKb = !!(c && c.in_kb);          // 完整映射里查不到 = 还没建库
      if (which === "built") return inKb ? "✓" : "✗";
      const n = (c && typeof c.outline_sections === "number")
        ? c.outline_sections : 0;
      if (!inKb || n <= 0) return "✗";
      // ⚠ 节数**补零**再交给 Zotero，否则排序是错的：
      //   自定义列的排序是"把 dataProvider 的字符串按本地化 collation 比"
      //   （itemTree.js 的 _compareField → _sortCollation.compareString），
      //   不补零时 "13" < "4"，点一下表头会看到 13 节排在 4 节前面。
      //   补零只影响排序键；给人看的文字在 kbColText 里把前导零去掉。
      return String(n).padStart(3, "0");
    } catch (e) {
      return "";
    }
  },


  /** 把列值变成给人看的文字（"012" → "12 节"）。 */
  kbColText: function (data, which) {
    if (data == null || data === "") return "";
    if (which !== "outline") return String(data);
    if (String(data) === "✗") return "✗";
    const n = parseInt(String(data).replace(/^0+/, ""), 10);
    return isNaN(n) ? String(data) : (n + " 节");
  },


  /**
   * renderCell：复刻 Zotero 的标准单元格结构（否则列会"不像正常的列"，
   * 本机踩过 —— 见 16-weightcol.js 里那段说明）：
   *   · className 必须含 `cell`（样式/对齐/内边距挂在这个类上）；
   *   · 用传进来的 document（拿不到才退回主窗口的）；
   *   · 空值也要返回元素，不能返回 null。
   *
   * ⚠ 文字外面**多套了一层 span**，这是为了提示能显示出来：
   *   虚拟表格的 _handleMouseOver（components/virtualized-table.js）会给
   *   "第一个子节点是文本"的单元格**清掉 title**（文字没被截断就
   *   removeAttribute('title')）。套一层之后，鼠标悬停在文字上时它清的是
   *   内层（内层没设 title），外层这个 title 才留得住。
   */
  kbColCell: function (doc, column, data, which) {
    var self = ZoteroKB;
    let doc2 = doc;
    if (!doc2) {
      try {
        doc2 = (Zotero.getMainWindow && Zotero.getMainWindow().document) || document;
      } catch (e) { doc2 = document; }
    }
    const span = doc2.createElement("span");
    // 自定义列的 column.className 可能是 undefined，兜个底
    span.className = "cell " + ((column && column.className) || "kb-col");
    const text = doc2.createElement("span");
    const shown = self.kbColText(data, which);
    text.textContent = shown;
    span.appendChild(text);
    if (!shown) return span;        // 没数据：留空，但保留标准结构
    span.style.fontVariantNumeric = "tabular-nums";
    if (which === "built") {
      const on = String(data).indexOf("✓") >= 0;
      span.style.color = on ? "#1a7f37" : "#9aa0a6";
      span.title = on ? "这篇已经在知识库里（切片、索引都在）"
                      : "这篇还不在知识库里";
    } else {
      const n = parseInt(String(data).replace(/^0+/, ""), 10);
      const has = !isNaN(n) && n > 0;
      span.style.color = has ? "#1f6feb" : "#9aa0a6";
      span.title = has
        ? "已生成 " + n + " 节分节纲要（中间层：比摘要详细、比全文短）"
        : "还没生成分节纲要 —— 在文献上右键 →「连接到本地模型」→"
          + "「生成分节纲要」（管理面板的「AI」页也有）";
    }
    return span;
  },
});
