# publish-work-to-gallery-least-hw

把作品发到「华为云高校运营平台」（作品陈列馆 / 训练营），**不碰华为的任何工具**。

## 一句话

官方 `publish-work-to-gallery` 里九成代码是给新手兜底的：自动截图、排版封面、下 111MB Chromium、装中文字体、写门禁标记、打包 zip。真正跟"提交"有关的，只有一个 multipart POST。

这九成还顺手把人拖进华为的整套工具链：`hcloud` KooCLI、IAM 委托、STS、GitCode、DevBridge 隧道、一堆版本推送。

本技能只保留平台**真正要求**的两样东西：**一个华为云账号 + 一对 AK/SK**。
IAM/STS 全部用纯 REST 直连（自己实现了华为的 AK/SK 签名），其余一概不要。

> 唯一保留了华为的一层：网关强制要求请求头带 STS **临时**凭证，永久 AK/SK 会被拒。所以需要一个自委托 `SELF_VERIFY` 来换取，但它的创建与换取都由脚本直接调 API 完成，不装任何 CLI。

## 目录

```
SKILL.md                        agent 侧契约：要用户提供什么、六步流程、失败判读
README.md                       本文件
scripts/gallery_direct.py       唯一执行入口（Python 3.8+，零第三方依赖）
references/platform-contract.md API 契约、字段约束、错误码、签名与凭证
references/verified-notes.md    实测记录与踩坑
```

## 它做什么

Agent 驱动，六个子命令：

```
sts      解析账号 ID，换 STS 临时凭证（委托缺失时才创建）
probe    用临时凭证验平台连通性
inspect  读仓库：gitUrl / branch / 候选作品名 / README / resources
camps    列训练营，标注「可投稿 / 未开始 / 进行中 / 已结束」
publish  打包详情 zip，POST 发布（带 --dry-run 预览）
```

## 快速开始（手动跑）

```bash
cd scripts

# 1. 换 STS 临时凭证（首次会用到 --create；已有委托则跳过）
python3 gallery_direct.py sts   --creds-file ~/.gallery-creds.json --create --out /tmp/sts-creds.json

# 2. 验连通
python3 gallery_direct.py probe --creds-file /tmp/sts-creds.json

# 3. 读仓库
python3 gallery_direct.py inspect --dir /path/to/work

# 4. 选训练营
python3 gallery_direct.py camps --year 2026 --creds-file /tmp/sts-creds.json

# 5. 发布（先 --dry-run，确认字段无误再去掉）
python3 gallery_direct.py publish \
  --camp <camp-id> --name "作品名" --intro "十五到五十个中文字符的简介" \
  --cover cover.png --readme README.md --resources ./resources \
  --git https://github.com/you/repo.git --branch main \
  --creds-file /tmp/sts-creds.json --dry-run
```

凭证文件（`~/.gallery-creds.json`，`chmod 600`）：

```json
{ "accessKeyId": "<你的AK>", "secretAccessKey": "<你的SK>" }
```

## 用起来会问你什么

整个流程只会让你给三样东西，其余 agent 自己补：

1. **华为云 AK/SK** —— 控制台「我的凭证 → 访问密钥 → 新增」下载 `credentials.csv`，放进文件（如 `~/.gallery-creds.json`）。**不要贴进对话。**
2. **作品仓库目录** —— 本地路径，已 push 到公开仓库。
3. **封面图** —— 一张 png/jpg 的路径。

会反过来先给你**确认**的：作品名（从 README 扒/提炼）、一句话简介（agent 代写）、训练营（列出来你挑）。

## 平台硬约束（脚本已本地前置校验）

| 字段 | 约束 |
|---|---|
| `introduction` | 15~50 个 CJK 字符 |
| `gitUrl` | `https://` 开头、`.git` 结尾、host 在白名单，且**仓库必须公开可访问**（服务端会实际拉取） |
| `image` | png/jpeg/webp/gif，< 10MB |
| `detail` | zip ≤20MiB，根目录 `README.md`（+ 可选 `resources/`） |
| `trainingCampId` | 必须在「可投稿」窗口内 |
| `workName` | 不与他人重名（重名 409） |

## 踩过的坑

签名三段式、STS 区域端点、IAM 子用户权限、平台会拉仓库……都记在 [`references/verified-notes.md`](references/verified-notes.md)。
里面还有一套**零副作用验证法**：怎么在不往平台留脏数据的前提下把整条链路验穿。

## License

GPL-3.0-or-later，见 [LICENSE](LICENSE)。

注意：GPL 管的是**分发**，不影响**使用**——直接 clone、自己改着用都不受限制；只有在把修改过的版本再发布时才要求同样开源。
