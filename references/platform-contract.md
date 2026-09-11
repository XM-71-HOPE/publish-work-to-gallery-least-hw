# Platform Contract & Fallbacks

契约提炼自平台 open-api（与官方 skill 的 api-spec.md 同源）。本文件是 least-hw 版的单一事实源。

Base：`https://gallery.developer.huaweicloud.com`
前缀：`/open-api-public`（需身份）/ `/open-api-guest`（免鉴权）。

## 端点

| 方法 | 路径 | 前缀 | 用途 |
|---|---|---|---|
| GET | `/v1/gallery/announcements/current` | guest | 连通性探测 |
| GET | `/v1/gallery/camps` | public | 训练营列表 |
| POST | `/v1/gallery/works` | public | 发布作品 |

## 身份

`/open-api-public` **强制**要求请求头带**最小权限 STS 临时凭证三件套**：

```
X-Tmp-Ak: <临时 AK>
X-Tmp-Sk: <临时 SK>
X-Security-Token: <SecurityToken>
```

网关用临时凭证调 `STS.GetCallerIdentity` 得账号（Domain）ID，注入 `X-Domain-Id`/`X-Domain-Hash`。

**实测结论**（用真实账号验证过）：
- 只传永久 AK/SK、不带 token → `400 GALLERY.PARAM.MISSING「缺少最小权限 STS 临时凭证」`（网关先做存在性检查）。
- 带假 token → `401 GALLERY.AUTH.UNAUTHORIZED`（`GetCallerIdentity` 验签失败）。
- 因此**必须**换真临时凭证。永久 AK/SK 无法直接当临时凭证用，需要委托。

获取临时凭证的两条路：
1. **本技能 `sts` 子命令**：纯 REST 调 IAM `CreateAgency`/`ListAgencies` + STS `AssumeAgency`，AK/SK 自签名，不装 CLI。
2. 官方 gen_sts.py（需装 hcloud），产出 `sts-creds.json` 后同样喂给本技能。

## 华为云 AK/SK 签名（脚本内置，自我实现）

直连 IAM/STS 需要自实现签名。华为的规范（官方《API签名认证机制示例》）：

```
规范请求 =
  METHOD\n
  规范URI\n            # 路径百分号编码，末尾补 "/"（/vpcs → /vpcs/）
  规范查询串\n         # 参数按 key 排序后拼接
  规范消息头\n         # 小写名，按名排序，"name:value\n" 各一行，块尾带 \n
  SignedHeaders\n     # 小写名按名排序，" ; " 连接
  HexEncode(SHA256(body))

待签字符串 =
  SDK-HMAC-SHA256\n
  <X-Sdk-Date>\n       # ★ 关键：中间这段日期，漏了就永远验签失败
  HexEncode(SHA256(规范请求))

签名 = HexEncode(HMAC-SHA256(SK, 待签字符串))
Authorization: SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=<...>, Signature=<签名>
```

必带 `X-Sdk-Date`（`YYYYMMDDTHHMMSSZ` UTC，15 分钟窗口内）和 `Host`；POST 再加 `Content-Type: application/json`。

## POST /v1/gallery/works

multipart/form-data。

| 字段 | 必填 | 约束 |
|---|---|---|
| `trainingCampId` | 是 | 训练营 ID，须处于「可投稿」窗口 |
| `workName` | 是 | ≥1 字符 |
| `introduction` | 是 | 15~50 字符 |
| `image` | 是（文件） | 封面，png/jpeg/webp/gif，< 10MB |
| `detail` | 是（文件） | zip ≤20MiB，根目录 `README.md`（+ 可选 `resources/`），README 图片须用 `resources/<file>` 相对路径 |
| `gitUrl` | 是 | `https://` 开头、`.git` 结尾，host 须在白名单；**服务端会实际访问仓库校验可达性，必须是公开仓库**（否则 `GALLERY.PARAM.INVALID`） |
| `gitBranch` | 是 | 字母/数字/`_`/`-`/`.`/`/`，1~250 |
| `envUrl` | 是 | 可为空串 `""` |

请求头 `Idempotency-Key`（8~128 字符）可选，同 key 同 body 重试返回原结果。

成功：`201`，`data.work.id/name`、`data.workUrl`、`data.reward`。
作品状态从 `pending_review` 开始，审核后上架。

## 训练营投稿状态判定

| 条件 | 状态 | 可投稿 |
|---|---|---|
| today < startsAt | 未开始 | 否 |
| startsAt ≤ today ≤ endsAt 且 status=published | 可投稿 | 是 |
| startsAt ≤ today ≤ endsAt 且 status≠published | 进行中 | 否 |
| today > endsAt | 已结束 | 否 |

## 错误码

| HTTP | 码 | 处理 |
|---|---|---|
| 400 | `GALLERY.PARAM.MISSING/INVALID` | 补/改参数 |
| 400 | `GALLERY.PARAM.INVALID` | 参数非法，含“代码仓库地址无法访问”（仓库非公开/不可达）|
| 400 | `GALLERY.PARAM.GIT_URL_INVALID` | 改 `https://`+`.git` |
| 400 | `GALLERY.PARAM.GIT_URL_DOMAIN_FORBIDDEN` | 换 github/gitee/gitcode/gitlab |
| 400 | `GALLERY.PARAM.IMAGE_INVALID` | 封面换 png/jpg，<10MB |
| 400 | `GALLERY.CAMP.UNAVAILABLE` | 换「可投稿」训练营（未开始/进行中/已结束均拒）|
| 401 | `GALLERY.AUTH.UNAUTHORIZED` | 见下方凭证回退 |
| 409 | `GALLERY.WORK.DUPLICATE` | 改 `workName` 重发 |
| 409 | `GALLERY.IDEMPOTENCY.CONFLICT` | 同 key 不同 body，换 key |
| 429 | `GALLERY.RATE_LIMIT.PUBLISH` | 稍后重试 |
| 500 | `GALLERY.SYSTEM.INTERNAL` | 稍后重试 |

## 相关端点

| 服务 | 方法/路径 | 用途 |
|---|---|---|
| IAM | `GET /v3/auth/domains` | 解析账号 ID（KeystoneListAuthDomains） |
| IAM | `GET /v3.0/OS-AGENCY/agencies?domain_id=<id>` | 列委托 |
| IAM | `POST /v3.0/OS-AGENCY/agencies` | 建自委托 `SELF_VERIFY` |
| STS | `POST /v5/agencies/assume` | 换临时凭证（900s） |

> 主机：IAM 为全局 `iam.myhuaweicloud.com`；**STS 为区域级** `sts.<region>.myhuaweicloud.com`（如 `sts.cn-north-4.myhuaweicloud.com`）。全局 `sts.myhuaweicloud.com` 不可达。官方 skill 的 `--cli-region=cn-north-4` 就是为此。

`sts` 子命令全自动完成以上四步，幂等（已有委托则跳过创建）。

## 凭证过期

临时凭证 900s 过期。`probe` 或 `publish` 返回 `401` 时，重跑 `sts`（保留 `--create` 无害，已存在会跳过创建）刷新 `/tmp/sts-creds.json`，再用新文件重试。长流程可在 publish 前重新 `sts` 一次。

## reward 注意事项

发布成功时代领「成长积分」。**当日重复领取仍返回 `is_success: true` 但不到账**，无法从响应判断是否真正入账。因此无论结果如何都要提示「每日仅限一次」，余额 https://developer.huaweicloud.com/grow。
