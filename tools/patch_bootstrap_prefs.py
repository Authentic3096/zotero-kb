"""把 bootstrap.js 里的旧设置面板实现替换成官方 PreferencePanes 方式。

    python tools/patch_bootstrap_prefs.py

旧的 addToWindow/buildPrefsHTML/bindPrefsUI/removeFromWindow 有几个问题：
  1. addToWindow 不是 Zotero 的插件生命周期钩子，**根本不会被调用**；
  2. 它去找 `zotero-prefpane-advanced` 这个 DOM 元素注入，而 Zotero 7+ 的
     设置窗口不是那样组织的（该元素不存在）；
  3. 因此插件实际上**没有可用的设置界面** —— 用户没法配服务地址和 token。

改成 Zotero 官方的 `Zotero.PreferencePanes.register()`（签名见
chrome/content/zotero/preferences/preferences.js 的注释）：
    { id, pluginID, label, rawLabel, image, src, scripts, stylesheets, defaultXUL }
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOOTSTRAP = os.path.join(ROOT, "zotero-plugin", "bootstrap.js")

NEW_BLOCK = r'''  // ================================================================ 设置面板

  /**
   * 注册设置面板。用 Zotero 官方的 PreferencePanes（Zotero 7+）。
   *
   * 为什么不用之前那套：旧实现去 `document.getElementById("zotero-prefpane-advanced")`
   * 注入 DOM，而 Zotero 7+ 的设置窗口根本不是那个结构 —— 那个元素不存在，
   * 而且 `addToWindow` 也不在 Zotero 的插件生命周期里、压根不会被调用。
   * 结果就是插件装上了却没有可用的设置界面（用户没法填服务地址/token）。
   *
   * 现在把 settings.xhtml 交给 Zotero 自己渲染，交互逻辑放在 settings.js 里
   * （通过 scripts 选项加载，避免内联脚本被 CSP 拦掉）。
   */
  registerPrefPane: function () {
    try {
      if (!Zotero.PreferencePanes || !Zotero.PreferencePanes.register) {
        Zotero.debug("[zotero-kb] 这个 Zotero 版本没有 PreferencePanes，跳过设置面板");
        return;
      }
      // id 用固定串：重复 register 会报错，所以先按 id 让 Zotero 忽略重复
      Zotero.PreferencePanes.register({
        pluginID: this.id,
        id: "zotero-kb-prefpane",
        label: "文献知识库",
        rawLabel: "文献知识库",
        image: this.rootURI + "icon.svg",
        src: this.rootURI + "settings.xhtml",
        scripts: [this.rootURI + "settings.js"],
      });
      Zotero.debug("[zotero-kb] 设置面板已注册");
    } catch (e) {
      // 已注册过会抛错，属正常（Zotero 升级/重载时会走到）
      Zotero.debug("[zotero-kb] 注册设置面板： " + e);
    }
  },

  /** 把运行状态写到文件，方便从 Python 侧和排障时查看。 */
  writeStatusFile: function (extra) {
    try {
      const path = "D:\\DSHplugins\\zotero-kb\\kb\\plugin-status.json";
      const data = {
        pluginVersion: this.version,
        zoteroVersion: Zotero.version,
        platformVersion: Services.appinfo.platformVersion,
        server: this.getPref(this.PREFS.server, ""),
        model: this.getPref(this.PREFS.model, ""),
        autoProcess: !!this.getPref(this.PREFS.autoProcess, true),
        serverOk: this.serverOk,
        serverInfo: this.serverInfo,
        wroteAt: new Date().toISOString(),
      };
      if (extra) Object.assign(data, extra);
      const file = Zotero.File.pathToFile(path);
      Zotero.File.putContents(file, JSON.stringify(data, null, 2));
    } catch (e) {
      Zotero.debug("[zotero-kb] 写状态文件失败：" + e);
    }
  },
};
'''

# settings.js 与 xhtml 一起打进 xpi；面板逻辑
SETTINGS_JS = r'''// 设置面板的交互逻辑。由 bootstrap.js 通过 PreferencePanes 的 scripts 选项加载。
// 用 Zotero.Prefs 读写（与 bootstrap.js 里的键保持一致）。

const KB_PREFS = {
  server: "extensions.zotero-kb.server",
  token: "extensions.zotero-kb.token",
  model: "extensions.zotero-kb.model",
  categories: "extensions.zotero-kb.categories",
  autoProcess: "extensions.zotero-kb.autoProcess",
};

function kbInitPane(doc) {
  const root = doc.getElementById("zotero-kb-settings");
  if (!root) return;

  // 1) 把已存的值填进输入框
  root.querySelectorAll("[data-pref]").forEach((el) => {
    const key = el.getAttribute("data-pref");
    try {
      const v = Zotero.Prefs.get(key);
      if (el.type === "checkbox") el.checked = v !== false && v !== undefined
        ? !!v : true;
      else el.value = v == null ? "" : String(v);
    } catch (e) { /* 未设置过就留空 */ }
  });

  // 2) 改动即保存
  const save = (el) => {
    const key = el.getAttribute("data-pref");
    try {
      if (el.type === "checkbox") Zotero.Prefs.set(key, el.checked);
      else Zotero.Prefs.set(key, el.value.trim());
    } catch (e) { Zotero.debug("[zotero-kb] 保存设置失败：" + e); }
  };
  root.querySelectorAll("[data-pref]").forEach((el) => {
    el.addEventListener("change", () => save(el));
    el.addEventListener("blur", () => save(el));
  });

  // 3) 版本号
  const verEl = doc.getElementById("zotero-kb-version");
  if (verEl) {
    try {
      const addon = Zotero.Plugins.__proto__ && null;
      verEl.textContent = Zotero.ZoteroKB ? Zotero.ZoteroKB.version : "—";
    } catch (e) { verEl.textContent = "—"; }
  }

  // 4) 测试连接
  const btn = doc.getElementById("zotero-kb-test-btn");
  const status = doc.getElementById("zotero-kb-test-status");
  if (btn && status) {
    btn.addEventListener("click", async () => {
      root.querySelectorAll("[data-pref]").forEach(save);
      status.textContent = "测试中…";
      status.style.color = "#888";
      try {
        const ok = await Zotero.ZoteroKB.healthCheck();
        if (ok) {
          const info = Zotero.ZoteroKB.serverInfo || {};
          status.textContent = "✓ 连接成功（" + (info.service || "zotero-kb")
            + " v" + (info.version || "?") + "）";
          status.style.color = "#0a0";
        } else {
          status.textContent = "✗ 连不上：检查服务是否启动、地址与 token";
          status.style.color = "#c00";
        }
        Zotero.ZoteroKB.writeStatusFile();
      } catch (e) {
        status.textContent = "✗ 出错：" + e;
        status.style.color = "#c00";
      }
    });
  }

  // 5) 诊断信息
  const diag = doc.getElementById("zotero-kb-diag");
  if (diag) {
    try {
      const s = Zotero.ZoteroKB.serverInfo || {};
      diag.textContent = "知识库目录：" + (s.kb_dir || "(未连接)")
        + "\nZotero " + Zotero.version + " / Firefox " + Services.appinfo.platformVersion;
    } catch (e) { /* ignore */ }
  }
}

// Zotero 加载 scripts 时会执行本文件；面板插入后再找元素更稳妥
if (typeof Zotero !== "undefined") {
  try {
    const w = Zotero.getMainWindow();
    if (w) {
      setTimeout(() => { try { kbInitPane(w.document); } catch (e) {} }, 300);
    }
  } catch (e) { /* 面板可能还没插好，下次打开设置时会重跑 */ }
}
'''


def main() -> int:
    src = open(BOOTSTRAP, encoding="utf-8").read()
    # 定位旧块：从「首选项面板」注释到文件末尾的对象收尾 "};"
    marker = "  // ================================================================ 首选项面板"
    idx = src.find(marker)
    if idx < 0:
        print("找不到旧的首选项面板标记，可能已经改过了")
        return 1
    tail = src.rfind("};")
    if tail < idx:
        print("找不到对象收尾的 };")
        return 1
    new_src = src[:idx] + NEW_BLOCK
    # 备份
    bak = BOOTSTRAP + ".bak-before-prefs"
    open(bak, "w", encoding="utf-8").write(src)
    open(BOOTSTRAP, "w", encoding="utf-8").write(new_src)
    print(f"  已替换设置面板实现（{len(src)} → {len(new_src)} 字符）")
    print(f"  备份：{bak}")

    sj = os.path.join(ROOT, "zotero-plugin", "settings.js")
    open(sj, "w", encoding="utf-8").write(SETTINGS_JS)
    print(f"  已写入 {sj}")

    # startup 里要调用 registerPrefPane
    src2 = open(BOOTSTRAP, encoding="utf-8").read()
    if "registerPrefPane()" not in src2.split("registerPrefPane: function")[0]:
        src2 = src2.replace(
            "      this.registerPrefObserver();",
            "      this.registerPrefObserver();\n      this.registerPrefPane();",
            1)
        open(BOOTSTRAP, "w", encoding="utf-8").write(src2)
        print("  已在 startup 里调用 registerPrefPane()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
