# 实测记录（verification notes）

从零打通这条链路时踩到和验证到的点，附证据。给自己和后来者看。

---

## 0. 零副作用验证法（先看这个）

不往平台留任何脏数据，也能把整条链路验穿。两步：

**① 本地 mock 端到端** —— 起一个假服务端，验证客户端本身：多部件边界、8 个表单字段、文件名、`201` 响应解析。
```bash
GALLERY_API_HOST=127.0.0.1 GALLERY_API_PROTOCOL=http GALLERY_API_PORT=18080 \
python3 scripts/gallery_direct.py publish --camp camp-mock ... 
# 期望 #status=201，workId/url/reward 正确解析
```

**② 真实生产 + 假 camp id** —— 这一发**真的打到生产**，会走完「临时凭证鉴权 → 上传封面/zip → 字段校验」，只是在 camp 校验处被挡下：
```
POST 生产，camp=00000000-0000-0000-0000-000000000000
→ 400 GALLERY.CAMP.UNAVAILABLE
```
拿到 `CAMP.UNAVAILABLE` 就说明：鉴权过了、多部件上传对了、gitUrl 校验过了，**且没有创建任何记录**。
拿不到（例如 `401`）说明更前面的环节有问题，先修那里。

任何"改真实系统"的动作之前，先跑这两步。

---

## 1. 网关强制 STS 临时凭证，永久 AK/SK 无效

平台 `/open-api-public` 的鉴权，实测：

| 发送方式 | 平台响应 |
|---|---|
| 只传永久 AK/SK，不带 token | `400 GALLERY.PARAM.MISSING「缺少最小权限 STS 临时凭证」` |
| 永久 AK/SK + 假 token | `401 GALLERY.AUTH.UNAUTHORIZED` |
| 真 STS 临时凭证 | `200` |

结论：网关卡的是**真临时凭证**。永久 AK/SK 无法直接当临时凭证，必须经委托换来。

---

## 2. 华为 AK/SK 签名是**三段式**

官方《API签名认证机制示例》里，待签字符串是：

```
SDK-HMAC-SHA256
<X-Sdk-Date>
<SHA256(规范请求)>
```

**不是**业界（AWS/腾讯等）常见的一段或两段。按两段式签，会稳定得到
`verify ak sk signature failed`，而且服务端回显的 `canonical_request` 和你构造的**完全一致**
—— 因为差的不是规范请求，是待签串的组装。这个坑很隐蔽：比对规范请求永远找不出问题。

细节：
- 规范 URI 末尾要补 `/`（`/vpcs` → `/vpcs/`）。服务端回显的也是带斜杠的。
- `X-Sdk-Date` 格式 `YYYYMMDDTHHMMSSZ`，UTC，15 分钟窗口内。
- 必签 `host`；POST 再加 `content-type: application/json`。

---

## 3. STS 是**区域级端点**

- `sts.myhuaweicloud.com` → DNS 解析到 `43.254.0.22`，**TLS 握手超时**，不可达。
- `sts.cn-north-4.myhuaweicloud.com` → `120.46.247.26`，正常。

所以官方 skill 里 `hcloud ... --cli-region=cn-north-4` 不是摆设：KooCLI 按 region 拼 STS 端点。
IAM 是全局 `iam.myhuaweicloud.com`，不受影响。

---

## 4. 平台会**实际拉取**你的 git 仓库

传一个不存在的地址 `https://github.com/x/y.git`：
```
400 GALLERY.PARAM.INVALID
message: 代码仓库地址无法访问，请检查地址是否正确
reason:  代码仓库不可公开访问，请确认仓库为公开仓库（所有人可见）后重试
```
换成真实公开仓库 `https://github.com/octocat/Hello-World.git` 后，才继续走到 camp 校验。
**结论：gitUrl 必须是公开可访问的仓库。**

---

## 5. 密钥常常是 **IAM 子用户**，且默认没权限

用 `GetCallerIdentity` 看身份：
```
principal_urn = iam::<账号ID>:user:<用户名>
```
是 `:user:` 而非 `:root:`，说明是 IAM 子用户。子用户默认策略下：

| 动作 | 结果 |
|---|---|
| `CreateAgency` | `403 Policy doesn't allow iam:agencies:createAgency` |
| `ListAgencies` | `403` |
| `ListUsers` | `403` |
| `KeystoneListAuthDomains` | `200` |
| `STS AssumeAgency`（对已存在的委托） | `200` |

**关键推论**：这个身份**不能建委托，但能扮演已存在的委托**。
所以只要委托建好，链路就通。委托由**账号主身份**在 IAM 控制台建一次即可（名称 `SELF_VERIFY`，委托账号填自己账号 ID）。

这也解释了一个设计取舍：`sts` 子命令必须**先直接 AssumeAgency**，
不能先 `ListAgencies`（子用户没这个权限，会把"403 无权限"误当成"没有委托"）。

---

## 6. 临时凭证 900s

`AssumeAgency` 默认 900s。`sts` 每次执行都重新换一份。`probe`/`publish` 报 `401` 时重跑 `sts` 即可。
900s 足够一发发布；长流程（官方 skill 那种）需要在发布前重新换。

---

## 7. 官方 skill 到底怎么做的

一句话：它也是调同样的 API，只是套了 `hcloud`。

```
一次性：hcloud IAM CreateAgency --agency.name=SELF_VERIFY --agency.domain_id=<账号ID> --agency.trust_domain_id=<账号ID>
每次：  hcloud IAM KeystoneListAuthDomains → domain_id
        hcloud STS AssumeAgency --cli-region=cn-north-4 --agency_urn=iam::<账号ID>:agency:SELF_VERIFY \
             --duration_seconds=900 --policy=<最小策略>      → sts-creds.json
        api.mjs 把 AK/SK/Token 塞进 X-Tmp-* 请求头发给 gallery
```

它没有魔法，也不比本技能多做了什么。它的"建委托"是文档里的一次性手动步骤，且隐式假设调用方有 IAM 写权限（在华为 CodeArts / AI DevSpace 沙箱里通常成立）。
换成一个默认权限的 IAM 子用户，**官方 skill 会卡在同一行**。
