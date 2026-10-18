# -*- coding: utf-8 -*-
"""qqread_config.py —— 凭据与设备信息隔离层。

【这一层解决什么】
开源项目里绝不能出现真实 Cookie、登录态或解密密钥。本模块把「凭据从哪来」
与业务代码彻底解耦：业务层只调用 load_credentials()，不关心凭据存在哪。

【加载优先级（先命中先用）】
  1. 环境变量 QQREAD_COOKIE        —— 直接给 Cookie 串，完全不落盘（CI / 临时使用首选）
  2. 环境变量 QQREAD_CREDENTIALS   —— 指向凭据 JSON 文件的绝对路径
  3. 环境变量 QQREAD_HOME          —— 指向凭据目录
  4. %USERPROFILE%\\.qqread\\credentials.json   —— 设备级隔离目录（日常使用推荐）
  5. %APPDATA%\\qqread\\credentials.json
  6. <项目上级目录>\\.qqread\\credentials.json   —— 项目外、工作区内，便于本地开发
  7. <项目目录>\\session_cookies.json           —— 兼容旧版；已被 .gitignore 屏蔽

  密钥缓存（fkp）同理：写入凭据目录，不再留在项目内。
  项目目录里不保留任何真实凭据 —— 这样 `git add .` 不可能把凭据带进仓库。

【为什么不能明文写死在代码里】
  git 历史是永久的：密钥一旦提交，即使后续删除也仍能从历史里翻出来。
  所以本模块只从上述外部位置读取，且提供 mask() 供日志脱敏。

【支持的凭据文件格式（三种，自动识别）】
  A) {"cookie": "uid=...; fuid=...; usid=..."}            Cookie 串
  B) {"cookies": [{"name": "...", "value": "..."}, ...]}  Playwright storage_state
  C) {"uid": "...", "fuid": "...", ...}                   扁平字典
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 环境变量名 ----------------
ENV_COOKIE = "QQREAD_COOKIE"
ENV_CRED_FILE = "QQREAD_CREDENTIALS"
ENV_HOME = "QQREAD_HOME"

# ---------------- 固定文件名 ----------------
HOME_DIRNAME = ".qqread"
CRED_FILENAME = "credentials.json"
FKP_FILENAME = "fkp_cache.json"
LEGACY_SESSION = os.path.join(HERE, "session_cookies.json")

# 登录态必需的关键字段（用于判断凭据是否可用）
KEY_FIELDS = ("uid", "fuid", "usid")

# 凭据文件模板（首次运行会写出一份到凭据目录，不含任何真实值）
CREDENTIAL_TEMPLATE = {
    "_说明": "把浏览器登录 book.qq.com 后的 Cookie 填进 cookie 字段（形如 uid=...; fuid=...; "
             "usid=...）。本文件位于项目之外，不会被提交到 git。支持三种格式，见 "
             "qqread_config.py 顶部注释。",
    "_获取方法": "Edge/Chrome 打开 https://book.qq.com 并登录 -> F12 -> "
                 "Application/应用程序 -> Cookies -> 全选复制，或使用 Cookie 导出插件。",
    "_注意": "cookie 留空时本文件按「未配置」处理，会继续查找其它位置的凭据；"
             "切勿把中文说明文字填进 cookie。",
    "cookie": "",
}


def _log(msg: str) -> None:
    """日志走 stderr，保持 stdout 机器可读。"""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 脱敏

def mask(secret: object) -> str:
    """脱敏显示：只保留前 4 位。用于所有对外日志，避免凭据经日志泄漏。"""
    if secret is None or secret == "":
        return "(空)"
    s = str(secret)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:4]}…(len={len(s)})"


def mask_cookie_value(v: str) -> str:
    """Cookie 值脱敏：短值全遮，长值留前 4 位。"""
    return mask(v)


# ---------------------------------------------------------------- 目录解析

def candidate_homes() -> list[str]:
    """按优先级列出候选凭据目录（不创建、不校验存在性）。"""
    out: list[str] = []
    env_home = os.environ.get(ENV_HOME)
    if env_home:
        out.append(os.path.abspath(env_home))
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if up:
        out.append(os.path.join(up, HOME_DIRNAME))
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "qqread"))
    # 从项目目录向上逐级查找 .qqread（项目外，但通常仍在工作区内，无需额外权限）。
    # 这样无论项目被放在哪一层，只要在某个祖先目录放一份 .qqread 就能被找到。
    d = os.path.dirname(HERE)
    for _ in range(4):
        if not d or d == os.path.dirname(d):
            break
        out.append(os.path.join(d, HOME_DIRNAME))
        d = os.path.dirname(d)
    # 去重且保序
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def find_config_dir() -> str | None:
    """返回凭据目录：优先「有可用凭据」的，其次「已存在文件」的，都没有则 None。

    只按「文件存在」判断会让空模板目录胜出，导致 fkp 密钥缓存、模板写入
    都落到一个用户其实没在用的目录里，而真正的凭据在另一处——排查时极易
    看错地方。所以先挑能解析出非空 Cookie 的目录。
    """
    homes = candidate_homes()
    for d in homes:  # 1) 有可用凭据
        p = os.path.join(d, CRED_FILENAME)
        if os.path.exists(p):
            raw = _read_json(p)
            if raw is not None and parse_credentials(raw):
                return d
    for d in homes:  # 2) 退一步：存在文件（空模板/待填写）
        if os.path.exists(os.path.join(d, CRED_FILENAME)):
            return d
    return None


def ensure_config_dir() -> str | None:
    """返回第一个「可创建/可写」的凭据目录；全部不可写则 None。

    写入凭据或密钥缓存前调用。目的是把数据放到项目外，避免误提交。
    """
    for d in candidate_homes():
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return d
        except Exception:  # noqa: BLE001
            continue
    return None


def config_dir() -> str:
    """凭据目录：优先已存在的，其次可写的，最后退回项目目录（并在日志中警告）。"""
    d = find_config_dir() or ensure_config_dir()
    if d:
        return d
    _log("  [配置] ⚠ 所有项目外凭据目录均不可写，退回项目目录。"
         "请务必确认 session_cookies.json 已被 .gitignore 屏蔽。")
    return HERE


# ---------------------------------------------------------------- 解析

def _cookie_safe(v: str) -> bool:
    """Cookie 值必须能进 HTTP 头（RFC 6265 规定为 ASCII 可见字符）。

    TTF 里的中文说明、模板占位符都过不了这一关：占位符一旦被当成真实
    Cookie，urllib 会在发请求时抛 "'latin-1' codec can't encode
    characters"，报错位置离真正原因（文件没填）很远，很难排查。
    这里在解析阶段就挡掉，并提示用户。
    """
    try:
        v.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


def parse_cookie_text(text: str) -> dict:
    """把 Cookie 串（a=b; c=d）解析成字典，丢弃非 ASCII 的占位值。"""
    jar: dict[str, str] = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and _cookie_safe(v):
                jar[k] = v
    return jar


def parse_credentials(data: object) -> dict:
    """把三种格式的凭据数据统一成 {name: value} 字典。"""
    jar: dict[str, str] = {}
    if isinstance(data, dict):
        if isinstance(data.get("cookie"), str):
            jar.update(parse_cookie_text(data["cookie"]))
        elif isinstance(data.get("cookies"), list):
            for c in data["cookies"]:
                if isinstance(c, dict) and c.get("name"):
                    v = str(c.get("value", ""))
                    if _cookie_safe(v):
                        jar[str(c["name"])] = v
        else:
            for k, v in data.items():
                if k.startswith("_") or k == "origins":
                    continue          # 跳过模板里的说明字段
                if isinstance(v, (str, int)) and _cookie_safe(str(v)):
                    jar[str(k)] = str(v)
    return jar


def _read_json(path: str) -> object | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        _log(f"  [配置] 读取 {os.path.basename(path)} 失败: {e}")
        return None


def _candidate_paths() -> list[str]:
    """按优先级列出所有可能的凭据文件路径（含环境变量与旧版兼容路径）。"""
    paths: list[str] = []
    env_file = os.environ.get(ENV_CRED_FILE)
    if env_file:
        paths.append(env_file)
    for d in candidate_homes():
        paths.append(os.path.join(d, CRED_FILENAME))
    paths.append(LEGACY_SESSION)
    return paths


def credentials_path() -> str | None:
    """定位「真正生效」的凭据文件，返回其路径。

    注意：只判断文件是否存在是不够的。`write_credential_template()` 会在
    高优先级目录里写一份 cookie 为空的模板，若按「文件存在」判定，就会把这份
    空模板报成实际生效来源，把后面真正填好的凭据遮住，直接影响用户排查方向。
    因此这里必须复用与 `load_credentials()` 完全相同的判定：能解析出非空
    Cookie 才算数。环境变量 QQREAD_COOKIE 不落盘，此时返回 None。
    """
    if os.environ.get(ENV_COOKIE):
        return None
    for p in _candidate_paths():
        if not os.path.exists(p):
            continue
        raw = _read_json(p)
        if raw is None:
            continue
        if parse_credentials(raw):
            return p
    return None


def load_credentials(verbose: bool = True) -> dict:
    """加载会话 Cookie。找不到返回空 dict（= 匿名模式，只能下免费章）。

    返回值只含 Cookie 键值；调用方不得把返回值直接打进日志。
    """
    # 1) 环境变量直接给 Cookie 串：不落盘，最安全
    env_cookie = os.environ.get(ENV_COOKIE)
    if env_cookie:
        jar = parse_cookie_text(env_cookie)
        if jar:
            if verbose:
                _log(f"  [配置] 已从环境变量 {ENV_COOKIE} 加载 {len(jar)} 个 Cookie（未落盘）")
            return _check_fields(jar, f"环境变量 {ENV_COOKIE}", verbose)

    # 2~7) 逐个候选路径
    for p in _candidate_paths():
        if not os.path.exists(p):
            continue
        raw = _read_json(p)
        if raw is None:
            continue
        jar = parse_credentials(raw)
        if not jar:
            continue
        if verbose:
            _log(f"  [配置] 已加载 {len(jar)} 个 Cookie（来源：{p}）")
        return _check_fields(jar, p, verbose)

    if verbose:
        _log("  [配置] 未找到任何凭据，按匿名模式运行（只能下免费章）")
    return {}


def _check_fields(jar: dict, src: str, verbose: bool) -> dict:
    """关键字段缺失时给出明确警告，避免静默按匿名处理。"""
    if verbose and not any(k in jar for k in KEY_FIELDS):
        _log(f"  [配置] ⚠ 来源 {src} 中未见 {'/'.join(KEY_FIELDS)}，"
             "可能不是有效登录态，只能下免费章")
    return jar


# ---------------------------------------------------------------- fkp 密钥缓存

def cache_dir(*parts: str) -> str:
    """运行期缓存目录（GEO 索引、bigram、伪字体、临时文件）。

    放在项目内的 cache/：缓存是纯运行期产物，体积大且可重建，
    既不该写进系统目录，也不该混进随仓库分发的源码目录。
    本函数负责按需创建并保证可用（不可写时退回系统临时目录）。
    """
    d = os.path.join(HERE, "cache", *parts)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        _log(f"  [配置] 缓存目录创建失败，退回系统临时目录: {e}")
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "qqread_cache", *parts)
        os.makedirs(d, exist_ok=True)
    return d


def cache_path(name: str) -> str:
    """运行期缓存**文件**路径（与 cache_dir 区分：这个只拼路径，不建目录）。

    cache_dir 是"给我一个目录"的语义，会 makedirs；拿它拼文件名会
    在磁盘上造出一个与文件同名的空目录，之后以文件方式打开必然
    EACCES/PermissionError（Windows 上尤其难看出原因）。凡是要写
    单个缓存文件，一律走本函数。
    """
    d = cache_dir()
    return os.path.join(d, name)


def fkp_cache_path() -> str:
    """fkp 密钥缓存路径（项目外，避免密钥随项目提交）。"""
    return os.path.join(config_dir(), FKP_FILENAME)


def load_fkp_cache() -> dict | None:
    p = fkp_cache_path()
    if not os.path.exists(p):
        return None
    raw = _read_json(p)
    if isinstance(raw, dict) and raw.get("fuid") and raw.get("key"):
        _log(f"  [fkp] 缓存命中（fuid={mask(raw.get('fuid'))}）")
        return raw
    return None


def save_fkp_cache(fkp: dict) -> bool:
    p = fkp_cache_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(fkp, f, ensure_ascii=False, indent=2)
        _log(f"  [fkp] 已缓存密钥到项目外：{p}")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"  [fkp] 密钥缓存写入失败（不影响运行）: {e}")
        return False


# ---------------------------------------------------------------- 模板

def write_credential_template(force: bool = False) -> str | None:
    """在凭据目录写一份不含真实值的模板，方便用户照格式填写。

    force=False（默认）时若凭据文件已存在则直接返回其路径，绝不覆盖——
    用户填好的 Cookie 绝不能被模板冲掉。
    目录选择优先用「已有凭据的目录」，保证打印出来的路径就是实际生效的路径。
    """
    d = find_config_dir() or ensure_config_dir()
    if not d:
        return None
    p = os.path.join(d, CRED_FILENAME)
    if os.path.exists(p) and not force:
        return p
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(CREDENTIAL_TEMPLATE, f, ensure_ascii=False, indent=2)
        return p
    except Exception:  # noqa: BLE001
        return None


def _describe_home(d: str) -> str:
    """单个候选目录的状态标记。

    区分「空模板」与「真凭据」：`配置账号.bat` 写出的模板 cookie 为空，
    若仍标成 ✓有凭据，用户会以为已经配好了，实际却跑在匿名模式。
    """
    p = os.path.join(d, CRED_FILENAME)
    if not os.path.exists(p):
        return "—"
    raw = _read_json(p)
    if raw is None:
        return "✗文件损坏"
    return "✓有凭据" if parse_credentials(raw) else "△空模板"


def describe() -> str:
    """人类可读的配置状态摘要（不含任何凭据值）。"""
    lines = ["凭据目录候选（按优先级，首个 ✓ 生效）："]
    for i, d in enumerate(candidate_homes(), 1):
        lines.append(f"  {i}. [{_describe_home(d)}] {d}")
    lines.append(f"  兼容旧文件: {'存在' if os.path.exists(LEGACY_SESSION) else '不存在'} "
                 f"({LEGACY_SESSION})")
    if os.environ.get(ENV_COOKIE):
        lines.append(f"实际生效来源: 环境变量 {ENV_COOKIE}（未落盘）")
    elif os.environ.get(ENV_CRED_FILE):
        lines.append(f"环境变量 {ENV_CRED_FILE} = {os.environ[ENV_CRED_FILE]}")
    cp = credentials_path()
    lines.append(f"实际生效来源: {cp if cp else '(未配置，匿名模式)'}")
    if cp is None:
        tpl = [d for d in candidate_homes()
               if _describe_home(d) == "△空模板"]
        if tpl:
            lines.append(f"  ⚠ 发现空模板未填写：{tpl[0]}")
    return "\n".join(lines)


if __name__ == "__main__":
    _log("== qqread_config 自检 ==")
    _log(describe())
    ck = load_credentials()
    _log(f"Cookie 数量: {len(ck)}；关键字段: "
         f"{ {k: mask(ck[k]) for k in KEY_FIELDS if k in ck} }")
    _log(f"fkp 缓存路径: {fkp_cache_path()}")
    _log("== 自检结束 ==")
