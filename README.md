# qqread-dl · QQ阅读小说下载器（book.qq.com）

纯协议还原、**浏览器无关**的 QQ 阅读小说下载器，支持下载账号**本身有权阅读**的 VIP 章节。

- 不依赖 Selenium / Playwright，只用标准库 `urllib` 发请求
- VIP 章的两层反爬（Fock 加密 + 伪字体）全部离线还原
- 凭据与设备登录信息**全部隔离在项目之外**，仓库内不存在任何密钥

---

## 一、项目介绍

### 1.1 它能做什么

| 步骤 | 说明 |
|---|---|
| 搜索 | 按关键词搜索前 10 本书，结果写入 `output/搜索结果.json` |
| 目录 | 一次拉取全书章节列表（章节名 / cid / 是否免费） |
| 下载 | 5 线程并发取章，**按章序流式追加写入** `output/<书名>.txt`（边下边写，中断不丢已下内容） |
| VIP 还原 | 账号有权限的付费章：先 Fock 解密，再做字体字形还原 |

### 1.2 协议结论（实测）

| 用途 | 接口 |
|---|---|
| 搜索 | `GET https://book.qq.com/api/booksearch/query?keyWord=&pageNo=&pageSize=` |
| 书籍详情 | `GET https://book.qq.com/api/book/detail?bid=` |
| 章节目录 | `GET https://book.qq.com/api/book/detail/chapters?bid=` |
| 章节正文 | `GET https://book.qq.com/api/book/read?bid=&cid=` |

- 读接口无签名、无 Cookie 也可调用；HTTP 恒 200，业务状态在响应体 `code` 字段。
- **免费章**：`data.content` 就是明文全文。
- **付费章**：服务端按账号权限分流——
  - `authStatus=1` + `encrypt=true`：有权限，返回密文，需要本地解密；
  - `authStatus=0`：无权限，服务端固定截断 203 字试读。

### 1.3 VIP 章的两层反爬

**第 1 层：Fock 加密**（`encrypt=true`）

`content` 是 base64 密文，要用 SSR 阅读页里 `fkConfig.fkp` 解出的
（128 字符密钥, 时间戳）+ `fuid` + **章节号 `cid`**，在 Fock 内核里解密。

Fock 库是 Emscripten 的 **wasm2js** 产物（全程不出现 `WebAssembly`、不加载 `.wasm`），
所以能直接在 Node 里离线跑 → `fock_unlock.js`。

两个容易踩的坑：

- `unlock()` 第二个参数必须是**章节号字符串**（如 `"22"`），不是响应里的 `ccid`；传错会解出空串。
- `fkp` 密钥是全局固定的，取一次缓存到凭据目录即可，不必每章重取。

**第 2 层：字体反爬**（`fontEncrypt=true`）

解出的 HTML 里字符是伪码位：扩展 A 区由 `randomFont` 渲染、基本区由 `fixedFont` 渲染。
需要按 `fontResponse` 的 CSS + 两套 TTF 做**字形识别**还原 → `qqread_font.py`。

实现要点（都来自实测调优）：

- 参照字体用 **微软雅黑 msyh**，用 Chamfer 距离做字形匹配，再叠加 **bigram 语言模型**纠错。
- `randomFont` 与 `fixedFont` **每章重新生成**（连 fixedFont 的 URL 版本号都每章变），
  映射绝不能跨章缓存；用错 fixedFont 会出现整段系统性乱码。
- 几何置信度足够高时（`margin > GEO_MARGIN = 1.2`）必须让几何结果**压过**语言模型，
  否则 LM 会把正确的「即」改成「那」、「磨」改成「唐」。
- 语言模型语料只能用**其它书 + 本书的免费章明文**。绝不能把本书上一轮解码出的 txt 喂回去——
  那是解码结果、自带错字，会自我强化（实测错字从 1 处涨到 9 处）。

### 1.4 权限边界（重要）

本项目**只下载当前账号本身有权阅读的章节**：

- `authStatus=1 + encrypt=true` → 正常解密下载；
- `authStatus=0` → 判定无权限，**立即停止整本**；
- 响应 `data` 为空 / 关键字段缺失 → 同样判无权限停下（防止把 203 字试读当正文写进文件）。

不实现、也不提供任何绕过付费或试读截断的手段。

### 1.5 文件说明

| 文件 | 说明 |
|---|---|
| `main.py` | 主程序：交互搜索 → 选书 → 并发下载 → 流式写文件 |
| `qqread_vip.py` | 协议层：会话 Cookie、HTTP 请求、fkp 密钥、调 Node 解密、**权限判定** |
| `qqread_font.py` | 字体层：TTF 解析、CSS 规则、字形识别、语言模型纠错 |
| `qqread_config.py` | 配置层：凭据与密钥的**隔离加载**、脱敏日志、缓存目录 |
| `fock_unlock.js` | Fock 解密器（Node CLI） |
| `lib/fock_glue.js` | Fock 内核（wasm2js 产物，**不可重建，必须随仓库分发**） |
| `一键运行.bat` 等 | Windows 一键执行脚本，见第四节 |

---

## 二、使用方法

### 2.1 最快路径

1. 双击 **`安装依赖.bat`**（只需一次）
2. 双击 **`一键运行.bat`**
3. 按提示输入搜索关键词 → 输入要下载的序号 → 等待完成
4. 结果在 `output/` 目录：`搜索结果.json` 和 `<书名>.txt`

不需要 VIP 内容时，第 1、2 步就够用（未配置账号会自动进入匿名模式，只下免费章）。

### 2.2 命令行方式

```bat
python main.py
```

非交互环境（管道、双击重定向）下会自动使用默认值，保证无参可跑。

单独自检各层：

```bat
python qqread_config.py    :: 凭据加载状态与来源（不打印任何密钥值）
python qqread_font.py      :: 参照字体 / 字形索引 / 解码器能否初始化
python qqread_vip.py       :: 接口连通性、账号信息、Node 与胶水就绪情况
```

### 2.3 下载 VIP 章

VIP 章必须有登录态。步骤：

1. 用浏览器打开 <https://book.qq.com> 并登录；
2. 按 `F12` → `Application / 应用程序` → `Cookies` → 全选复制，或用 Cookie 导出插件导出；
3. 把 Cookie 填进凭据文件（双击 **`配置账号.bat`** 会生成模板并打印路径）；
4. 重新运行 `一键运行.bat`。

凭据文件支持三种格式，自动识别：

```json
{"cookie": "uid=...; fuid=...; usid=..."}                 // A. Cookie 串
{"cookies": [{"name": "uid", "value": "..."}, ...]}       // B. Playwright storage_state
{"uid": "...", "fuid": "...", "usid": "..."}              // C. 扁平字典
```

### 2.4 运行效果参考

| 场景 | 结果 |
|---|---|
| 搜索 | 3 键 × 10 条，写入 `output/搜索结果.json` |
| 免费章（匿名，无会员） | 前 21 章，顺序完整、无乱码 |
| 会员有效期内 | 免费章 + VIP 章（Fock + 字体还原）连续下载 |
| 单章耗时 | 免费章约 0.2 s；VIP 章解密 + 还原约 1–3 s |

字体还原的准确率：以 203 字试读明文做基准回归，**逐字准确率约 97.6%–98.2%**。
残余错字基本都是形近字（几/儿、犬/大、绐/给），单字字形骨架层面几乎一致，只能靠语料兜底。

---

## 三、补环境方法

"补环境"指把**依赖、字体、凭据**这三类外部条件补齐。下面按顺序排查即可。

### 3.1 Python 与依赖

- **Python 3.8 及以上**（实测 3.13）。
- 字体层需要 `numpy` 和 `Pillow`：

  ```bat
  pip install numpy pillow
  ```

- **依赖自举**：`main.py` 启动时会自检这两个包。若当前解释器缺失，
  会自动查找其它已装好依赖的解释器并切换过去重新执行自己。
  也可以显式指定：

  ```bat
  set QQREAD_PY=D:\some\python.exe
  python main.py
  ```

  自举只做一次（用 `QQREAD_BOOTSTRAPPED` 标记防止无限递归）。

### 3.2 Node.js（只有 VIP 章需要）

Fock 解密要跑 `fock_unlock.js`。

- 安装 <https://nodejs.org/>，或 `winget install OpenJS.NodeJS`。
- 脚本会按顺序查找：环境变量 `NODE_EXE` → `PATH` 里的 `node` →
  `C:\Program Files\nodejs\node.exe` → nvm-windows 目录 → Linux/macOS 常见路径。
- 都找不到时显式指定：

  ```bat
  set NODE_EXE=D:\你的路径\node.exe
  ```

### 3.3 参照字体（字形识别的"尺子"）

字形识别需要一款标准中文字体来比对字形，默认用 **微软雅黑**。

- Windows 10/11 自带，无需处理。
- 换机器/换系统时脚本会按顺序探测：环境变量 `QQREAD_REF_FONT` → 微软雅黑 →
  macOS PingFang → Linux Noto CJK / 文泉驿微米黑。
- 全部找不到会**在启动时就明确报错**，而不是等到识别出满屏错字才发现。
- 显式指定：

  ```bat
  set QQREAD_REF_FONT=C:\Windows\Fonts\msyh.ttc
  ```

  ⚠ 换用其它参照字体后字形识别准确率会变化，需要重新做基准回归。

### 3.4 字形索引（首次运行自动重建）

`cache/idx_v5_msyh.pkl` 是参照字体 6763 个字的位图 + 距离变换表，
**首次运行会自动重建**（约数分钟，体积约 200 MB）。

- 它是纯缓存，删掉不影响正确性，下次运行重新生成。
- 仓库不提交它；重建后同一台机器后续运行秒开。

### 3.5 凭据补环境（设备登录信息隔离）

凭据**不放在项目目录**。`qqread_config.py` 按以下优先级寻找，先命中先用：

| 顺序 | 位置 | 适用场景 |
|---|---|---|
| 1 | 环境变量 `QQREAD_COOKIE`（直接给 Cookie 串） | CI / 临时使用，**完全不落盘** |
| 2 | 环境变量 `QQREAD_CREDENTIALS`（凭据 JSON 绝对路径） | 自定义位置 |
| 3 | 环境变量 `QQREAD_HOME`（凭据目录） | 自定义目录 |
| 4 | `%USERPROFILE%\.qqread\credentials.json` | **日常使用推荐**，设备级隔离 |
| 5 | `%APPDATA%\qqread\credentials.json` | 备选 |
| 6 | 项目**上级目录**的 `.qqread\credentials.json`（向上最多 4 层） | 便携 / 本地开发 |
| 7 | 项目目录 `session_cookies.json` | 兼容旧版，已被 `.gitignore` 屏蔽 |

- 密钥缓存 `fkp_cache.json` 同样写入凭据目录，不落在项目内。
- 日志全部经过脱敏（形如 `uid=1234…(len=12)`，只留前 4 位），不会打印完整凭据。
- 匿名模式：找不到任何凭据时仍可运行，只是只能下免费章，并会明确提示。
- 判定标准是**能解析出非空 Cookie**，不是"文件存在"。所以 `配置账号.bat` 生成、
  尚未填写的空模板不会被误报为已配置，也不会遮蔽后面真正填好的凭据
  （`环境自检.bat` 会把它标成 `△空模板`，并在没生效凭据时提示路径）。

### 3.6 运行期文件不会残留

两处中间文件都做了自清理（早期版本会累积，实测攒到 2 GB 以上）：

- `fock_unlock.js`：打补丁后的 glue（约 191 KB/次）在退出前删除，启动时还会清扫同目录陈旧副本。
- `qqread_font.py`：每章伪字体写入 `cache/tmp_fonts/<pid>/`，读入内存后立即删文件，
  `atexit` 兜底清目录，并清扫"进程已不存在"的历史残留。

另外两处缓存是**有界复用**（按 URL / 内容哈希命名，不会无限增长）：

- `cache/fixed_font/*.ttf`：fixedFont 按 URL 哈希缓存（约 470 KB/章，命中即复用）
- `cache/bigram_<sig>.pkl`：语言模型 bigram 磁盘缓存

所有缓存都写在 `cache/`，且已在 `.gitignore` 中。

### 3.7 常见问题

| 现象 | 原因与处理 |
|---|---|
| `未找到参照字体` | 安装微软雅黑，或设 `QQREAD_REF_FONT` 指向任意中文 TTF/TTC |
| `缺少依赖 numpy / Pillow` | `pip install numpy pillow`，或设 `QQREAD_PY` |
| `未找到 node 可执行文件` | 装 Node.js，或设 `NODE_EXE` |
| 只下了前 N 章就停 | 匿名模式；或会员过期；或该书 VIP 章账号无权限（这是设计行为） |
| 提示 `Cookie 已失效` | 重新在浏览器登录 book.qq.com，更新凭据文件 |
| 首次运行卡几分钟无输出 | 正在重建字形索引，属正常 |
| 出现零星形近字错字 | 字形识别的固有局限，见 2.4 |

---

## 四、Windows 一键执行（bat）

| 脚本 | 作用 |
|---|---|
| `一键运行.bat` | **主入口**：检查环境 → 运行 `main.py` |
| `安装依赖.bat` | 安装 `numpy` / `Pillow`，并检测 Node.js |
| `配置账号.bat` | 生成凭据模板、打印凭据文件位置与填写方法 |
| `环境自检.bat` | 依次运行三个模块的自检，排查环境问题 |

想下 VIP 内容：`安装依赖.bat` → `配置账号.bat`（按提示填 Cookie）→ `一键运行.bat`。
只想下免费章：`安装依赖.bat` → `一键运行.bat`。

所有脚本都自带 `chcp 65001`（UTF-8），不依赖系统区域设置。

---

## 五、提交限制

本仓库**只收源码与文档**。抓取产物、凭据、构建产物、大文件一律不入库。

判断标准只有一句：**要提交的东西必须能重新生成，或者本来就是手写的。**

### 5.1 绝不入库（已由 `.gitignore` 强制屏蔽）

| 类别 | 具体内容 | 原因 |
|---|---|---|
| **登录凭据** | `session_cookies.json`、`credentials.json`、`cookies.json`、`fkp_cache.json` | git 历史是永久的。密钥一旦提交，即使后续 `git rm` 也仍能从历史里翻出来 |
| **设备登录信息** | Cookie 导出文件、`storage_state.json`、fkp 密钥缓存 | 同上；且与个人账号强绑定 |
| **下载产物** | `output/*.txt`、`output/搜索结果.json` | 体积大、可重新抓取，且含正文内容 |
| **运行时缓存** | `cache/`（`idx_v5_msyh.pkl`、`fixed_font/`、`tmp_fonts/`、`bigram_*.pkl`） | 可重建，单个数百 MB |
| **逆向取证样本** | 原始响应、伪字体、CSS 规则、探针脚本、分析报告 | 研究过程材料，且可能含正文切片 |
| **构建与临时物** | `__pycache__/`、`*.pyc`、`.tmp/`、`*.log` | 无意义噪声 |
| **大文件** | `*.zip` / `*.7z` / `*.rar` / `*.pkl` | GitHub 单文件上限 100 MB；一旦入库会永久留在 git 对象里 |

### 5.2 必须入库

- 全部源码：`main.py`、`qqread_vip.py`、`qqread_font.py`、`qqread_config.py`、`fock_unlock.js`
- 文档：`README.md`
- 批处理脚本：`*.bat`
- **`lib/fock_glue.js`**：Fock 内核（wasm2js 产物），**不可重建，缺失则 VIP 章无法解密**，必须随仓库分发

### 5.3 提交前自查

```bat
git status --short          :: 确认没有凭据 / output / cache 出现在待提交列表
git diff --cached --stat    :: 确认改动范围符合预期
```

仓库根目录的 `.gitignore` 与本项目目录下的 `.gitignore` 双层生效。
即便如此，**提交前请再肉眼确认一次待提交文件清单**——
自动规则挡的是已知模式，挡不住新类型的意外文件。

### 5.4 密钥处理原则

1. **不明文写死在代码里**：凭据只从环境变量或项目外的凭据目录读取。
2. **不落盘（可选）**：用 `QQREAD_COOKIE` 环境变量时凭据完全不写磁盘。
3. **日志脱敏**：所有对外日志经 `mask()` 处理，只保留前 4 位。
4. **项目内零凭据**：项目目录不保留任何真实凭据，`git add .` 不可能把凭据带进仓库。
5. **失效即更新**：Cookie 有有效期，失效后重新导出即可，历史凭据不做任何备份入库。

---

## 六、免责声明

本项目仅用于**协议研究与个人学习**，用于下载本人账号有权访问的内容。

请勿用于商业用途或大规模爬取。使用者应自行遵守目标站点的服务条款与相关法律法规，
因使用本项目产生的任何后果由使用者自行承担。

## 七、编码

全部文件统一 **UTF-8**。
