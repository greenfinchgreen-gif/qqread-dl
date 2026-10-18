# -*- coding: utf-8 -*-
"""qqread_font.py —— QQ阅读「字体反爬」还原层（第 2 层，自包含）。

【这一层解决什么】
Fock 解密（第 1 层，见 fock_unlock.js）解出来的 HTML 不是可读正文，字符被替换成
「伪码位」，且真字映射每次请求重新生成，因此不能预置映射表，必须运行时按字形识别：

  1) 正文里 ExtA 区（U+3400-4DBF）伪码位 —— 由 fontResponse.randomFont 渲染
  2) 正文里基本区（U+4E00-9FFF）伪码位 —— 由 fontResponse.fixedFont 渲染
     （注意：基本区汉字同样是伪码位，这是最容易踩空的一点）
  3) tagged 段落 <p class="pN"><ytag ...>inner</ytag></p> 里，真字由 fontResponse.css
     给出，且有四种形态：
        .pN ytag::after{content:'X'}          → 可见字 X（inner 是诱饵）
        .pN ytag::after{content:attr(yXXXX)}  → 该属性值才是真字，同标签其它属性是诱饵
        .pN ytag::before{content:attr(yXXXX)} → 同上（::before 与 ::after 无重叠）
        无任何规则                            → 退化为 inner（仍需字形还原）
     另有 ytag{order:N} 决定组内视觉顺序、.pN ytag{transform:scaleX(-1)} 表镜像。
  4) untagged 段落裸文本流；<y class="sy-0"> 视觉隐藏（font-size:0，是诱饵）、sy-1 镜像。

【还原算法】
用系统「微软雅黑」当参照字体（实测 8/8 全中且 rank 恒为 1；simhei 7/8、simsun 5/8，
fixedFont 自己 0/8 —— 它只是伪码位载体，不是参照字体）。

- 归一化：渲染 → 按墨迹 bbox 裁剪 → **长边等比缩放到 T** → 居中补到 T×T。
  （必须按长边：若按高度缩放，「一」这类极宽字居中裁剪后会变成满格实心方框，
   Chamfer 距离恒为 0，会污染全部结果。）
- 距离：双向 Chamfer（掩码到最近墨迹的 3-4 距离变换，正向/反向取均值）。
- 每个伪字形同时试 正常 / 水平镜像 两种变体（部分伪字形在字体内部就是镜像的）。
- 取 top-K 候选，再用字符 bigram（同书语料加权）做 Viterbi 纠错
  —— 纯几何无法区分「犬/大」「绐/给」「柰/查」这类形近字，语言模型可解。
  实测该组合在 203 字明文 GT 上逐字准确率 100%。

用法：
    from qqread_font import FontDecoder
    fd = FontDecoder()
    text = fd.decode(html, css, corpus_files=[(path, weight), ...])
"""
from __future__ import annotations

import atexit
import gc
import hashlib
import math
import os
import pickle
import re
import shutil
import struct
import sys
import time
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qqread_config as cfg  # noqa: E402  运行期缓存目录统一由配置层给出

OUTPUT = os.path.join(HERE, "output")
# 样本字体目录（可选；缺失时按下方逻辑自动降级为空，不报错）
ASSETS = os.path.join(HERE, "assets")


def _log(msg: str) -> None:
    """模块级日志（日志走 stderr，保持 stdout 机器可读）。"""
    print(msg, file=sys.stderr, flush=True)

# ---------------------------------------------------------------- 伪字体临时目录
# randomFont / fixedFont 每章都不同，必须落盘才能交给 FreeType 打开。
# 早期实现从不删除临时字体，实测跑全本会累积 1600+ 个文件、近 400MB 垃圾。
# 现在改为：每次进程用 cache/tmp_fonts/<pid>/，退出时清理；
# 同时启动时清扫「进程已不存在」的历史残留目录（Windows 上文件可能被
# FreeType 持有而删不掉，所以清理失败不算错误，下次再扫）。
_FONT_TMP_ROOT = cfg.cache_dir("tmp_fonts")


def _font_tmp_dir() -> str:
    d = os.path.join(_FONT_TMP_ROOT, str(os.getpid()))
    os.makedirs(d, exist_ok=True)
    return d


def _sweep_stale_font_dirs() -> None:
    """删除上次运行留下的临时字体目录（按 PID 判断进程是否还在）。"""
    try:
        names = os.listdir(_FONT_TMP_ROOT)
    except OSError:
        return
    for name in names:
        p = os.path.join(_FONT_TMP_ROOT, name)
        if not os.path.isdir(p) or name == str(os.getpid()):
            continue
        try:
            pid = int(name)
        except ValueError:
            continue
        alive = True
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            alive = False
        except Exception:  # noqa: BLE001
            alive = True
        if not alive:
            shutil.rmtree(p, ignore_errors=True)


def _cleanup_font_tmp() -> None:
    """进程退出时清掉本进程的临时字体（先 collect，让 FreeType 释放句柄）。"""
    gc.collect()
    shutil.rmtree(os.path.join(_FONT_TMP_ROOT, str(os.getpid())), ignore_errors=True)


_sweep_stale_font_dirs()
atexit.register(_cleanup_font_tmp)

# ---- 字形归一化参数（v5 定标值，改前先看 GT 回归）----
S = 160            # 渲染字号
T = 80             # 归一化位图边长
INK = 24           # >24 视为墨迹
BIN = 110          # 二值化阈值


def _find_ref_font() -> str:
    """定位参照字体：字形识别要拿一个「标准字形」当尺子，实测微软雅黑最准。

    可用环境变量 QQREAD_REF_FONT 显式指定。不写死单一绝对路径，
    换机器/换系统时按常见位置逐个探测；全找不到就明确报错，
    而不是等到识别出满屏错字才发现参照字体根本不存在。
    """
    env_font = os.environ.get("QQREAD_REF_FONT")
    cands = [env_font] if env_font else []
    cands += [
        "C:/Windows/Fonts/msyh.ttc",        # 微软雅黑（Windows 10/11）
        "C:/Windows/Fonts/msyh.ttf",        # 旧版 Windows
        "/System/Library/Fonts/PingFang.ttc",   # macOS
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise RuntimeError(
        "未找到参照字体（默认找 微软雅黑 msyh.ttc）。\n"
        "  请安装微软雅黑，或用环境变量 QQREAD_REF_FONT 指向任意中文 TTF/TTC，例如：\n"
        f"  set QQREAD_REF_FONT={cands[1]}\n"
        "  注意：换用其它参照字体后字形识别准确率会变化，需重新回归。")


REF_FONT = _find_ref_font()          # 参照字体（懒加载失败即在启动时报错）
# 字形索引（可重建，约 206MB）放 cache/：仓库不提交，首次运行自动重建
GEO_CACHE = cfg.cache_path("idx_v5_msyh.pkl")

# ---- 语言模型参数 ----
TOPK = 8
W_GEO = 1.0
W_LM = 0.9
# 几何门控：逐位置把候选裁剪到 d <= d_top1 + GEO_MARGIN，再交给 Viterbi。
# 原因：Chamfer 距离尺度 0~5，而 -logP 尺度可达 0~16，两者混加会让语言模型
# 覆盖几何上极有把握的字（实测 即 0.59 被改成 那 3.0、磨 0.83 被改成 唐 2.17）。
# 门控后 GT 相似度从 0.9799/0.9704/0.9704 提升到 0.9799/0.9763/0.9822，错字归零。
GEO_MARGIN = 1.2
SAME_BOOK_WEIGHT = 6

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
KEEP_PUNCT = "，。！？；：“”‘’、…—（）《》"


# ---------------------------------------------------------------- TTF 最小解析
class TTF:
    """只解析 cmap（format 4/12），用于判断某伪码位该用哪个伪字体渲染。"""

    def __init__(self, path: str) -> None:
        self.buf = open(path, "rb").read()
        self.tables: dict[str, tuple[int, int]] = {}
        n = struct.unpack(">H", self.buf[4:6])[0]
        for i in range(n):
            off = 12 + i * 16
            tag = self.buf[off:off + 4].decode("latin1")
            toff, tlen = struct.unpack(">II", self.buf[off + 8:off + 16])
            self.tables[tag] = (toff, tlen)
        m = self.tables["maxp"][0]
        self.numGlyphs = struct.unpack(">H", self.buf[m + 4:m + 6])[0]
        self.cmap: dict[int, int] = {}
        self._parse_cmap()

    def _parse_cmap(self) -> None:
        base = self.tables["cmap"][0]
        n = struct.unpack(">H", self.buf[base + 2:base + 4])[0]
        best = None
        for i in range(n):
            o = base + 4 + i * 8
            _plat, _enc, sub = struct.unpack(">HHI", self.buf[o:o + 8])
            fmt = struct.unpack(">H", self.buf[base + sub:base + sub + 2])[0]
            if fmt in (4, 12):
                best = (fmt, base + sub)
                if fmt == 12:
                    break
        if best is None:
            return
        fmt, off = best
        if fmt == 4:
            segX2 = struct.unpack(">H", self.buf[off + 6:off + 8])[0]
            seg = segX2 // 2
            ends = struct.unpack(">%dH" % seg, self.buf[off + 14:off + 14 + segX2])
            starts = struct.unpack(">%dH" % seg, self.buf[off + 16 + segX2:off + 16 + segX2 * 2])
            deltas = struct.unpack(">%dh" % seg, self.buf[off + 16 + segX2 * 2:off + 16 + segX2 * 3])
            roff = off + 16 + segX2 * 3
            rangeOffsets = struct.unpack(">%dH" % seg, self.buf[roff:roff + segX2])
            for s in range(seg):
                for c in range(starts[s], min(ends[s], 0xFFFF) + 1):
                    if rangeOffsets[s] == 0:
                        g = (c + deltas[s]) & 0xFFFF
                    else:
                        idx = roff + s * 2 + rangeOffsets[s] + (c - starts[s]) * 2
                        if idx + 2 > len(self.buf):
                            continue
                        g = struct.unpack(">H", self.buf[idx:idx + 2])[0]
                        if g:
                            g = (g + deltas[s]) & 0xFFFF
                    if g:
                        self.cmap[c] = g
        else:
            ngroups = struct.unpack(">I", self.buf[off + 12:off + 16])[0]
            for i in range(ngroups):
                go = off + 16 + i * 12
                sc, ec, sg = struct.unpack(">III", self.buf[go:go + 12])
                for c in range(sc, ec + 1):
                    self.cmap[c] = sg + (c - sc)


# ---------------------------------------------------------------- 位图归一化
def gb2312_chars() -> list[str]:
    """GB2312 一二级汉字表（6763 字）——候选集必须是它，不能是 fixedFont 的 cmap。"""
    out, seen = [], set()
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes([hi, lo]).decode("gb2312")
            except Exception:
                continue
            if "\u4e00" <= ch <= "\u9fff" and ch not in seen:
                seen.add(ch)
                out.append(ch)
    return out


def render_mask(font, ch: str) -> np.ndarray | None:
    """渲染单字 → 长边等比缩放到 T → 居中补到 T×T 的 bool 掩码。"""
    canvas = S * 3
    img = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(img).text((canvas // 6, canvas // 6), ch, fill=255, font=font)
    a = np.array(img)
    ys, xs = np.where(a > INK)
    if len(ys) == 0:
        return None
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = a.shape
    scale = T / float(max(h, w))          # 关键：按长边缩放，保留真实长宽比
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    im = Image.fromarray(a).resize((nw, nh), Image.BILINEAR)
    g = np.array(im) > BIN
    out = np.zeros((T, T), dtype=bool)
    y0, x0 = (T - nh) // 2, (T - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = g
    return out


def _dt1d(a: np.ndarray, w: float, axis: int) -> np.ndarray:
    n = a.shape[axis]
    idx = np.arange(n, dtype=np.float64)
    if axis == 1:
        f = np.minimum.accumulate(a - w * idx[None, :], axis=1) + w * idx[None, :]
        b = np.minimum.accumulate((a + w * idx[None, :])[:, ::-1], axis=1)[:, ::-1] - w * idx[None, :]
    else:
        f = np.minimum.accumulate(a - w * idx[:, None], axis=0) + w * idx[:, None]
        b = np.minimum.accumulate((a + w * idx[:, None])[::-1, :], axis=0)[::-1, :] - w * idx[:, None]
    return np.minimum(f, b)


def dist_transform(mask: np.ndarray) -> np.ndarray:
    """3-4 chamfer 距离变换：每个像素到最近墨迹的近似欧氏距离。"""
    a = np.where(mask, 0.0, 1e9)
    return _dt1d(_dt1d(a, 3.0, 1), 4.0, 0)


# ---------------------------------------------------------------- CSS 解析
CHILD = re.compile(r"<y([0-9a-z]+)((?:\s[^>]*)?)>(.*?)</y([0-9a-z]+)>", re.S)
YGEN = re.compile(r'<y(?: class="(sy-\d)")?>(.*?)</y>', re.S)
CLEAN = re.compile(r"<[^>]+>")
ATTR = re.compile(r'\b([A-Za-z0-9_-]+)="([^"]*)"')

RE_ORDER = re.compile(r"y([0-9a-z]+)\{order:(\d+);\}")
RE_AFTER_LIT = re.compile(r"\.p(\d+)\s+y([0-9a-z]+)::after\{content:'(.*?)'\}")
RE_AFTER_ATTR = re.compile(r"\.p(\d+)\s+y([0-9a-z]+)::after\{content:attr\((y[0-9a-z]+)\)\}")
RE_BEFORE_ATTR = re.compile(r"\.p(\d+)\s+y([0-9a-z]+)::before\{content:attr\((y[0-9a-z]+)\)\}")
RE_MIRROR = re.compile(r"\.p(\d+)\s+y([0-9a-z]+)\{transform:scaleX\(-1\)\}")


def parse_css(css: str):
    """→ (order: {tag: n}, rules: {(pN, tag): {...}})。"""
    order: dict[str, int] = {}
    rules: dict[tuple[str, str], dict] = {}
    for m in RE_ORDER.finditer(css):
        order[m.group(1)] = int(m.group(2))
    for m in RE_AFTER_LIT.finditer(css):
        rules.setdefault((m.group(1), m.group(2)), {})["after_lit"] = m.group(3)
    for m in RE_AFTER_ATTR.finditer(css):
        rules.setdefault((m.group(1), m.group(2)), {})["after_attr"] = m.group(3)
    for m in RE_BEFORE_ATTR.finditer(css):
        rules.setdefault((m.group(1), m.group(2)), {})["before_attr"] = m.group(3)
    for m in RE_MIRROR.finditer(css):
        rules.setdefault((m.group(1), m.group(2)), {})["mirror"] = True
    return order, rules


def _attrs(s: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in ATTR.finditer(s or "")}


def visual_items(attr_str: str, body: str, order: dict, rules: dict):
    """把一段 <p> 的原始 HTML 展开成视觉字符序列 [(char, mirror), ...]（尚未字形还原）。"""
    out: list[tuple[str, bool]] = []
    mcls = re.search(r'class="(p\d+)"', attr_str)
    if mcls:
        pc = mcls.group(1)[1:]          # CSS 写的是 .p0，class 是 "p0"，要去掉 p
        items, pos = [], 0
        for m in CHILD.finditer(body):
            if m.start() > pos:
                for off, c in enumerate(CLEAN.sub("", body[pos:m.start()])):
                    items.append((10 ** 6, pos + off, c, False))
            tag, attrs, inner = m.group(1), m.group(2), m.group(3)
            rule = rules.get((pc, tag), {})
            mir = bool(rule.get("mirror"))
            av = _attrs(attrs)
            if "after_lit" in rule:
                ch = rule["after_lit"]
            elif "after_attr" in rule:
                ch = av.get(rule["after_attr"], "")     # 只认规则点名的属性，其余是诱饵
            elif "before_attr" in rule:
                ch = av.get(rule["before_attr"], "")
            else:
                ch = inner                              # 无规则 → 退化 inner
            items.append((order.get(tag, 10 ** 6), pos, ch, mir))
            pos = m.end()
        if pos < len(body):
            for off, c in enumerate(CLEAN.sub("", body[pos:])):
                items.append((10 ** 6, pos + off, c, False))
        items.sort(key=lambda x: (x[0], x[1]))
        for _o, _p, ch, mir in items:
            for c in ch:
                out.append((c, mir))
    else:
        pos = 0
        for m in YGEN.finditer(body):
            if m.start() > pos:
                for c in CLEAN.sub("", body[pos:m.start()]):
                    out.append((c, False))
            cls, inner = m.group(1), m.group(2)
            if cls != "sy-0":                           # sy-0 是隐藏诱饵
                for c in inner:
                    out.append((c, cls == "sy-1"))
            pos = m.end()
        if pos < len(body):
            for c in CLEAN.sub("", body[pos:]):
                out.append((c, False))
    return out


# ---------------------------------------------------------------- 语言模型
def build_bigram(files) -> tuple[dict, dict, float]:
    """files: [(path, weight), ...] → (unigram, bigram, total)。带磁盘缓存。"""
    sig = hashlib.sha256(
        "|".join(f"{os.path.abspath(p)}:{w}:{os.path.getsize(p) if os.path.exists(p) else -1}"
                 for p, w in files).encode("utf-8")
    ).hexdigest()[:16]
    cache = cfg.cache_path(f"bigram_{sig}.pkl")
    if os.path.exists(cache):
        try:
            with open(cache, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    uni: dict[str, float] = defaultdict(float)
    bi: dict[tuple[str, str], float] = defaultdict(float)
    total = 0.0
    for path, w in files:
        if not path or not os.path.exists(path):
            continue
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        txt = "".join(c for c in txt if CJK.match(c) or c in KEEP_PUNCT)
        for i in range(len(txt) - 1):
            a, b = txt[i], txt[i + 1]
            if CJK.match(a) and CJK.match(b):
                bi[(a, b)] += w
                uni[a] += w
                total += w
    res = (dict(uni), dict(bi), total)
    try:
        with open(cache, "wb") as f:
            pickle.dump(res, f, protocol=4)
    except Exception:
        pass
    return res


def viterbi(cands, uni, bi, total, w_geo: float = W_GEO, w_lm: float = W_LM,
            margin: float | None = GEO_MARGIN) -> list[str]:
    """cands: [[(char, dist), ...], ...] → 最优字符序列。

    margin 非 None 时启用几何门控：每个位置的候选先裁剪到「几何距离与最优候选
    之差 <= margin」，再参与 Viterbi。这防止语言模型把几何上极有把握的字改掉。
    """
    n = len(cands)
    if n == 0:
        return []
    floor = 1e-7
    NEG = 1e18
    if margin is not None:
        trimmed = []
        for opts in cands:
            if not opts:
                trimmed.append(opts)
                continue
            best_d = opts[0][1]
            keep = [o for o in opts if o[1] <= best_d + margin]
            trimmed.append(keep or opts[:1])
        cands = trimmed
    dp: list[dict] = [dict() for _ in range(n)]
    bk: list[dict] = [dict() for _ in range(n)]
    for c, d in cands[0]:
        dp[0][c] = w_geo * d
    for i in range(1, n):
        for c, d in cands[i]:
            geo = w_geo * d
            bestv, bestp = NEG, None
            for p, pv in dp[i - 1].items():
                cnt = bi.get((p, c), 0)
                pu = uni.get(p, 0)
                p_lm = (cnt + 0.1) / (pu + 0.1 * total) if pu else floor
                v = pv + geo + w_lm * (-math.log(p_lm))
                if v < bestv:
                    bestv, bestp = v, p
            dp[i][c] = bestv
            bk[i][c] = bestp
    last = min(dp[n - 1], key=lambda c: dp[n - 1][c])
    seq = [last]
    for i in range(n - 1, 0, -1):
        last = bk[i][last]
        seq.append(last)
    return list(reversed(seq))


# ---------------------------------------------------------------- 解码器
class FontDecoder:
    """字体反爬还原器。构造一次可复用（索引与字体加载较慢，约 2-5 秒）。

    random_font_bytes：fontResponse.randomFont.data 的原始字节。
    注意 randomFont 每次请求都会重新生成（码位与字形映射随请求变），
    因此生产环境必须把当次响应里的 randomFont 传进来；不传则用 assets/ 里的样本。
    """

    def __init__(self, verbose: bool = True,
                 random_font_path: str | None = None,
                 fixed_font_path: str | None = None) -> None:
        self.verbose = verbose
        rnd = random_font_path or os.path.join(ASSETS, "font_random.ttf")
        fix = fixed_font_path or os.path.join(ASSETS, "font_fixed.ttf")
        # 生产环境下每章字体都由响应里带（fork 时替换），主实例的字体不是必需。
        # 因此样本字体缺失时只降级为空，不报错 —— 否则只提交代码、不提交样本就起不来。
        self.random_font_path = rnd if os.path.exists(rnd) else None
        self.fixed_font_path = fix if os.path.exists(fix) else None
        self.rcmap = set(TTF(rnd).cmap) if self.random_font_path else set()
        self.fcmap = set(TTF(fix).cmap) if self.fixed_font_path else set()
        self.rfont = ImageFont.truetype(rnd, S) if self.random_font_path else None
        self.ffont = ImageFont.truetype(fix, S) if self.fixed_font_path else None
        self._load_index()
        self._topk_cache: dict[str, list[tuple[str, float]]] = {}
        self.stat = {"glyph": 0, "keep": 0, "mirror": 0}

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def _load_index(self) -> None:
        if os.path.exists(GEO_CACHE):
            with open(GEO_CACHE, "rb") as f:
                keep, masks, dts = pickle.load(f)
            self._log(f"  字形索引缓存命中 {len(keep)} 字")
        else:
            keep, masks, dts = self._build_index()
            with open(GEO_CACHE, "wb") as f:
                pickle.dump((keep, masks, dts), f)
            self._log(f"  字形索引已构建并缓存 {len(keep)} 字")
        self.keep = keep
        self.masks = masks
        self.dts = dts
        # 反向 Chamfer 用 一次矩阵乘法代替 6763 次 Python 循环（实测快 18 倍，结果逐位相同）。
        self._maskf = masks.astype(np.float32)
        self._cnt = masks.sum(axis=1).astype(np.float32)
        self._lm_cache: dict = {}

    def _build_index(self):
        chars = gb2312_chars()
        font = ImageFont.truetype(REF_FONT, S, index=0)
        keep, masks, dts = [], [], []
        for i, ch in enumerate(chars):
            m = render_mask(font, ch)
            if m is None or m.sum() < 6:
                continue
            keep.append(ch)
            masks.append(m.ravel())
            dts.append(dist_transform(m).ravel())
            if i % 2000 == 0:
                self._log(f"    构建索引 {i}/{len(chars)}")
        return keep, np.array(masks), np.array(dts, dtype=np.float32)

    def _font_for(self, ch: str):
        """伪码位 → 该用哪个伪字体渲染；返回 None 表示不是伪码位（保持原样）。"""
        cp = ord(ch)
        if 0x3400 <= cp <= 0x4DBF and cp in self.rcmap:
            return self.rfont, "random"
        if 0x4E00 <= cp <= 0x9FFF and cp in self.fcmap:
            return self.ffont, "fixed"
        return None

    def fork(self, random_font_bytes: bytes, fixed_font_bytes: bytes | None = None,
             tag: str = "") -> "FontDecoder":
        """派生出「共享大索引、独立伪字体」的轻量副本，供多线程一章一个。

        为什么需要：randomFont/fixedFont 每次请求都会重新生成，5 个线程各解各的章，
        绝不能共用同一套字体和 _topk_cache（会互相污染）。
        而 216MB 的字形索引（keep/masks/dts）与伪字体无关，必须共享，不能每线程重建。
        """
        obj = object.__new__(FontDecoder)
        obj.verbose = self.verbose
        obj.keep = self.keep
        obj.masks = self.masks
        obj.dts = self.dts
        obj._maskf = self._maskf
        obj._cnt = self._cnt
        obj._lm_cache = self._lm_cache       # 只读的 bigram 缓存，可安全共享
        obj._topk_cache = {}
        obj.stat = {"glyph": 0, "keep": 0, "mirror": 0}

        uniq = tag or hashlib.md5(random_font_bytes).hexdigest()[:10]
        tmpdir = _font_tmp_dir()
        rp = os.path.join(tmpdir, f"r_{uniq}.ttf")
        with open(rp, "wb") as f:
            f.write(random_font_bytes)
        obj.random_font_path = rp
        obj.rcmap = set(TTF(rp).cmap)
        obj.rfont = ImageFont.truetype(rp, S)
        # PIL 的 truetype 会把整个字体读进内存，文件已无用途，立刻删掉；
        # Windows 上若句柄未释放会删除失败，交给 atexit 兜底。
        try:
            os.remove(rp)
        except OSError:
            pass

        if fixed_font_bytes:
            fp = os.path.join(tmpdir, f"f_{uniq}.ttf")
            with open(fp, "wb") as f:
                f.write(fixed_font_bytes)
            obj.fixed_font_path = fp
            obj.fcmap = set(TTF(fp).cmap)
            obj.ffont = ImageFont.truetype(fp, S)
            try:
                os.remove(fp)
            except OSError:
                pass
        else:
            obj.fixed_font_path = self.fixed_font_path
            obj.fcmap = self.fcmap
            obj.ffont = self.ffont
        return obj

    def add_fonts(self, random_font_bytes: bytes | None = None,
                  fixed_font_bytes: bytes | None = None) -> None:
        """换一批伪字体（每章响应的 randomFont 都不同），并清空识别缓存。

        字形索引（参照字体 msyh 的 6763 字）与伪字体无关，不需要重建。
        字体文件写进本进程的临时目录（cache/tmp_fonts/<pid>/，退出即清理）。
        """
        tmpdir = _font_tmp_dir()
        if random_font_bytes:
            p = os.path.join(tmpdir, "_random_live.ttf")
            with open(p, "wb") as f:
                f.write(random_font_bytes)
            self.random_font_path = p
            self.rcmap = set(TTF(p).cmap)
            self.rfont = ImageFont.truetype(p, S)
        if fixed_font_bytes:
            p = os.path.join(tmpdir, "_fixed_live.ttf")
            with open(p, "wb") as f:
                f.write(fixed_font_bytes)
            self.fixed_font_path = p
            self.fcmap = set(TTF(p).cmap)
            self.ffont = ImageFont.truetype(p, S)
        self._topk_cache.clear()

    def topk_batch(self, chars, k: int = TOPK) -> dict[str, list[tuple[str, float]]]:
        """批量版 topk：整章只做 2 次 BLAS 矩阵乘，比逐字快 20 倍以上。

        返回 {伪码位: [(候选字, chamfer 距离), ...]}；非伪码位映射为自身。
        """
        out: dict[str, list[tuple[str, float]]] = {}
        todo: list[tuple[str, np.ndarray, np.ndarray]] = []   # (ch, q, a_dt)
        for ch in dict.fromkeys(chars):
            if ch in self._topk_cache:
                out[ch] = self._topk_cache[ch]
                continue
            hit = self._font_for(ch)
            if hit is None:
                out[ch] = [(ch, 0.0)]
                continue
            m0 = render_mask(hit[0], ch)
            if m0 is None or m0.sum() < 6:
                out[ch] = [(ch, 0.0)]
                continue
            for mir in (False, True):
                m = m0[:, ::-1].copy() if mir else m0
                q = m.ravel().astype(np.float32)
                if q.sum() <= 0:
                    continue
                todo.append((ch, q, dist_transform(m).ravel().astype(np.float32)))
        if not todo:
            return out

        Q = np.stack([t[1] for t in todo], axis=1)              # (T*T, n)
        ADT = np.stack([t[2] for t in todo], axis=1)            # (T*T, n)
        qsum = Q.sum(axis=0)
        fwd = (self.dts @ Q) / qsum                              # (N, n)
        bwd = (self._maskf @ ADT) / self._cnt[:, None]           # (N, n)
        sc = (fwd + bwd) / 2.0

        merged: dict[str, dict[str, float]] = {}
        for j, (ch, _q, _d) in enumerate(todo):
            col = sc[:, j]
            idx = np.argpartition(col, k)[:k]
            idx = idx[np.argsort(col[idx])]
            slot = merged.setdefault(ch, {})
            for i in idx:
                c, v = self.keep[i], float(col[i])
                if c not in slot or v < slot[c]:
                    slot[c] = v
        for ch, slot in merged.items():
            got = sorted(slot.items(), key=lambda x: x[1])[:k] or [(ch, 0.0)]
            self._topk_cache[ch] = got
            out[ch] = got
        for ch in list(out):
            if ch not in merged and ch not in self._topk_cache:
                out[ch] = [(ch, 0.0)]
        return out

    def topk(self, ch: str, k: int = TOPK) -> list[tuple[str, float]]:
        """单字版（保留给调试/自检用）；内部走批量实现。"""
        return self.topk_batch([ch], k)[ch]

    def decode(self, html: str, css: str, corpus_files=None,
               use_lm: bool = True) -> tuple[str, dict]:
        """解出可读正文。返回 (text, stat)。"""
        order, rules = parse_css(css or "")
        paras = re.findall(r"<p([^>]*)>(.*?)</p>", html or "", re.S)
        self._log(f"  CSS: order {len(order)} 项 / 元素规则 {len(rules)} 条 / 段落 {len(paras)}")

        if use_lm:
            key = tuple(sorted((os.path.abspath(p), float(w)) for p, w in (corpus_files or [])))
            if key in self._lm_cache:
                uni, bi, total = self._lm_cache[key]
                self._log(f"  bigram: 缓存命中 字对 {len(bi)}")
            else:
                uni, bi, total = build_bigram(corpus_files or [])
                self._lm_cache[key] = (uni, bi, total)
                self._log(f"  bigram: 字对 {len(bi)} / 首字 {len(uni)}")
        else:
            uni, bi, total = {}, {}, 0.0

        # 第一遍：把所有段落展开成视觉字符序列，收集全部伪码位一次性批量识别。
        seqs: list[list[tuple[str, bool]]] = []
        for attr_str, body in paras:
            seqs.append(visual_items(attr_str, body, order, rules))
        all_chars = [ch for seq in seqs for ch, _m in seq if CJK.match(ch)]
        t0 = time.time()
        self.topk_batch(all_chars)
        self._log(f"  字形识别 {len(set(all_chars))} 个唯一伪码位 用时 {time.time() - t0:.2f}s")

        lines: list[str] = []
        low = 0
        # 统计用局部变量：本模块会被 5 线程并发调用，共享 self.stat 会互相污染。
        stat = {"glyph": 0, "keep": 0, "mirror": 0}
        self._last_stat = stat
        for seq in seqs:
            cands: list[list[tuple[str, float]]] = []
            for ch, mir in seq:
                if mir:
                    stat["mirror"] += 1
                if not CJK.match(ch):
                    cands.append([(ch, 0.0)])
                    continue
                got = self._topk_cache.get(ch) or [(ch, 0.0)]
                if len(got) == 1 and got[0][0] == ch:
                    stat["keep"] += 1
                else:
                    stat["glyph"] += 1
                cands.append(got)
            if use_lm and len(cands) > 1:
                out = viterbi(cands, uni, bi, total)
            else:
                out = [c[0][0] for c in cands]
            for i, ch in enumerate(out):
                if cands[i] and ch != cands[i][0][0]:
                    low += 1
            lines.append("".join(out))
        stat["lm_changed"] = low
        stat["unique"] = len(self._topk_cache)
        return "\n".join(lines), dict(stat)

    def map_table(self) -> dict[str, str]:
        """已识别的伪码位 → 真字映射（便于人工抽检/回写评审）。"""
        return {k: v[0][0] for k, v in self._topk_cache.items()
                if v and v[0][0] != k}


if __name__ == "__main__":
    # 环境自检：字体层能不能正常初始化（不依赖任何取证样本）
    _log("== qqread_font 自检 ==")
    _log(f"  参照字体: {REF_FONT}")
    _log(f"  字形索引: {GEO_CACHE} "
         f"({'已存在，直接加载' if os.path.exists(GEO_CACHE) else '不存在，首次运行将自动重建（约数分钟）'})")
    _log("  正在初始化解码器 ...")
    t0 = time.time()
    try:
        dec = FontDecoder()
    except Exception as e:  # noqa: BLE001
        _log(f"  ✗ 初始化失败: {e}")
        sys.exit(1)
    _log(f"  ✓ 初始化成功（{time.time() - t0:.1f}s，索引 {len(dec.keep)} 字）")
    _log(f"  样本伪字体: random={'有' if dec.random_font_path else '无（运行时会用响应里的真实字体，无需样本）'}"
         f" fixed={'有' if dec.fixed_font_path else '无'}")
    _log(f"  缓存目录: {cfg.cache_dir()}")
    _log("== 自检结束 ==")
