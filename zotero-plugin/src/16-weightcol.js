/**
 * 16-weightcol.js —— 文献列表里的「知识库权重」自定义列
 *
 * ⚠ 这是**源码**：改完跑 tools/build_bootstrap.py 重新生成
 *   bootstrap.js（xpi 里装的是那个生成物）。
 */
Object.assign(ZoteroKB, {

  // ================================================================ 权重列

  /**
   * 在文献列表里加一列「知识库权重」。
   *
   * 关键约束：ItemTree 的 `dataProvider` 是**同步**的、每一行都会调用 ——
   * 不能在里面发网络请求（几百行会打爆本地服务）。所以做法是：
   *   · 启动时（以及手动刷新时）拉一次 `/weights` 全量映射放进内存；
   *   · dataProvider 只做字典查表，O(1)。
   *
   * 显示规则：
   *   · 不在知识库里的条目 → 显示 "—"
   *   · 在库、权重为 1.0（没被经验加权）→ 显示 "1.0"，不加粗
   *   · 权重 > 1（有经验/被标重点）→ 加粗显示，★ 表示标了重点
   * 这样一眼能看出"哪些文献是被验证过的"。
   */
  registerWeightColumn: function () {
    try {
      if (!Zotero.ItemTreeManager || !Zotero.ItemTreeManager.registerColumn) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 ItemTreeManager，跳过权重列");
        return;
      }
      const self = ZoteroKB;
      // 诊断计数：搞清"列显示了但没值"到底是
      //   A. dataProvider 压根没被调用（providerCalls=0），还是
      //   B. 被调用了但 key 对不上（calls 多、hits 0）
      self.colDiag = { calls: 0, hits: 0, sample: [] };

      const key = Zotero.ItemTreeManager.registerColumn({
        dataKey: "kbWeight",
        label: "知识库权重",
        pluginID: self.id,
        // 只加到主列表（不加到 feed 等）
        enabledTreeIDs: ["main"],
        // 用 flex 而不是固定 width —— Zotero 内置列绝大多数是 flex: 1
        // （见 itemTreeColumns.js：title flex:4、firstCreator flex:1、date flex:1 …），
        // 只有极少数窄列用固定 width。之前我用 width:"96" 会让列在
        // 虚拟表格里定位不自然（用户反馈"不像正常加进去的列"）。
        flex: 1,
        minWidth: 70,
        sortable: true,
        zoteroPersist: ["width", "hidden", "sortDirection"],
        dataProvider: function (item) {
          try {
            const d = self.colDiag;
            if (d) d.calls++;
            if (!item) return "";
            const k = item.key;          // Zotero 条目的 8 位 key，缓存以它做键
            if (!k) return "";
            const w = self.weightsCache[k];
            if (!w) {
              if (d && d.sample.length < 5) d.sample.push("miss:" + k);
              return "";
            }
            if (d) {
              d.hits++;
              if (d.sample.length < 5) d.sample.push("hit:" + k + "=" + w.weight);
            }
            const num = (typeof w.weight === "number") ? w.weight : 1;
            if (Math.abs(num - 1) < 0.005 && !w.pinned) return "1.0";
            // ⚠ 星号放在**数字后面**并留一个空格。
            //   原来写成 "★" + 数字（"★2.61"），用户反馈：
            //   "重点标记也在右键中菜单中显示标记，用的星在权重后面空一点位置显示"
            //   —— 数字在前更符合"权重值 + 一个标记"的读法，
            //   而且列表里所有数值左对齐，数字都在同一列上，扫起来整齐。
            return num.toFixed(2) + (w.pinned ? "  ★" : "");
          } catch (e) {
            return "";
          }
        },
        renderCell: function (index, data, column, isFirstColumn, doc) {
          // ⚠ 必须复刻 Zotero 的标准单元格结构，否则列会"不像正常的列"
          //   （本机踩过：自己造了个裸 <span>，丢了 Zotero 的单元格样式）：
          //     · className 必须含 `cell`（样式/对齐/内边距挂在这个类上）
          //     · 用传入的 document（别用全局 document）
          //     · 空值也要返回元素，不能返回 null
          //   标准实现见 Zotero 的 components/virtualized-table.js：
          //     let span = document.createElement('span');
          //     span.className = `cell ${column.className}`;
          //     span.textContent = data;
          //     if (dir) span.dir = dir;
          const doc2 = doc || (Zotero.getMainWindow ? Zotero.getMainWindow().document
                                                    : document);
          const span = doc2.createElement("span");
          // 自定义列的 column.className 可能是 undefined，兜个底
          span.className = "cell " + ((column && column.className) || "kb-weight");
          span.textContent = data == null ? "" : data;
          if (!data) return span;        // 不在知识库：留空，但保留标准结构
          span.style.fontWeight = "600";
          span.style.fontVariantNumeric = "tabular-nums";
          // 颜色按权重分级 —— 一眼看出"哪些文献是被验证过的"
          //   重点（橙）> 3.0（红）> 2.0（蓝）> 1.0 以上（青）> 1.0（灰）
          // ⚠ 星号现在在**数字后面**（"2.61  ★"），所以判重点要用 contains
          //   而不是 startsWith —— 改格式时这里最容易漏（漏了后果是
          //   重点文献不再显示橙色，看着像"标记丢了"）。
          const pinned = String(data).indexOf("★") >= 0;
          const num = parseFloat(String(data).replace(/[^\d.]/g, "")) || 1;
          let color;
          if (pinned) color = "#e8890c";          // 橙色：标过的重点
          else if (num >= 3.0) color = "#c0392b"; // 红：经验加权最多
          else if (num >= 2.0) color = "#1f6feb"; // 蓝：有明确经验
          else if (num > 1.005) color = "#0e8a7d";// 青：轻微加权
          else color = "#9aa0a6";                 // 灰：没被经验加权
          span.style.color = color;
          span.title = "知识库权重 " + data
            + "（>1 表示被使用经验加权过；★ 是标为重点）";
          return span;
        },
      });
      if (key) {
        this.weightColumnKey = key;
        Zotero.debug("[zotero-kb] 权重列已注册：" + key);
      } else {
        Zotero.debug("[zotero-kb] 权重列注册失败（可能已存在）");
      }
    } catch (e) {
      Zotero.logError(e);
    }
  },


  /**
   * 让文献列表重画（权重变化后星号/颜色要立刻更新）。
   *
   * ⚠ 这里踩过一个**静默失效**的坑（用户报"标了重点但不显示星、权重也没变"）：
   *   原来写的是
   *       if (Zotero.ItemTreeManager && Zotero.ItemTreeManager.refresh) {
   *         Zotero.ItemTreeManager.refresh();
   *       }
   *   而 `Zotero.ItemTreeManager` **根本没有 `refresh` 方法** —— 它只有
   *   `registerColumn` / `unregisterColumn` / `refreshColumns` /
   *   `getCustomColumns` / `isCustomColumn` / `getCustomCellData`
   *   （读 omni.ja 的 xpcom/pluginAPI/itemTreeManager.js 确认）。
   *   于是那个 `if` **永远为假**，列表从来没重画过 ——
   *   而外层还有 `catch {}`，所以连个报错都没有。
   *
   * 正确的重画入口是**虚拟化表格自己**：
   *   `ZoteroPane.itemsView.tree.invalidate()`
   * （Zotero 内部就是这么干的，见 itemTree.js 的 `this.tree.invalidate()`）。
   * 它是"重画当前可见行"、不重新查库，正好适合"数据没变、只是显示值变了"。
   */
  redrawItemTree: function () {
    const done = [];
    try {
      const pane = (typeof Zotero.getActiveZoteroPane === "function"
                    && Zotero.getActiveZoteroPane())
        || (Zotero.getMainWindow && Zotero.getMainWindow().ZoteroPane);
      const tree = pane && pane.itemsView && pane.itemsView.tree;
      if (tree && typeof tree.invalidate === "function") {
        tree.invalidate();
        done.push("tree.invalidate");
      }
      // 更彻底一层：有些情况下（列刚注册、列表还没建）需要整体刷新。
      // 它是 async 的，这里不 await —— 重画是"尽力而为"，不该挡住调用方。
      if (pane && pane.itemsView
          && typeof pane.itemsView.refreshAndMaintainSelection === "function") {
        Promise.resolve(pane.itemsView.refreshAndMaintainSelection())
          .catch(() => {});
        done.push("refreshAndMaintainSelection");
      }
    } catch (e) {
      Zotero.debug("[zotero-kb] 重画列表失败：" + e);
    }
    // 列定义也刷一下：万一权重列是这次才注册上的（启动竞态）
    try {
      if (Zotero.ItemTreeManager
          && typeof Zotero.ItemTreeManager.refreshColumns === "function") {
        Zotero.ItemTreeManager.refreshColumns();
        done.push("refreshColumns");
      }
    } catch (e) { /* ignore */ }
    Zotero.debug("[zotero-kb] 已请求重画列表：" + (done.join(" + ") || "无可用入口"));
    return done;
  },


  /** 拉取全量权重映射填进内存缓存（供同步的 dataProvider 查表）。 */
  refreshWeights: async function () {
    try {
      let res = await this.request("GET", "/weights");
      if (!res || !res.weights) {
        // 拿不到（服务刚重启、token 变了、服务刚被拉起还没就绪）→
        // 补问一次 /health，它会顺手把新 token 同步进 pref，再试一次。
        await this.healthCheck();
        res = await this.request("GET", "/weights");
      }
      if (res && res.weights) {
        // ⚠ **内容没变就不重画**。权重现在是每几秒拉一次，若无条件
        //   `redrawItemTree()`，列表会周期性重画 —— 用户正在滚动或
        //   多选时会被打断，看着像"列表在抖"。
        const sig = JSON.stringify(res.weights);
        this.weightsCache = res.weights;
        if (sig === this.weightsSig) return true;
        this.weightsSig = sig;
        Zotero.debug("[zotero-kb] 权重缓存已更新："
                     + Object.keys(res.weights).length + " 条");
        this.redrawItemTree();
        return true;
      }
    } catch (e) {
      Zotero.debug("[zotero-kb] 拉权重失败：" + e);
    }
    return false;
  },


  /** 注销权重列（插件卸载/禁用时调用，避免留下悬空列）。 */
  unregisterWeightColumn: function () {
    try {
      if (this.weightColumnKey && Zotero.ItemTreeManager
          && Zotero.ItemTreeManager.unregisterColumn) {
        Zotero.ItemTreeManager.unregisterColumn(this.weightColumnKey);
        Zotero.debug("[zotero-kb] 权重列已注销");
      }
    } catch (e) { Zotero.logError(e); }
    this.weightColumnKey = null;
  },
});
