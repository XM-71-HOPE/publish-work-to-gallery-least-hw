---
name: publish-work-to-gallery-least-hw
description: |
  Publish a work to the Huawei Cloud University Operations Platform (华为云高校运营平台/作品陈列馆) while avoiding
  Huawei's tooling. Use this as a drop-in replacement for publish-work-to-gallery when the user wants to submit to a
  training camp (训练营) or the gallery but does NOT want to install hcloud (KooCLI), use GitCode, open a DevBridge
  tunnel, or run the Playwright/Chromium/font pipeline. Triggers include "把作品发到平台", "投稿到陈列馆",
  "提交作品到训练营", "发布作品但不想装华为 CLI", "least hw 发布", "publish to work gallery without hcloud",
  "submit to training camp, no Huawei tooling". Requires only a Huawei Cloud account and one AK/SK.
metadata:
  tags: huawei-cloud,publish,gallery,university operations platform,least-hw
version: 2026.09.11.004
---

# Publish Work to Gallery (least-hw)

发布作品到华为云高校运营平台，**不安装、不使用华为的任何工具/CLI**。

唯一执行入口：`scripts/gallery_direct.py`（Python 3.8+，零第三方依赖）。判定以 stdout 与退出码为准，成功路径不读源码。

## 与官方 `publish-work-to-gallery` 的差别

| | 官方 skill | 本技能 |
|---|---|---|
| hcloud (KooCLI) | 必需 | **不用**，IAM/STS 全部纯 REST + 自实现 AK/SK 签名 |
| IAM 自委托 `SELF_VERIFY` | 需要 | 需要（网关强制 STS 临时凭证），但由脚本纯 REST 幂等创建，不装 CLI |
| STS 临时凭证 | 需要 | 需要，`sts` 子命令自取（900s） |
| GitCode | 推荐 | **不用**，github/gitee/gitlab 均可 |
| DevBridge 隧道 | 可选 | **不用**（`envUrl` 传空串） |
| Playwright/Chromium/字体/门禁 | 有 | **全不用**，封面由用户提供 |
| 远程版本检查/升级推送 | 有 | 无 |

## 需要用户提供什么

| 项 | 说明 | 从哪来 |
|---|---|---|
| AK / SK | 华为云访问密钥 | 控制台「我的凭证 → 访问密钥 → 新增」（SK 只显示一次） |
| 作品仓库 | `gitUrl` + `gitBranch` | `inspect` 自动扒；须托管在 github/gitee/gitcode/gitlab，且**公开可访问** |
| 作品名 | `workName`，≥1 字符 | `inspect` 给候选，用户确认 |
| 一句话简介 | `introduction`，**15~50 个中文字符** | agent 依 README 代写，用户确认 |
| 封面图 | png/jpeg/webp/gif，< 10MB | 用户提供（本技能不生成封面） |
| 训练营 ID | `trainingCampId` | `camps` 列出，用户选「可投稿」的 |

**凭证纪律**：AK/SK 走 `--creds-file`（推荐）或环境变量，**禁止**让用户把 SK 贴进对话，也**禁止**把 SK 回显到任何输出里。

## Pipeline

```
0 收集    向用户要三样：AK/SK、仓库目录、封面（其余 agent 补）
1 sts      解析账号 ID + 换 STS 临时凭证（委托缺失时才创建）→ sts-creds.json
2 probe    用临时凭证验连通性（应 200）
3 inspect  读仓库，得 gitUrl/branch/name
4 收集      简介(代写) + 作品名(草案) → 交用户确认
5 camps    选可投稿训练营
6 publish  --dry-run → 正式发布
```

### Step 0 — 向用户收集输入（必须先做）

**开场一次性要三样**，并说明其余由 agent 补。可直接照下面的话术问：

> 要发作品了，我需要你给三样（其余我来补）：
> 1. **华为云 AK/SK** —— 控制台「我的凭证 → 访问密钥 → 新增」下载 `credentials.csv`，
>    放进一个文件（如 `~/.gallery-creds.json`，`chmod 600`），**不要贴进对话**。
> 2. **作品仓库目录** —— 本地路径，需已 push 到 GitHub / Gitee / GitCode / GitLab 且**公开**。
> 3. **封面图** —— 一张 png/jpg 的路径（项目截图即可，< 10MB）。
>
> 作品名和简介我从 README 里提炼后给你确认；训练营我列出来你挑。

拿到上面的东西后：`sts` → `probe` → `inspect`，再把 `#name` 和代写的简介草案摆给用户确认。

**缺东西时的动作**：

| 情况 | 怎么做 |
|---|---|
| 用户把 SK 贴进对话 | 不要回显；提示它改用文件（`--creds-file`），并建议作废/重建该密钥 |
| 没有封面 | 让用户截一张项目运行图；实在没有，任意项目相关图也可过（平台只校验格式与大小，不看内容） |
| 仓库没 push 或非公开 | 提示先 push 到公开仓库（平台会实际拉取校验） |
| 仓库不在白名单域名 | 提示迁到 github/gitee/gitcode/gitlab |
| 读不出作品名（`source=dirname`） | agent 提议一个（<30 字符）交用户确认 |
| 训练营无「可投稿」 | 如实告知，建议联系主办方或改期 |

> 平台 `open-api-public` 强制要求请求头带**最小权限 STS 临时凭证**。实测：直接传永久 AK/SK 会被拒
> `400 GALLERY.PARAM.MISSING「缺少最小权限 STS 临时凭证」`；假 token 会被拒 `401 GALLERY.AUTH.UNAUTHORIZED`。
> 所以必须先用 `sts` 换真凭证。临时凭证默认 900s，长流程前重新跑 `sts` 即可。

### Step 1 — sts

```bash
# 换临时凭证（推荐带 --create：缺失委托时自动创建，已存在则跳过，幂等）
python3 <skill>/scripts/gallery_direct.py sts --creds-file <ak.json> --create --out /tmp/sts-creds.json
```

内部顺序：`KeystoneListAuthDomains`（解析账号）→ `STS AssumeAgency`（900s，最小策略 `sts::GetCallerIdentity` + `iam::listAuthDomains`）；
**只有** AssumeAgency 返回"委托不存在"且带 `--create` 时才 `CreateAgency` 建 `SELF_VERIFY` 后重试。
这样设计是为了兼容没有 `iam:agencies:listAgencies` 权限的只读密钥。

> **权限坑**：华为云密钥常常是 **IAM 子用户**，默认无 IAM 写权限。此时 `--create` 会 `403 Policy doesn't allow iam:agencies:createAgency`。
> 解决：让用户用**账号主身份**在 IAM 控制台手动建一个自委托（名称 `SELF_VERIFY`，委托账号填自己账号 ID），之后 `sts` 纯只读即可跑通。
> **创建委托是对用户云账号的写操作，执行前必须说明并获确认。**

成功输出 `#sts=ok expires_at=... out=...`。之后所有命令用 `--creds-file /tmp/sts-creds.json`。

### Step 2 — probe

```bash
python3 <skill>/scripts/gallery_direct.py probe --creds-file /tmp/sts-creds.json
```

`#guest_announcement=200` + `#camps_auth=200` + `#verdict=ok` → 通过。`#verdict=unauthorized` → 重跑 `sts` 刷新。

### Step 3 — inspect

```bash
python3 <skill>/scripts/gallery_direct.py inspect --dir <workDir>
```

输出 `#gitUrl=` / `#gitBranch=` / `#name=` / `#name.source=` / `#readme=` / `#resources=`。`source=dirname` 时作品名是目录名，agent 可提议更合适的名字（<30 字符）交用户确认。
仓库须**公开可访问**（服务端会实际拉取校验），非 github/gitee/gitcode/gitlab 的需先推送。

### Step 4 — 收集简介与作品名（交用户确认）

- **作品名**：Step 3 的 `#name`，`source=dirname` 时 agent 另提一个，**<30 字符**。
- **简介**：读 README 提炼一句话，**15~50 个 CJK 字符**（脚本按此校验）。
- **封面**：本技能不生成。Step 0 已要；缺则按 Step 0 的「没有封面」处理。

把作品名 + 简介草案一起报给用户，两者都确认后才进 `camps`/`publish`。

### Step 5 — camps

```bash
python3 <skill>/scripts/gallery_direct.py camps --year <当前年> --creds-file /tmp/sts-creds.json
```

表含 `状态/ID/学校/名称`。**只有「可投稿」可选**；选其他会 `GALLERY.CAMP.UNAVAILABLE`。

### Step 6 — publish

```bash
python3 <skill>/scripts/gallery_direct.py publish \
  --camp <id> --name "<workName>" --intro "<简介>" \
  --cover <cover.png> --readme <workDir>/README.md [--resources <workDir>/resources] \
  --git <gitUrl> --branch <branch> \
  --creds-file /tmp/sts-creds.json --dry-run      # 去掉 --dry-run 才真发
```

成功 `#status=201` → 取 `workId`/`workUrl`/`reward`，并**无论成功与否都提示**：积分每日仅限一次，余额 https://developer.huaweicloud.com/grow。

失败：
- `409 GALLERY.WORK.DUPLICATE` → 换 `--name` 重发。
- `400 GALLERY.PARAM.INVALID`（含"代码仓库地址无法访问"）→ 仓库非公开或不可达。
- `GALLERY.PARAM.GIT_URL_DOMAIN_FORBIDDEN` → 仓库域名不在白名单。
- 其余错误码原样输出，见 [references/platform-contract.md](references/platform-contract.md)。

## 本地前置校验（脚本已内置）

`introduction` 15~50 CJK / `gitUrl` https+`.git`+白名单 / `gitBranch` 字符 / 封面格式与大小 / 详情 zip ≤20MiB。

## 不做什么

- 不装 hcloud、不装华为 SDK、不调华为 CLI（IAM/STS 由脚本用自实现 AK/SK 签名直连）。
- 不用 GitCode，不用 DevBridge 隧道。
- 不生成封面、不截图、不查字体、不写门禁标记文件。
- 不做官方 skill 的远程版本检查与升级推送。

## References

| 文档 | 用途 |
|---|---|
| [platform-contract.md](references/platform-contract.md) | API 契约、字段约束、错误码、签名与凭证说明 |
| [verified-notes.md](references/verified-notes.md) | 实测记录与踩坑（含零副作用验证方法） |
