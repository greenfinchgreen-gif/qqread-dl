# -*- coding: utf-8 -*-
"""qqread_vip.py —— QQ阅读 VIP 章节协议层（第 3 层，自包含）。

【这一层解决什么】
main.py 原先只下免费章，遇到 free!=1 就停。VIP 版要下账号**有权阅读**的章节，
而那些章节的正文是双重加密的：

  第 1 层  Fock 加密        data.encrypt == true
           content 是 base64 密文，必须用 fkConfig 里的 (fkp 密钥, fuid, cid)
           在 Fock 的 wasm2js 内核里解 → Node 侧 fock_unlock.js 负责。

  第 2 层  字体反爬         data.fontEncrypt == true
           解出来的 HTML 里字符是伪码位，必须按 fontResponse 还原
           → 由 qqread_font.py 负责。

【本模块的职责】
  * 会话 Cookie 加载（session_cookies.json，多种格式兼容）
  * 纯标准库 urllib 发请求（实测不需要 curl_cffi 的 TLS 指纹伪装）
  * 拉 SSR 阅读页拿 fkp 密钥（全局固定，取一次缓存到磁盘）
  * 调 Node 跑 fock_unlock.js 做 Fock 解密
  * 判定「有权限 / 无权限」，无权限就停，绝不尝试绕过付费

【权限边界（重要）】
只能下载账号本身有权阅读的章节。判定依据是服务端返回的 authStatus：
  authStatus=1 + encrypt=true   → 有权限，正常解密下载
  authStatus=0 + content 203 字  → 无权限（未购买/未订阅），立即停止整本
本模块不实现任何绕过付费的手段。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qqread_config as cfg  # noqa: E402  凭据/密钥隔离层

OUTPUT = os.path.join(HERE, "output")
# 运行期临时文件（Fock 输入输出 JSON）放在 cache/：都是可重建的中间产物，
# 不该混进源码目录，更不该留在仓库里。
TMP_DIR = cfg.cache_dir("tmp")

BASE = "https://book.qq.com"
API = BASE + "/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0")

# 凭据不落在项目目录：加载逻辑全部委托给 qqread_config（见该模块文档）。
# Fock 胶水是解密必需的二进制产物，随仓库分发，放在 lib/（不可重建，必须入库）
FOCK_GLUE = os.path.join(HERE, "lib", "fock_glue.js")
FOCK_JS = os.path.join(HERE, "fock_unlock.js")
# 批量模式复用同一个脚本（见 fock_unlock.js 的 inp.items 分支），不需要第二个文件
FOCK_JS_BATCH = FOCK_JS

TIMEOUT = 30
RETRIES = 3
RETRY_WAIT = 1.5

# 无权限/付费的哨兵：试读固定截断到 203 字
TRIAL_LEN = 300


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 会话 Cookie


def load_cookies(path: str | None = None) -> dict:
    """读取会话 Cookie（委托给 qqread_config 的隔离加载层）。

    兼容三种格式：
      1) {"uid": "<你的uid>", "fuid": "<你的fuid>", "usid": "<你的usid>"}  —— 扁平字典
      2) {"cookie": "a=b; c=d"}                              —— Cookie 串
      3) {"cookies": [{"name": "...", "value": "..."}, ...]} —— Playwright storage_state
    凭据来源优先级见 qqread_config 模块文档；找不到返回空 dict
    （= 匿名模式，只能下免费章）。项目目录内不保存真实凭据。
    """
    if path:
        # 显式指定路径时只读该文件（保持旧调用方式可用）
        if not os.path.exists(path):
            _log(f"  [会话] 指定凭据文件不存在：{path}")
            return {}
        raw = json.load(open(path, encoding="utf-8"))
        jar = cfg.parse_credentials(raw)
        if jar:
            return jar
        _log(f"  [会话] {os.path.basename(path)} 内容无法解析为 Cookie")
        return {}
    return cfg.load_credentials(verbose=True)


# ---------------------------------------------------------------- HTTP


class Client:
    """最小 HTTP 客户端：标准库 urllib + 会话 Cookie。"""

    def __init__(self, cookies: dict | None = None) -> None:
        self.cookies = dict(cookies or {})

    def _headers(self, referer: str = "https://book.qq.com/") -> dict:
        h = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        }
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET /api/... 返回 body JSON；HTTP 非 200 或网络异常重试。"""
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = r.read().decode("utf-8", "replace")
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                j = json.loads(body)
                if not isinstance(j, dict) or "code" not in j:
                    raise RuntimeError(f"响应结构异常: {body[:160]}")
                return j
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < RETRIES:
                    time.sleep(RETRY_WAIT * attempt)
        raise RuntimeError(f"请求失败 {path}: {last}")

    def get_text(self, url: str, referer: str = "https://book.qq.com/") -> str:
        req = urllib.request.Request(url, headers={**self._headers(referer),
                                                   "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")

    def get_bytes(self, url: str, referer: str = "https://book.qq.com/") -> bytes:
        """下载二进制资源（字体等）。fixedFont 不走 /api，是独立 CDN。"""
        req = urllib.request.Request(url, headers={**self._headers(referer),
                                                   "Accept": "*/*"})
        for attempt in range(1, RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return r.read()
            except Exception:  # noqa: BLE001
                if attempt >= RETRIES:
                    raise
                time.sleep(RETRY_WAIT * attempt)
        raise RuntimeError("unreachable")

    # ---- 业务接口 ----
    def homepage(self) -> dict:
        j = self.get_json("user/homepage")
        return j.get("data") or {}

    def detail(self, bid: str) -> dict:
        j = self.get_json("book/detail", {"bid": bid})
        if j.get("code") != 0:
            raise RuntimeError(f"详情失败 code={j.get('code')}")
        return j.get("data") or {}

    def chapters(self, bid: str) -> list[dict]:
        j = self.get_json("book/detail/chapters", {"bid": bid})
        if j.get("code") != 0:
            raise RuntimeError(f"目录失败 code={j.get('code')}")
        return j.get("data") or []

    def read(self, bid: str, cid: int) -> dict:
        j = self.get_json("book/read", {"bid": bid, "cid": cid})
        if j.get("code") != 0:
            raise RuntimeError(f"正文失败 code={j.get('code')}")
        return j.get("data") or {}


# ---------------------------------------------------------------- fkp 密钥


def fetch_fkp(client: Client, bid: str, cid: int = 1) -> dict:
    """从 SSR 阅读页抓 fkConfig（fuid + fkp 密钥 + 时间戳）。

    实测该密钥全局固定（跨 cid 完全一致），所以整个下载过程只需取一次。
    返回 {"fuid": str, "key": str, "ts": int}。

    缓存写到项目外的凭据目录（%USERPROFILE%\\.qqread\\fkp_cache.json 等），
    绝不落在项目里 —— 否则密钥会随 git 提交泄漏。
    """
    cached = cfg.load_fkp_cache()
    if cached:
        return cached

    url = f"{BASE}/book-read/{bid}/{cid}/"
    html = client.get_text(url)
    m = re.search(r'fkp\s*:\s*"([A-Za-z0-9+/=]+)"', html)
    if not m:
        raise RuntimeError("SSR 页面里没找到 fkp 密钥（可能需要登录态）")
    import base64
    decoded = base64.b64decode(m.group(1)).decode("utf-8", "replace")
    # 形如：window.onkeyfocus("3W/1i...", 1642763424)
    mm = re.search(r'onkeyfocus\(\s*"([^"]+)"\s*,\s*(\d+)\s*\)', decoded)
    if not mm:
        raise RuntimeError(f"fkp 内容无法解析: {decoded[:120]}")
    fuid_m = re.search(r'fuid\s*:\s*"([^"]+)"', html)
    out = {"fuid": fuid_m.group(1) if fuid_m else client.cookies.get("fuid", ""),
           "key": mm.group(1), "ts": int(mm.group(2))}
    cfg.save_fkp_cache(out)
    # 密钥本身绝不进日志，只记录长度与时间戳
    _log(f"  [fkp] 已取得密钥（fuid={cfg.mask(out['fuid'])} "
         f"key=len{len(out['key'])} ts={out['ts']}）")
    return out


# ---------------------------------------------------------------- Fock 解密


def find_node() -> str:
    """定位 node 可执行文件。

    不硬编码任何机器专属路径（开源项目里写死开发者的安装目录会暴露隐私、
    也让别人跑不起来）。查找顺序：
      1. 环境变量 NODE_EXE（显式指定）
      2. PATH 里的 node
      3. 常见安装位置（Windows / macOS / Linux）
    """
    from shutil import which

    env_node = os.environ.get("NODE_EXE")
    if env_node:
        if os.path.isabs(env_node) and os.path.exists(env_node):
            return env_node
        w = which(env_node)
        if w:
            return w
        _log(f"  [Node] 环境变量 NODE_EXE 指向的路径不可用：{env_node}")

    w = which("node") or which("node.exe")
    if w:
        return w

    import glob
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        "/usr/local/bin/node",
        "/usr/bin/node",
        "/opt/homebrew/bin/node",
    ]
    # 兼容用 nvm-windows 的用户（只匹配版本目录，不涉及任何个人标识）
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if up:
        candidates += sorted(glob.glob(os.path.join(
            up, "AppData", "Roaming", "nvm", "v*", "node.exe")), reverse=True)
    for p in candidates:
        if os.path.exists(p):
            return p

    raise RuntimeError(
        "未找到 node 可执行文件；VIP 章解密需要 Node.js。\n"
        "  安装：https://nodejs.org/  （或 winget install OpenJS.NodeJS）\n"
        "  已装但找不到时，设置环境变量 NODE_EXE 指向 node.exe")


class FockUnlocker:
    """调 Node 侧 fock_unlock.js 做 Fock 解密（进程级缓存密钥与胶水文件）。"""

    def __init__(self, fkp: dict) -> None:
        self.fuid = fkp["fuid"]
        self.key = fkp["key"]
        self.ts = int(fkp["ts"])
        self.node = find_node()
        if not os.path.exists(FOCK_GLUE):
            raise RuntimeError(f"缺少 Fock 胶水文件: {FOCK_GLUE}")

    def unlock(self, content_b64: str, cid: int, timeout: int = 90) -> str:
        """解密单章 → HTML 字符串。cid 必须是章节号，不是 ccid。

        多线程安全：临时文件名带 uuid，避免 5 个线程同 pid 互相覆盖。
        """
        inp = {"glue": FOCK_GLUE, "content": content_b64, "cid": str(cid),
               "fuid": self.fuid, "key": self.key, "ts": self.ts}
        tag = f"{os.getpid()}_{uuid.uuid4().hex[:10]}"
        tmp_in = os.path.join(TMP_DIR, f"_fock_io_{tag}.in.json")
        tmp_out = os.path.join(TMP_DIR, f"_fock_io_{tag}.out.json")
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(tmp_in, "w", encoding="utf-8") as f:
            json.dump(inp, f, ensure_ascii=False)
        try:
            proc = subprocess.run([self.node, FOCK_JS, tmp_in, tmp_out],
                                  capture_output=True, timeout=timeout)
            if not os.path.exists(tmp_out):
                raise RuntimeError(f"Node 无输出: {proc.stderr.decode('utf-8', 'replace')[:300]}")
            res = json.load(open(tmp_out, encoding="utf-8"))
            if not res.get("ok"):
                raise RuntimeError(f"Fock 解密失败: {res.get('error')}")
            return res.get("text") or ""
        finally:
            for p in (tmp_in, tmp_out):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def unlock_many(self, jobs: list[tuple], timeout: int = 1200) -> dict:
        """批量解密：一次 Node 进程解开多章，避免每章 spawn 一次（约 2s/次）。

        jobs: [(id, cid, content_b64), ...]，id 任意可哈希标记（一般就用 cid）。
        返回 {id: html_str}，键类型与传入的 id 一致（JS 侧统一转字符串，这里映射回来）；
        失败章不出现，调用方可回退到 unlock 单章重试。
        """
        if not jobs:
            return {}
        items = []
        back = {}  # JS 侧字符串 id -> 调用方原始 id
        for j, cid, c in jobs:
            sid = str(j)
            back[sid] = j
            items.append({"id": sid, "cid": str(cid), "content": c})
        payload = {"glue": FOCK_GLUE, "fuid": self.fuid, "key": self.key,
                   "ts": self.ts, "items": items}
        tag = f"{os.getpid()}_{uuid.uuid4().hex[:10]}"
        tmp_in = os.path.join(TMP_DIR, f"_fock_batch_{tag}.in.json")
        tmp_out = os.path.join(TMP_DIR, f"_fock_batch_{tag}.out.json")
        os.makedirs(TMP_DIR, exist_ok=True)
        with open(tmp_in, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        try:
            proc = subprocess.run([self.node, FOCK_JS_BATCH, tmp_in, tmp_out],
                                  capture_output=True, timeout=timeout)
            if not os.path.exists(tmp_out):
                raise RuntimeError(f"Node 无输出: {proc.stderr.decode('utf-8', 'replace')[:500]}")
            res = json.load(open(tmp_out, encoding="utf-8"))
            out = {}
            for r in res.get("results") or []:
                if r.get("ok") and r.get("text"):
                    key = str(r["id"])
                    out[back.get(key, key)] = r["text"]
            return out
        finally:
            for p in (tmp_in, tmp_out):
                try:
                    os.remove(p)
                except OSError:
                    pass


def chapter_rights(data: dict) -> tuple[str, str]:
    """判断一章的权限状态。

    返回 (状态, 说明)，状态取值：
      "free"    免费章，content 即明文正文
      "vip"     账号有权限的 VIP 章，encrypt/fontEncrypt 需解密
      "denied"  无权限（未购买/未订阅/会话失效），必须停止
    """
    # 空 data 必须先拦下：会话失效时 /api/book/read 会返回 code=0 但 data={}，
    # 此时 chapterVipFlag/authStatus 全是 None。若不先判空，就会命中下面的
    # 「免费章」分支，把 VIP 章当成免费章一路下下去——那是危险的误判（会把
    # 203 字试读当正文写进 txt）。宁可判 denied 停下，也不能静默降级。
    if not isinstance(data, dict) or not data:
        return "denied", "响应 data 为空（会话可能已失效，需重新导出 Cookie）"

    ctype = data.get("chapterVipFlag")
    auth = data.get("authStatus")
    content = data.get("content") or ""
    encrypt = bool(data.get("encrypt"))
    font_enc = bool(data.get("fontEncrypt"))

    # 关键字段缺失同样不能当免费章
    if ctype is None and auth is None:
        return "denied", f"响应缺 chapterVipFlag/authStatus（{list(data)[:6]}）"

    if ctype != 1 and (auth in (None, 0, 1)) and not encrypt:
        return "free", "免费章"
    if auth == 1 and encrypt:
        layers = "Fock" + ("+字体反爬" if font_enc else "")
        return "vip", f"VIP章（有权限，需解 {layers}）"
    if auth == 0 or (ctype == 1 and not encrypt):
        return "denied", f"无权限（authStatus={auth}, 试读 {len(content)} 字）"
    return "denied", f"状态不明（chapterVipFlag={ctype}, authStatus={auth}, encrypt={encrypt}）"


def fetch_and_decode(client: Client, unlocker: FockUnlocker | None,
                     bid: str, cid: int, decoder=None, corpus=None) -> tuple[str, str, dict]:
    """取一章并还原成正文。

    返回 (状态, 正文, 原始 data)。状态同 chapter_rights。
    """
    data = client.read(bid, cid)
    state, why = chapter_rights(data)
    if state != "vip":
        if state == "free":
            txt = _html_to_text(data.get("content") or "")
            return state, txt, data
        return state, "", data
    if unlocker is None:
        raise RuntimeError("遇到 VIP 章但没有可用会话/密钥，无法解密")
    html = unlocker.unlock(data.get("content") or "", cid)
    if data.get("fontEncrypt"):
        if decoder is None:
            raise RuntimeError("遇到字体反爬章但解码器未就绪")
        css = (data.get("fontResponse") or {}).get("css") or ""
        txt, _stat = decoder.decode(html, css, corpus)
        return state, txt, data
    return state, _html_to_text(html), data


def _html_to_text(html: str) -> str:
    """明文/已还原 HTML → 纯文本（保留段落换行与全角缩进）。"""
    s = re.sub(r"</p\s*>", "\n", html or "", flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 只丢掉「整行都是空白」的行，不能用 strip()，否则会吃掉段首的全角缩进
    lines = [ln for ln in s.split("\n") if ln.strip("\u3000 \t")]
    return "\n".join(lines).strip("\n")


# ---------------------------------------------------------------- 自检


if __name__ == "__main__":
    _log("== qqread_vip 自检 ==")
    _log(cfg.describe())
    ck = load_cookies()
    cli = Client(ck)
    if ck:
        try:
            home = cli.homepage()
            _log(f"  账号: {home.get('nickname')} memberType={home.get('memberType')} "
                 f"tickets={home.get('tickets')} content={home.get('content')}")
        except Exception as e:  # noqa: BLE001
            _log(f"  homepage 失败（凭据可能已失效）: {e}")
    else:
        _log("  未配置凭据 -> 匿名模式，只能下免费章")

    # 探测一个已知免费章与一个付费章，确认接口连通性与权限判定
    bid = "56858498"
    for cid in (1, 22):
        try:
            d = cli.read(bid, cid)
            st, why = chapter_rights(d)
            _log(f"  cid={cid}: {st} —— {why}; content len={len(d.get('content') or '')}")
        except Exception as e:  # noqa: BLE001
            _log(f"  cid={cid} 失败: {e}")

    # Node 与 Fock 胶水是否就绪（VIP 解密的前提）
    try:
        node = find_node()
        ver = ""
        try:
            r = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=10)
            ver = f" 版本 {r.stdout.strip()}" if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            pass
        _log(f"  Node: {node}{ver}")
    except Exception as e:  # noqa: BLE001
        _log(f"  Node 不可用（VIP 章将无法解密）: {e}")
    _log(f"  Fock 胶水: {'就绪 ' + FOCK_GLUE if os.path.exists(FOCK_GLUE) else '缺失 ' + FOCK_GLUE}")
    _log("== 自检结束 ==")
