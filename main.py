# -*- coding: utf-8 -*-
"""QQ阅读（book.qq.com）小说爬虫 —— 纯协议还原，浏览器无关，右键直接运行。

用法：直接运行，按提示输入关键词，选择要下载的书即可。
     未输入关键词时不会发起搜索，程序会提示后退出。

    python main.py

协议结论（本项目逆向实测得出）：
  搜索   GET /api/booksearch/query?keyWord=&pageNo=&pageSize=   前 10 条
  详情   GET /api/book/detail?bid=
  目录   GET /api/book/detail/chapters?bid=                      一次全量
  正文   GET /api/book/read?bid=&cid=

  免费章：data.content 即明文全文（无加密）。
  账号有权阅读的 VIP 章：data 同时带两重反爬
      1) encrypt=true      content 是 base64 密文 → Fock（wasm2js）解密
      2) fontEncrypt=true  解出的 HTML 字符是伪码位 → 字体字形还原
  账号无权/未购买的 VIP 章：authStatus=0，服务端固定截 203 字试读（不可伪造绕过）。

【权限边界】
本脚本只下载当前账号**本身有权阅读**的章节：
  authStatus=1 + encrypt=true → 正常解密
  authStatus=0              → 判定无权限，停止整本
不实现任何绕过付费/试读截断的手段。

【依赖自举】
字体还原层需要 numpy + Pillow，而常见 venv 里通常没有。
脚本启动时自检，若当前解释器缺依赖，会自动切换到带依赖的运行时解释器
（可用环境变量 QQREAD_PY 指定）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qqread_config as cfg  # noqa: E402  凭据/密钥隔离层

# ---------------- 可调参数 ----------------
TOP_N = 10                 # 搜索返回条数
THREADS = 5                # 下载线程数
SLEEP = 0.15               # 单章请求后限速秒数
RETRIES = 3                # 单章重试次数
RETRY_WAIT = 1.5           # 重试退避秒数
FREE_CORPUS_MAX = 60       # 最多抓多少免费章当语言模型语料
# ------------------------------------------

ROOT = HERE
OUT_DIR = os.path.join(ROOT, "output")
SEARCH_JSON = os.path.join(OUT_DIR, "搜索结果.json")
CORPUS_LIVE = os.path.join(cfg.cache_dir(), "_corpus_live.txt")


def _candidate_pythons() -> list[str]:
    """列出「可能已装好 numpy/Pillow」的其它解释器（不含当前解释器）。

    不硬编码任何机器专属路径：开源项目里写死开发者的安装目录既暴露隐私，
    也让别人跑不起来。查找顺序：
      1. 环境变量 QQREAD_PY（显式指定，优先级最高）
      2. PATH 里的 python / python3
      3. Windows 常见安装位置（仅用环境变量拼路径，不含用户名硬编码）
    """
    from shutil import which

    out: list[str] = []
    env_py = os.environ.get("QQREAD_PY")
    if env_py:
        out.append(env_py)
    for name in ("python", "python3", "python.exe"):
        w = which(name)
        if w:
            out.append(w)
    import glob
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out += sorted(glob.glob(os.path.join(
            local, "Programs", "Python", "Python3*", "python.exe")), reverse=True)
    out += ["/usr/bin/python3", "/usr/local/bin/python3"]

    # 去重、排除当前解释器、过滤不存在或不可执行的项
    seen: set[str] = set()
    me = os.path.normcase(os.path.abspath(sys.executable))
    uniq: list[str] = []
    for p in out:
        if not p:
            continue
        ap = os.path.abspath(p)
        k = os.path.normcase(ap)
        if k in seen or k == me or not os.path.exists(ap):
            continue
        seen.add(k)
        uniq.append(ap)
    return uniq


DEPS_PY = _candidate_pythons()


def _log(msg: str) -> None:
    """日志走 stderr，保持 stdout 机器可读。"""
    print(msg, file=sys.stderr, flush=True)


# ---------------- 依赖自举 ----------------

def _deps_ok() -> bool:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _bootstrap() -> None:
    """缺 numpy/Pillow 时切换到带依赖的解释器重新执行自己。

    这样用户用任何 python（含裸 venv）右键运行都能跑通字体还原层。
    注意：Windows 上 os.execve 不可靠（实测进程直接退出且无输出），
    所以用 subprocess 起子进程并转发返回码。
    """
    if _deps_ok():
        return
    me = os.path.abspath(__file__)
    if os.environ.get("QQREAD_BOOTSTRAPPED") == "1":
        raise SystemExit(
            "缺少依赖 numpy / Pillow，且自动切换失败。\n"
            "请安装：pip install numpy pillow\n"
            "或设置环境变量 QQREAD_PY 指向已装好依赖的 python。")
    import subprocess
    for p in DEPS_PY:
        if not p or not os.path.exists(p):
            continue
        _log(f"[自举] 当前解释器缺 numpy/Pillow，切换到 {p}")
        env = dict(os.environ)
        env["QQREAD_BOOTSTRAPPED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            rc = subprocess.run([p, me] + sys.argv[1:], env=env).returncode
        except Exception as e:  # noqa: BLE001
            _log(f"[自举] 启动 {p} 失败: {e}")
            continue
        raise SystemExit(rc)
    raise SystemExit(
        "缺少依赖 numpy / Pillow，且未找到其它可用的解释器。\n"
        "请安装：pip install numpy pillow\n"
        "或设置环境变量 QQREAD_PY 指向已装好依赖的 python。")


# ---------------- 一、搜索 ----------------

def search(client, keyword: str) -> tuple[dict, list[str]]:
    """搜索前 TOP_N 条，返回 ({小说名称:[], 小说作者名称:[], 小说链接:[]}, bids)。"""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")
    j = client.get_json("booksearch/query",
                        {"keyWord": keyword, "pageNo": 1, "pageSize": TOP_N})
    code = j.get("code")
    if code != 0:
        from qqread_vip import _api_error
        raise _api_error("booksearch/query", j)
    book_list = (j.get("data") or {}).get("bookList") or []
    result = {"小说名称": [], "小说作者名称": [], "小说链接": []}
    bids: list[str] = []
    for b in book_list[:TOP_N]:
        result["小说名称"].append(b.get("title", ""))
        result["小说作者名称"].append(b.get("author", ""))
        bid = b.get("bid")
        bids.append(str(bid))
        result["小说链接"].append(f"https://book.qq.com/book-detail/{bid}")
    return result, bids


def save_search_json(result: dict) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SEARCH_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return SEARCH_JSON


# ---------------- 二、语料（语言模型纠错用） ----------------

def _corpus_from_output(exclude_names: tuple[str, ...] = ()) -> list[tuple[str, float]]:
    """把 output 目录里已下载的小说当语料（不同书权重 1）。

    exclude_names：必须排除的文件名。**本书自己上一轮解码出的 txt 绝不能进语料**——
    它是解码结果，里面已经带着同样的错字（实测「那可」「唐炼」「很几酒」），
    再喂回 bigram 会把错字权重放大，形成自我强化的污染闭环（实测错字从 1 处涨到 9 处）。
    本书语料只允许用「免费章明文」（服务端下发、绝对正确）另存为 CORPUS_LIVE。
    """
    out: list[tuple[str, float]] = []
    if not os.path.isdir(OUT_DIR):
        return out
    for name in os.listdir(OUT_DIR):
        if not name.endswith(".txt"):
            continue
        if name in exclude_names or any(name.startswith(n) for n in exclude_names):
            continue
        p = os.path.join(OUT_DIR, name)
        try:
            if os.path.getsize(p) > 2000:
                out.append((p, 1.0))
        except OSError:
            pass
    return out


def prepare_corpus(client, chapters: list[dict], bid: str, plan_all: bool,
                   title: str = "") -> list[tuple[str, float]]:
    """组语料：其它书 + 本书免费章明文（权重最高）。

    字体字形识别只能区分「骨架」，形近字（犬/大、绐/给）必须靠语言模型。
    实测带上同书语料后，203 字 GT 的逐字准确率从 ~92% 提升到 100%。
    注意：本书语料只能用免费章明文，不能用上一轮解码结果（会自我强化错字）。
    """
    # 排除本书自己的输出文件（含 _全本 变体）
    excludes = (f"{title}.txt", f"{title}_全本.txt") if title else ()
    corpus = [x for x in _corpus_from_output(excludes)]
    if excludes:
        _log(f"  语料：排除本书自身输出 {list(excludes)}（防自污染）")
    free = [c for c in chapters if c.get("free") == 1]
    # 本书自己的语料最有效，但只抓前面若干章（避免为了语料把整本免费书都下两遍）
    picked = free[:FREE_CORPUS_MAX]
    if not picked:
        return corpus
    texts: list[str] = []
    for c in picked:
        try:
            d = client.read(bid, c["cid"])
            if not d.get("encrypt"):
                texts.append(d.get("content") or "")
        except Exception:  # noqa: BLE001
            continue
    if texts:
        os.makedirs(os.path.dirname(CORPUS_LIVE), exist_ok=True)
        body = "\n".join(texts)
        with open(CORPUS_LIVE, "w", encoding="utf-8") as f:
            f.write(body)
        corpus.append((CORPUS_LIVE, 6.0))     # 同书免费章明文，权重 6
        _log(f"  语料：本书免费章 {len(texts)} 章 {len(body)} 字（权重 6，明文权威）")
    return corpus


# ---------------- 三、VIP 解密流水线 ----------------

class VipPipeline:
    """账号有权限的 VIP 章：Fock 解密 → 字体字形还原。

    线程安全性：
      * FockUnlocker.unlock 每次用 uuid 临时文件，可并发；
      * FontDecoder 的大索引（216MB）只加载一次并被 fork 出来的副本共享，
        每章各持一套伪字体（randomFont 每请求都变，不能共用）；
      * 字体下载/落盘有锁保护。
    """

    def __init__(self, client, bid: str, corpus) -> None:
        from qqread_vip import FockUnlocker, fetch_fkp
        from qqread_font import FontDecoder

        self.client = client
        self.corpus = corpus
        self.fkp = fetch_fkp(client, bid, 1)
        self.unlocker = FockUnlocker(self.fkp)
        t0 = time.time()
        self.master = FontDecoder(verbose=False)
        self._font_lock = threading.Lock()
        self._font_cache: dict[str, str] = {}
        _log(f"  [VIP] 解密流水线就绪 {time.time() - t0:.2f}s "
             f"(node={os.path.basename(self.unlocker.node)})")

    # ---- fixedFont 下载（URL 带版本号，每章可能不同，按 URL 缓存）----
    def _fixed_font_path(self, url: str | None) -> str | None:
        if not url:
            return None
        with self._font_lock:
            if url in self._font_cache:
                return self._font_cache[url]
        import hashlib
        # 缓存目录统一由 cfg.cache_dir 给出（cache/，已 gitignore；不可写时自动退系统临时目录）
        cache_dir = cfg.cache_dir("fixed_font")
        p = os.path.join(cache_dir, f"_fixed_{hashlib.sha1(url.encode()).hexdigest()[:12]}.ttf")
        if not os.path.exists(p):
            try:
                data = self.client.get_bytes(url)
                with self._font_lock:
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(p, "wb") as f:
                        f.write(data)
            except Exception as e:  # noqa: BLE001
                _log(f"  [VIP] fixedFont 下载失败（退化为本地样本）: {e}")
                return None
        with self._font_lock:
            self._font_cache[url] = p
        return p

    def decode(self, data: dict, cid: int) -> str:
        """VIP 章 data → 正文纯文本。"""
        from qqread_vip import _html_to_text

        html = self.unlocker.unlock(data.get("content") or "", cid)
        if not data.get("fontEncrypt"):
            return _html_to_text(html)

        fr = data.get("fontResponse") or {}
        rbytes = bytes((fr.get("randomFont") or {}).get("data") or [])
        if not rbytes:
            _log("  [VIP] 警告：fontEncrypt=true 但没拿到 randomFont，退回原样")
            return _html_to_text(html)
        fpath = self._fixed_font_path(fr.get("fixedFontTtf") or fr.get("fixedFontWoff2"))
        fbytes = None
        if fpath:
            try:
                with open(fpath, "rb") as f:
                    fbytes = f.read()
            except OSError:
                fbytes = None
        dec = self.master.fork(rbytes, fbytes, tag=f"c{cid}")
        txt, _stat = dec.decode(html, fr.get("css") or "", corpus_files=self.corpus)
        return txt


# ---------------- 四、按章序流式写入 ----------------

class StreamWriter:
    """按章节顺序流式写入：每拿到一段（且其前置章节已到齐）立即追加写文件。

    不是爬完再一次性写入 —— 每完成一章的前置依赖就 flush 一次。
    """

    def __init__(self, path: str, encoding: str = "utf-8") -> None:
        self.path = path
        self._fh = open(path, "w", encoding=encoding, buffering=1)  # 行缓冲
        self._lock = threading.Lock()
        self._pending: dict[int, str] = {}
        self._next = -1  # -1 为表头，正文从 0 开始
        self.written = 0

    def put(self, idx: int, text: str) -> None:
        with self._lock:
            self._pending[idx] = text
            self._flush_locked()

    def _flush_locked(self) -> None:
        while self._next in self._pending:
            chunk = self._pending.pop(self._next)
            self._fh.write(chunk)
            self._fh.flush()          # 立即落盘
            idx = self._next
            self._next += 1
            if idx >= 0:              # 表头（idx=-1）不计入章节数
                self.written += 1

    @property
    def next_index(self) -> int:
        with self._lock:
            return self._next

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# ---------------- 五、下载整本 ----------------

def download_book(client, bid: str, title: str, max_chapters: int = 0) -> str:
    """下载整本书：5 线程并发取章节，按章序流式追加写入 output/<书名>.txt。

    章节权限判定见 qqread_vip.chapter_rights：
      免费 → 直接用明文；VIP 但账号有权限 → 解密还原；无权限 → 停止整本。
    """
    detail = client.detail(bid)
    title = detail.get("title") or title
    chapters = client.chapters(bid)
    if not chapters:
        raise RuntimeError("目录为空")

    free_n = sum(1 for c in chapters if c.get("free") == 1)
    vip_n = len(chapters) - free_n

    # 有没有登录态（决定 VIP 章能不能下）
    pipeline = None
    home: dict = {}
    if client.cookies:
        try:
            home = client.homepage()
        except Exception as e:  # noqa: BLE001
            _log(f"  [会话] homepage 探测失败，继续尝试读取章节: {e}")
    nick = home.get("nickname")
    if nick:
        _log(f"  [会话] 已登录：{nick} memberType={home.get('memberType')} "
             f"到期={home.get('content')} 书券={home.get('tickets')}")
        if home.get("memberType") in (0, None) and vip_n:
            _log("  [会话] 注意：该账号当前没有有效会员（memberType=%s），VIP 章会读不到"
                 % home.get("memberType"))
    elif client.cookies and home.get("code") == -4:
        # homepage 与正文接口的鉴权口径不同：实测仅带 uid/fuid/usid 时
        # homepage 仍可能返回 -4，但 detail/read 可以正常鉴权。只在探测
        # 结果确为鉴权失败时提示，不擅自把有效 Cookie 判定为失效。
        _log("  [会话] ⚠ homepage 鉴权失败，无法显示账号昵称/会员信息；"
             "将继续尝试读取章节")
    elif client.cookies:
        _log("  [会话] homepage 未返回用户信息，将继续尝试读取章节")
    else:
        _log("  [会话] 未配置 Cookie（匿名模式）")

    can_try_vip = bool(client.cookies)
    if vip_n and can_try_vip:
        try:
            corpus = prepare_corpus(client, chapters, bid, True, title=title)
            pipeline = VipPipeline(client, bid, corpus)
        except Exception as e:  # noqa: BLE001
            _log(f"  [VIP] 流水线初始化失败，VIP 章将无法解密: {e}")
            pipeline = None
    elif vip_n and not can_try_vip:
        _log(f"  [VIP] 共 {vip_n} 章付费/VIP，当前未配置登录态 → 只能下免费部分")

    # 计划：先探测第一个非免费章的权限，静态决定下载范围。
    # 不能等线程跑到付费章才 stop —— 那会把还在排队的免费章一起丢掉（实测少 1 章）。
    from qqread_vip import chapter_rights

    first_vip = next((c for c in chapters if c.get("free") != 1), None)
    prefetch: dict[int, dict] = {}
    if first_vip is None:
        plan = list(chapters)
        _log("  全书免费，计划下载全部章节")
    elif pipeline is None:
        plan = [c for c in chapters if c.get("free") == 1]
        _log(f"  无 VIP 解密能力，计划只下前 {len(plan)} 章免费部分")
    else:
        probe = client.read(bid, first_vip["cid"])
        state, why = chapter_rights(probe)
        prefetch[first_vip["cid"]] = probe
        if state == "vip":
            plan = list(chapters)
            _log(f"  权限探测：{first_vip.get('chapterName')} -> {why}，计划下载全部章节")
        else:
            plan = [c for c in chapters if c.get("free") == 1]
            _log(f"  权限探测：{why} -> 当前账号读不到 VIP 章，"
                 f"计划只下前 {len(plan)} 章免费部分")
            _log("  （若刚续费/换号，请重新登录 book.qq.com 并更新凭据文件后再运行）")
    if max_chapters > 0:
        plan = plan[:max_chapters]
    if not plan:
        raise RuntimeError("没有可下载的章节")

    os.makedirs(OUT_DIR, exist_ok=True)
    safe_title = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or bid
    out_path = os.path.join(OUT_DIR, f"{safe_title}.txt")

    _log(f"《{title}》 共 {len(chapters)} 章（免费 {free_n} / VIP {vip_n}），"
         f"计划下载 {len(plan)} 章 -> {out_path}")

    writer = StreamWriter(out_path)
    stop_event = threading.Event()
    err_lock = threading.Lock()
    err_list: list[str] = []
    stat_lock = threading.Lock()
    counters = {"free": 0, "vip": 0}

    sess = f"已登录 {nick}" if nick else "匿名"
    if nick:
        sess += f"（memberType={home.get('memberType')}）"
    head = (f"《{title}》（bid={bid}）\n"
            f"目录共 {len(chapters)} 章：免费 {free_n} 章，VIP/付费 {vip_n} 章\n"
            f"会话：{sess}\n"
            f"策略：只下载账号有权阅读的章节；遇无权限章停止整本。\n"
            + "=" * 40 + "\n\n")
    writer.put(-1, head)

    def job(pos: int, cid: int, cname: str) -> None:
        if stop_event.is_set():
            return
        from qqread_vip import chapter_rights

        time.sleep(SLEEP * (pos % THREADS))  # 错峰限速
        try:
            data = prefetch.pop(cid, None) or client.read(bid, cid)
        except Exception as e:  # noqa: BLE001
            with err_lock:
                err_list.append(f"cid={cid}: {e}")
            writer.put(pos, f"\n【第 {pos + 1} 章获取失败：{e}】\n")
            return
        if stop_event.is_set():
            return

        state, why = chapter_rights(data)
        if state == "denied":
            stop_event.set()
            writer.put(pos, "\n【遇无权限章节，停止整本】\n")
            _log(f"  cid={cid} 无权限（{why}），停止下载")
            return
        try:
            if state == "vip":
                if pipeline is None:
                    raise RuntimeError("无 VIP 解密能力")
                text = pipeline.decode(data, cid)
                with stat_lock:
                    counters["vip"] += 1
            else:
                from qqread_vip import _html_to_text
                text = _html_to_text(data.get("content") or "")
                with stat_lock:
                    counters["free"] += 1
        except Exception as e:  # noqa: BLE001
            with err_lock:
                err_list.append(f"cid={cid}: {e}")
            writer.put(pos, f"\n【第 {pos + 1} 章解密失败：{e}】\n")
            return

        # 校验：VIP 章解出的字数应接近服务端给的 totalWords
        tw = data.get("totalWords") or 0
        n = len("".join(text.split()))
        flag = ""
        if state == "vip" and tw and n < tw * 0.5:
            flag = f"！（字数偏少 {n}/{tw}）"
        writer.put(pos, f"\n{cname}\n\n{text}\n")
        _log(f"  [{writer.written}/{len(plan)}] {cname} "
             f"({n} 字, {state}){flag}")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(job, i, c["cid"], c.get("chapterName", f"第{i+1}章"))
                   for i, c in enumerate(plan)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                stop_event.set()
                err_list.append(str(e))
    writer.close()

    _log(f"完成：写入 {writer.written} 章（免费 {counters['free']} / VIP {counters['vip']}）"
         f" -> {out_path}")
    if err_list:
        _log(f"存在 {len(err_list)} 个错误：{err_list[:3]}")
    return out_path


# ---------------- 交互主流程 ----------------

def _ask(prompt: str, default: str) -> str:
    """交互式输入；非 TTY / EOF 时自动用默认值，保证可无参运行。"""
    if not sys.stdin.isatty():
        _log(f"[非交互] {prompt} 使用默认值: {default}")
        return default
    try:
        s = input(prompt).strip()
        return s or default
    except EOFError:
        return default


def _ask_required(prompt: str) -> str:
    """读取必填输入；支持管道传入，只有 EOF 或空白时返回空串。"""
    while True:
        try:
            s = input(prompt).strip()
        except EOFError:
            _log(f"[非交互] {prompt} 未提供输入")
            return ""
        if s:
            return s
        _log("关键词不能为空，请重新输入。")


def main() -> None:
    _bootstrap()
    from qqread_vip import Client, load_cookies

    cookies = load_cookies()
    client = Client(cookies)
    if not cookies:
        _log("提示：未找到任何凭据，将按匿名模式运行（只能下免费章）。")
        _log("      VIP 章下载需先登录 book.qq.com 并配置凭据，方法见 README「使用方法」。")
        _log("      查看当前配置状态：python qqread_config.py")

    keyword = _ask_required("请输入搜索关键词: ")
    if not keyword:
        raise SystemExit("未提供搜索关键词，已取消搜索。")
    _log(f"搜索: {keyword} ...")
    result, bids = search(client, keyword)
    path = save_search_json(result)
    n = len(result["小说名称"])
    if n == 0:
        _log("无搜索结果")
        return
    _log(f"搜索完成，前 {n} 条已写入 {path}")
    for i in range(n):
        _log(f"  [{i + 1}] {result['小说名称'][i]} —— {result['小说作者名称'][i]}")

    pick = _ask(f"输入要下载的序号 1-{n}（回车默认 1）: ", "1")
    try:
        idx = int(pick) - 1
    except ValueError:
        idx = 0
    if not (0 <= idx < n):
        idx = 0
    bid = bids[idx]
    _log(f"开始下载 [{idx + 1}] {result['小说名称'][idx]} (bid={bid})")
    out = download_book(client, bid, result["小说名称"][idx])
    _log(f"输出文件: {out}")


if __name__ == "__main__":
    main()
