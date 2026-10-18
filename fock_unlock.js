// -*- coding: utf-8 -*-
/**
 * fock_unlock.js —— QQ阅读 VIP 章 Fock 解密器（Node 侧）
 *
 * 用法：
 *   node fock_unlock.js <input.json> <output.json>
 *
 * input.json:
 *   {
 *     "glue": "lib/fock_glue.js",               // Fock glue（wasm2js，纯 JS）
 *     "content": "<base64 密文>",               // /api/book/read 的 data.content
 *     "cid": "22",                              // 章节号字符串（注意：不是 ccid！）
 *     "fuid": "<你的fuid>",                     // fkConfig.fuid（32 位十六进制）
 *     "key": "<128 字符 base64>",               // fkp 里的 onkeyfocus 第一参
 *     "ts": 1642763424                          // fkp 里的 onkeyfocus 第二参
 *   }
 *
 * output.json:
 *   {"ok": true, "text": "<解密后的 HTML>"} 或 {"ok": false, "error": "..."}
 *
 * 【为什么可以在 Node 里跑】
 * lib/fock_glue.js 是 Emscripten 的 wasm2js 产物（WebAssembly 出现 0 次，
 * instantiate 被替换成纯 JS shim），因此没有任何 .wasm 二进制需要加载。
 *
 * 【必须的三处环境适配】
 * 1. setTimeout/setInterval 遇字符串回调（反调试 "debugger"）直接返回 0，
 *    否则 Node 抛 ERR_INVALID_ARG_TYPE 并中断 _main 初始化。
 * 2. import 表 13820 是反分析探针（执行 window.document.body.<随机名>(...) +
 *    window.eval），Node 里必须 patch 成不抛异常的版本。
 * 3. DOM 用 Proxy 造宽松桩，但**不能加 has 陷阱**（否则 `p in o` 恒真）。
 */
'use strict';
const fs = require('fs');
const path = require('path');

function main() {
  const [inPath, outPath] = process.argv.slice(2);
  if (!inPath || !outPath) {
    console.log(JSON.stringify({ ok: false, error: 'usage: node fock_unlock.js <in.json> <out.json>' }));
    process.exit(1);
  }
  const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
  const gluePath = path.resolve(inp.glue || path.join(__dirname, 'lib', 'fock_glue.js'));
  const logs = [];
  const say = (...a) => logs.push(a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '));

  // patched glue 是运行时临时产物（191KB/次），必须退出前删掉。
  // 在 finish 里引用它，因此先用 let 声明（避免 const 的 TDZ）。
  let patchedPath = null;

  // 兜底清理：历史版本不删这些文件，会按 pid 累积（曾攒到 495 个 / 90MB）。
  // 放在启动时顺手清理同目录下的陈旧副本，失败忽略（可能被其他进程占用）。
  try {
    const outDir = path.dirname(path.resolve(outPath));
    for (const f of fs.readdirSync(outDir)) {
      if (f.startsWith('_fock_patched_') && f.endsWith('.js')) {
        try { fs.unlinkSync(path.join(outDir, f)); } catch (_) { /* 忽略 */ }
      }
    }
  } catch (_) { /* 目录不可读则跳过 */ }

  let finished = false;
  const finish = (obj) => {
    if (finished) return;
    finished = true;
    if (patchedPath) {
      try { fs.unlinkSync(patchedPath); } catch (_) { /* 已被清理过 */ }
    }
    obj.log = logs.slice(-40);
    fs.writeFileSync(outPath, JSON.stringify(obj), 'utf8');
    process.exit(obj.ok ? 0 : 2);
  };

  // ---------- 1) 环境适配 ----------
  const _st = globalThis.setTimeout, _si = globalThis.setInterval;
  globalThis.setTimeout = function (fn, ms, ...r) {
    return typeof fn === 'string' ? 0 : _st(fn, Math.min(Number(ms) || 0, 5000), ...r);
  };
  globalThis.setInterval = function (fn, ms, ...r) {
    return typeof fn === 'string' ? 0 : _si(fn, ms, ...r);
  };

  function perm(name) {
    const t = { __name: name, style: {}, classList: { add() {}, remove() {} }, dataset: {} };
    const c = new Map();
    return new Proxy(t, {
      get(o, p) {
        if (typeof p === 'symbol') return o[p];
        if (Object.prototype.hasOwnProperty.call(o, p)) return o[p];
        if (p === 'then') return undefined;
        if (!c.has(p)) {
          c.set(p, function () { return perm(name + '.' + String(p) + '()'); });
        }
        return c.get(p);
      },
      set(o, p, v) { o[p] = v; return true; },
    });
  }

  globalThis.window = globalThis;
  globalThis.self = globalThis;
  // 不要去改写全局 navigator：Node 20+ 里它是只读 getter，且原生 navigator
  // （有 userAgent/platform/languages 等真值）正是 glue 能正常跑完 _main 的前提。
  // 早期探针脚本在非严格模式下赋值静默失败，反而保留了原生 navigator 才成功。
  if (!globalThis.navigator || !globalThis.navigator.userAgent) {
    try {
      Object.defineProperty(globalThis, 'navigator', {
        value: {
          userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0',
          platform: 'Win32', language: 'zh-CN', languages: ['zh-CN', 'zh'],
          webdriver: false, hardwareConcurrency: 8, maxTouchPoints: 0,
        },
        writable: true, configurable: true, enumerable: true,
      });
    } catch (e) { /* 保持 Node 原生即可 */ }
  }
  globalThis.location = {
    href: 'https://book.qq.com/book-read/0/0/', protocol: 'https:',
    host: 'book.qq.com', hostname: 'book.qq.com', origin: 'https://book.qq.com',
  };
  globalThis.document = {
    documentMode: undefined,
    currentScript: null,
    head: perm('head'),
    body: perm('body'),
    documentElement: perm('documentElement'),
    createElement: (t) => perm('el:' + t),
    createTextNode: () => perm('text'),
    getElementsByTagName: () => [],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    cookie: '',
  };
  globalThis.fetch = undefined;          // 逼 glue 走同步解码分支
  globalThis.XMLHttpRequest = function () {};
  globalThis.say = say;

  // ---------- 2) 打补丁 ----------
  let src = fs.readFileSync(gluePath, 'utf8');
  const PATCH_ANCHOR =
    'var F={13820:function(A,r,n,t,e){return window.document.documentMode?window[B(e)](B(t)):window[B(A)][B(r)][B(n)](B(t))},';
  if (src.indexOf(PATCH_ANCHOR) < 0) {
    finish({ ok: false, error: 'glue patch anchor not found (glue 版本可能变了)' });
    return;
  }
  src = src.replace(PATCH_ANCHOR,
    'var F={13820:function(A,r,n,t,e){try{if(window.document.documentMode)return window[B(e)](B(t));var _o=window[B(A)][B(r)][B(n)];return "function"==typeof _o?_o(B(t)):0}catch(_e){return 0}},');
  // 必须用绝对路径：require 相对路径是按本模块目录解析的
  const patched = path.resolve(path.dirname(outPath), '_fock_patched_' + process.pid + '.js');
  patchedPath = patched;
  fs.writeFileSync(patched, 'var say=globalThis.say||function(){};\n' + src, 'utf8');

  process.on('uncaughtException', (e) => {
    // 反调试/环境缺失导致的异常不影响主流程，记录后继续
    say('[uncaught]', String(e && e.message));
  });

  let Fock;
  try {
    Fock = require(patched);
  } catch (e) {
    finish({ ok: false, error: 'require glue failed: ' + String(e && e.message) });
    return;
  }
  if (typeof Fock !== 'function' && !(Fock && Fock.setupUserKey)) {
    finish({ ok: false, error: 'Fock export missing, got ' + typeof Fock });
    return;
  }

  // ---------- 3) 执行 ----------
  // 两种模式：
  //   单章：inp.content + inp.cid          → {ok, text}
  //   批量：inp.items = [{id, cid, content}] → {ok, results:[{id, ok, text|error}]}
  // 批量模式一次 Node 进程连续解多章，避免每章 spawn 一次（约 2s/次）。
  const batch = Array.isArray(inp.items) && inp.items.length > 0;
  let items;
  if (batch) {
    items = inp.items.map((it, i) => ({
      id: it.id === undefined || it.id === null ? String(i) : String(it.id),
      cid: String(it.cid),
      content: it.content,
    }));
    const bad = items.findIndex((it) => !it.content || !it.cid);
    if (bad >= 0) { finish({ ok: false, error: 'items[' + bad + '] missing content/cid' }); return; }
  } else {
    if (!inp.content) { finish({ ok: false, error: 'missing content' }); return; }
    items = [{ id: String(inp.cid), cid: String(inp.cid), content: inp.content }];
  }
  if (!inp.fuid) { finish({ ok: false, error: 'missing fuid' }); return; }
  if (!inp.key || inp.ts == null) { finish({ ok: false, error: 'missing key/ts' }); return; }

  try {
    Fock.setupUserKey(inp.fuid);
    say('[setup] ok');
  } catch (e) {
    finish({ ok: false, error: 'setupUserKey failed: ' + String(e && e.message) });
    return;
  }

  const results = [];
  let cur = 0;

  const nextOne = () => {
    if (cur >= items.length) {
      if (batch) {
        finish({ ok: true, results });
      } else {
        const r0 = results[0] || {};
        if (r0.ok) finish({ ok: true, text: r0.text });
        else finish({ ok: false, error: r0.error || 'unlock failed' });
      }
      return;
    }
    const it = items[cur++];
    let done = false;
    const t0 = Date.now();
    const guard = setTimeout(() => {
      if (done) return;
      done = true;
      results.push({ id: it.id, cid: it.cid, ok: false, error: 'timeout' });
      say('[unlock]', it.cid, 'timeout');
      setTimeout(nextOne, 0);
    }, 20000);

    try {
      Fock.unlock(it.content, it.cid, function (code, txt) {
        if (done) return;
        done = true;
        clearTimeout(guard);
        if (code !== 0) {
          results.push({ id: it.id, cid: it.cid, ok: false, error: 'unlock code=' + code });
          say('[unlock]', it.cid, 'code', code);
        } else if (!txt || !txt.length) {
          results.push({ id: it.id, cid: it.cid, ok: false, error: 'empty (cid 必须是章节号而非 ccid)' });
          say('[unlock]', it.cid, 'empty');
        } else {
          results.push({ id: it.id, cid: it.cid, ok: true, text: txt, ms: Date.now() - t0 });
        }
        setTimeout(nextOne, 0);
      });
    } catch (e) {
      if (done) return;
      done = true;
      results.push({ id: it.id, cid: it.cid, ok: false, error: 'unlock threw: ' + String(e && e.message) });
      setTimeout(nextOne, 0);
    }
  };

  // setupUserKey 会异步实例化 wasm2js 模块，必须等它就绪（Es 标志置真）再灌密钥。
  // 实测 300ms 不够，2500ms 稳定。
  setTimeout(() => {
    say('[phase] typeof window.onkeyfocus =', typeof window.onkeyfocus);
    try {
      window.onkeyfocus(inp.key, Number(inp.ts));
      say('[onkeyfocus] ok');
    } catch (e) {
      say('[onkeyfocus ERR]', String(e && e.message));
    }
    // 密钥灌入后给 WASM 一点时间完成 fock_a，再开始逐章解锁
    setTimeout(nextOne, 600);
  }, 2500);

  const globalMs = 30000 + items.length * 15000;
  setTimeout(() => {
    if (!finished) {
      if (batch) finish({ ok: true, results, error: 'global timeout, partial' });
      else finish({ ok: false, error: 'timeout waiting for unlock callback' });
    }
  }, globalMs);
}

main();
