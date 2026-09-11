#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""gallery_direct.py — 直连华为云高校运营平台（least-hw 版）

publish-work-to-gallery-least-hw 技能的唯一执行入口。
相比官方 skill：不装 hcloud、不调华为 CLI、不用 GitCode、不用 DevBridge 隧道、
不用 Playwright/Chromium/字体门禁。
只保留：华为云账号 + 一对 AK/SK；IAM/STS 用纯 REST 自实现 AK/SK 签名直连。

契约来源：平台 open-api（见 references/platform-contract.md）。
本脚本对平台硬约束做本地前置校验，fail-fast，stdout 即判定依据。

子命令：
  probe    连通性 + 凭证探测（验证永久 AK/SK 能否直连，核心假设）
  inspect  读仓库：git 地址/分支/候选作品名/README/resources
  camps    列训练营并标注可投稿状态
  publish  打包详情 zip 并发布

退出码：0 成功；1 平台或校验失败；2 参数错误。
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qsl, quote

IAM_HOST = "iam.myhuaweicloud.com"
STS_REGION = "cn-north-4"  # STS 为区域级端点：sts.<region>.myhuaweicloud.com

HOST = os.environ.get("GALLERY_API_HOST", "gallery.developer.huaweicloud.com")
PROTOCOL = os.environ.get("GALLERY_API_PROTOCOL", "https").lower()
_DEFAULT_PORT = 443 if PROTOCOL == "https" else 80
PORT = int(os.environ.get("GALLERY_API_PORT") or _DEFAULT_PORT)
BASE = f"{PROTOCOL}://{HOST}" if PORT == _DEFAULT_PORT else f"{PROTOCOL}://{HOST}:{PORT}"
PREFIX_PUBLIC = "/open-api-public"
PREFIX_GUEST = "/open-api-guest"
TIMEOUT = 30

ALLOWED_GIT_HOSTS = ("github.com", "gitcode.com", "gitee.com", "gitlab.com")
CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def err(msg):
    print(f"❌ {msg}", file=sys.stderr)


def ok(msg):
    print(f"✅ {msg}")


def count_cjk(s):
    return sum(1 for ch in s if any(lo <= ord(ch) <= hi for lo, hi in CJK_RANGES))


# ---------------- 凭证 ----------------
def load_creds(args, required=True):
    ak = getattr(args, "ak", None) or os.environ.get("GALLERY_AK")
    sk = getattr(args, "sk", None) or os.environ.get("GALLERY_SK")
    token = getattr(args, "token", None) or os.environ.get("GALLERY_STS_TOKEN") or ""
    cf = getattr(args, "creds_file", None)
    if cf and Path(cf).is_file():
        c = json.loads(Path(cf).read_text(encoding="utf-8"))
        ak = ak or c.get("accessKeyId") or c.get("access_key_id") or c.get("AK")
        sk = sk or c.get("secretAccessKey") or c.get("secret_access_key") or c.get("SK")
        token = token or c.get("securityToken") or c.get("security_token") or c.get("token") or ""
    if not ak or not sk:
        if not required:
            return None, None, ""
        err("缺少 AK/SK。优先用 --creds-file <json>；或 --ak/--sk；或环境变量 GALLERY_AK/GALLERY_SK。")
        sys.exit(2)
    return ak, sk, token


def _auth_headers(ak, sk, token):
    h = {"X-Tmp-Ak": ak, "X-Tmp-Sk": sk}
    if token:
        h["X-Security-Token"] = token
    return h


# ---------------- HTTP ----------------
def _try_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http(method, path, prefix, ak=None, sk=None, token="", query="", data=None, headers=None):
    url = f"{BASE}{prefix}{path}" + (("?" + query) if query else "")
    h = dict(headers or {})
    if ak and sk:
        h.update(_auth_headers(ak, sk, token))
    req = urlrequest.Request(url, method=method, headers=h, data=data)
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, _try_json(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        return e.code, _try_json(e.read().decode("utf-8", "replace"))
    except URLError as e:
        err(f"连接失败：{e.reason}")
        sys.exit(1)


def http_multipart(path, prefix, ak, sk, token, fields, files, idem_key):
    boundary = "----gallerydirect" + uuid.uuid4().hex
    chunks = []

    def add(s):
        chunks.append(s.encode("utf-8") if isinstance(s, str) else s)

    for name, value in fields.items():
        add(f"--{boundary}\r\n")
        add(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        add(f"{value}\r\n")
    for name, p in files.items():
        p = Path(p)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        add(f"--{boundary}\r\n")
        add(f'Content-Disposition: form-data; name="{name}"; filename="{p.name}"\r\n')
        add(f"Content-Type: {ctype}\r\n\r\n")
        add(p.read_bytes())
        add("\r\n")
    add(f"--{boundary}--\r\n")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": idem_key}
    headers.update(_auth_headers(ak, sk, token))
    req = urlrequest.Request(f"{BASE}{prefix}{path}", data=b"".join(chunks), method="POST", headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, _try_json(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        return e.code, _try_json(e.read().decode("utf-8", "replace"))
    except URLError as e:
        err(f"连接失败：{e.reason}")
        sys.exit(1)


# ---------------- 校验 ----------------
def validate_git_url(url):
    if not url.startswith("https://") or not url.endswith(".git"):
        err(f"gitUrl 必须 https:// 开头、.git 结尾：{url}")
        return False
    host = re.sub(r"^https://", "", url).split("/")[0].split("@")[-1].lower()
    if not any(h in host for h in ALLOWED_GIT_HOSTS):
        err(f"gitUrl host `{host}` 疑似不在白名单（github/gitcode/gitee/gitlab）：{url}")
        return False
    return True


def validate_branch(b):
    return bool(re.fullmatch(r"[A-Za-z0-9_\-./]{1,250}", b or ""))


def build_zip(readme, resources_dir, out):
    readme = Path(readme)
    if not readme.is_file():
        err(f"README 不存在：{readme}")
        sys.exit(2)
    out = Path(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(readme, "README.md")
        if resources_dir and Path(resources_dir).is_dir():
            res = Path(resources_dir)
            for f in sorted(res.rglob("*")):
                if f.is_file():
                    z.write(f, str(Path("resources") / f.relative_to(res)))
    if out.stat().st_size > 20 * 1024 * 1024:
        err("详情 zip 超过 20MiB 上限")
        sys.exit(1)
    ok(f"详情 zip：{out}（{out.stat().st_size/1024:.0f} KiB）")
    return str(out)


# ---------------- 子命令 ----------------
def cmd_probe(args):
    ak, sk, token = load_creds(args, required=False)
    st, _ = http("GET", "/v1/gallery/announcements/current", PREFIX_GUEST)
    print(f"#guest_announcement={st}")
    if not ak:
        print("#creds=missing")
        print("提供 AK/SK 后重跑 probe 验证凭证。")
        return
    st, body = http("GET", "/v1/gallery/camps", PREFIX_PUBLIC, ak, sk, token, query="pageNo=1&pageSize=1")
    print(f"#camps_auth={st} token={'yes' if token else 'no'}")
    if st == 200:
        print("#verdict=ok")
        if not token:
            ok("网关接受永久 AK/SK（无 SecurityToken），无需 hcloud / 委托 / STS。")
    elif st == 401:
        code = body.get("code") if isinstance(body, dict) else ""
        print(f"#verdict=unauthorized code={code}")
        err("网关要求 STS 临时凭证。回退方案见 references/platform-contract.md#凭证回退。")
        sys.exit(1)
    else:
        print(f"#verdict=http_{st}")
        print(json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, dict) else body)
        sys.exit(1)


def cmd_inspect(args):
    d = Path(args.dir).resolve()
    if not d.is_dir():
        err(f"目录不存在：{d}")
        sys.exit(2)

    def git(*a):
        try:
            r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""

    remote = git("remote", "get-url", "origin")
    branch = git("branch", "--show-current") or "main"
    # ssh → https，剥离凭证
    url = re.sub(r"^git@([^:]+):", r"https://\1/", remote)
    url = re.sub(r"^(https?://)[^@/]*@", r"\1", url)

    name, source = "", ""
    for f in ("README.md", "readme.md", "README.MD"):
        p = d / f
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^---\s*\n(.*?)\n---", txt, re.S)
            if m:
                t = re.search(r"^title\s*:\s*(.+)$", m.group(1), re.M)
                if t:
                    name, source = t.group(1).strip().strip("\"'"), "frontmatter"
                    break
            h1 = re.search(r"^#\s+(.+)$", txt, re.M)
            if h1:
                name, source = h1.group(1).strip(), "README-H1"
                break
    if not name:
        for cand in ("package.json", "manifest.json"):
            p = d / cand
            if p.is_file():
                try:
                    j = json.loads(p.read_text(encoding="utf-8"))
                    if j.get("name"):
                        name, source = j["name"], cand
                        break
                except Exception:
                    pass
    if not name:
        for cand in ("index.html", "src/index.html"):
            p = d / cand
            if p.is_file():
                t = re.search(r"<title>([^<]+)</title>", p.read_text(encoding="utf-8", errors="replace"), re.I)
                if t:
                    name, source = t.group(1).strip(), "html-title"
                    break
    if not name:
        name, source = d.name, "dirname"

    readme_found = any((d / f).is_file() for f in ("README.md", "readme.md"))
    resources_found = (d / "resources").is_dir() or (d / "static").is_dir() or (d / "public").is_dir()

    print(f"#gitUrl={url}")
    print(f"#gitBranch={branch}")
    print(f"#name={name}")
    print(f"#name.source={source}")
    print(f"#readme={'yes' if readme_found else 'no'}")
    print(f"#resources={'yes' if resources_found else 'no'}")


def cmd_camps(args):
    ak, sk, token = load_creds(args)
    q = "pageNo=1&pageSize=100"
    if args.year:
        q += f"&periodYear={args.year}"
    st, body = http("GET", "/v1/gallery/camps", PREFIX_PUBLIC, ak, sk, token, query=q)
    if st != 200:
        err(f"HTTP {st}: {json.dumps(body, ensure_ascii=False)[:600] if isinstance(body, dict) else body}")
        sys.exit(1)
    items = (body.get("data") or {}).get("items", [])
    today = time.strftime("%Y-%m-%d")
    print(f"{'状态':<6} {'ID':<28} {'学校':<14} 名称")
    for it in items:
        s, e, stat = it.get("startsAt", ""), it.get("endsAt", ""), it.get("status", "")
        if today < s:
            label = "未开始"
        elif s <= today <= e and stat == "published":
            label = "可投稿"
        elif s <= today <= e:
            label = "进行中"
        else:
            label = "已结束"
        print(f"{label:<6} {it.get('id',''):<28} {it.get('school') or '—':<14} {it.get('name','')}")


def cmd_publish(args):
    # dry-run 不需要凭证，便于先预览请求
    ak, sk, token = load_creds(args, required=not args.dry_run)
    cjk = count_cjk(args.intro or "")
    if not (15 <= cjk <= 50):
        err(f"introduction CJK 字符数 {cjk} 不在 15~50")
        sys.exit(1)
    if not validate_git_url(args.git):
        sys.exit(1)
    if not validate_branch(args.branch):
        err(f"gitBranch 不合法：{args.branch}")
        sys.exit(1)
    cover = Path(args.cover)
    if not cover.is_file():
        err(f"封面不存在：{cover}")
        sys.exit(2)
    if cover.stat().st_size > 10 * 1024 * 1024:
        err("封面超过 10MB")
        sys.exit(1)
    if cover.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        err(f"封面格式不支持：{cover.suffix}")
        sys.exit(1)

    zip_path = args.zip or build_zip(args.readme, args.resources, args.zip_out)
    if not zip_path:
        err("缺 --zip 或 --readme")
        sys.exit(2)

    fields = {
        "trainingCampId": args.camp, "workName": args.name, "introduction": args.intro,
        "gitUrl": args.git, "gitBranch": args.branch, "envUrl": args.env_url or "",
    }
    files = {"image": str(cover), "detail": zip_path}
    idem = args.idem or f"gallery-publish-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

    if args.dry_run:
        print(f"#dry_run=yes idempotency_key={idem}")
        print(f"POST {BASE}{PREFIX_PUBLIC}/v1/gallery/works")
        for k, v in fields.items():
            print(f"  form {k} = {v}")
        print(f"  file image  = {cover}")
        print(f"  file detail = {zip_path}")
        print(f"  headers X-Tmp-Ak/X-Tmp-Sk{'/X-Security-Token' if token else ''}")
        return

    print(f"开始发布作品「{args.name}」…")
    st, body = http_multipart("/v1/gallery/works", PREFIX_PUBLIC, ak, sk, token, fields, files, idem)
    print(f"#status={st}")
    if st == 201 and isinstance(body, dict):
        d = body.get("data") or {}
        w = d.get("work") or {}
        r = d.get("reward") or {}
        print(f"workId={w.get('id','')}")
        print(f"workName={w.get('name', args.name)}")
        print(f"workUrl={w.get('workUrl') or d.get('workUrl','')}")
        if r:
            print(f"reward={'success' if r.get('is_success') else 'failed'} value={r.get('value','')} msg={r.get('error_msg','')}")
        ok("发布成功，作品进入待审核。")
        print("⚠️ 积分每日仅限一次；余额：https://developer.huaweicloud.com/grow")
        return
    print(json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, dict) else body)
    code = body.get("code") if isinstance(body, dict) else ""
    if st == 409 and code == "GALLERY.WORK.DUPLICATE":
        err("作品重名（409）。换 --name 重发即可。")
    sys.exit(1)


def _hw_canon_uri(path):
    # 华为规范 URI：路径百分号编码，且末尾必须带 "/"（/vpcs -> /vpcs/）
    p = quote(path, safe="/~")
    return p if p.endswith("/") else p + "/"


def _hw_canon_query(q):
    if not q:
        return ""
    pairs = sorted(parse_qsl(q, keep_blank_values=True))
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def _hw_sign(method, url, headers, body_bytes, ak, sk):
    """华为云 SDK-HMAC-SHA256 签名。

    待签字符串为三段（官方规范）：
        SDK-HMAC-SHA256\n<X-Sdk-Date>\n<SHA256(规范请求)>
    """
    u = urlparse(url)
    signed = {k.lower(): v.strip() for k, v in headers.items()}
    signed_headers = ";".join(sorted(signed))
    canon_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    payload_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    canonical_request = "\n".join([method, _hw_canon_uri(u.path), _hw_canon_query(u.query),
                                   canon_headers, signed_headers, payload_hash])
    hashed_canon = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "SDK-HMAC-SHA256\n" + headers["X-Sdk-Date"] + "\n" + hashed_canon
    sig = hmac.new(sk.encode(), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"SDK-HMAC-SHA256 Access={ak}, SignedHeaders={signed_headers}, Signature={sig}"


def hw_request(method, url, ak, sk, body=None, timeout=20):
    """带华为云 AK/SK 签名的请求。body 为 dict 时自动 JSON 序列化。"""
    u = urlparse(url)
    headers = {"Host": u.netloc}
    body_bytes = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers["X-Sdk-Date"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    headers["Authorization"] = _hw_sign(method, url, headers, body_bytes, ak, sk)
    req = urlrequest.Request(url, data=body_bytes, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as r:
            return r.status, _try_json(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        return e.code, _try_json(e.read().decode("utf-8", "replace"))
    except URLError as e:
        err(f"连接失败：{e.reason}")
        sys.exit(1)


def _short(x, n=400):
    return json.dumps(x, ensure_ascii=False)[:n] if isinstance(x, (dict, list)) else str(x)[:n]


def _save_sts_creds(out_path, creds, domain_id, agency_urn):
    out = {"accessKeyId": creds["access_key_id"], "secretAccessKey": creds["secret_access_key"],
           "securityToken": creds["security_token"],
           "_refresh": {"accountId": domain_id, "agencyUrn": agency_urn}}
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.chmod(out_path, 0o600)
    print(f"#sts=ok expires_at={creds.get('expiration','')} out={out_path}")


def cmd_sts(args):
    """解析账号 ID，并通过 SELF_VERIFY 自委托换取 STS 临时凭证。

    策略：先直接 AssumeAgency（对无 ListAgencies 权限的只读密钥也有效）；
    仅当返回“委托不存在”且带 --create 时才创建委托后重试。
    """
    ak, sk, _ = load_creds(args)

    st, body = hw_request("GET", f"https://{IAM_HOST}/v3/auth/domains", ak, sk)
    domains = (body or {}).get("domains") if isinstance(body, dict) else None
    if st != 200 or not domains:
        err(f"解析账号失败（HTTP {st}）：{_short(body)}")
        sys.exit(1)
    domain_id = domains[0].get("id")
    print(f"#domain={domain_id} name={domains[0].get('name','')}")

    sts_host = f"sts.{args.region}.myhuaweicloud.com"
    agency_urn = f"iam::{domain_id}:agency:SELF_VERIFY"
    policy = json.dumps({"Version": "5.0", "Statement": [
        {"Effect": "Allow", "Action": ["sts::GetCallerIdentity", "iam::listAuthDomains"], "Resource": ["*"]}]})
    assume_body = {"agency_urn": agency_urn, "agency_session_name": f"gallery-{int(time.time())}",
                   "duration_seconds": 900, "policy": policy}

    def assume():
        return hw_request("POST", f"https://{sts_host}/v5/agencies/assume", ak, sk, body=assume_body)

    st, body = assume()
    msg = _short(body)
    ok_creds = st == 200 and isinstance(body, dict) and body.get("credentials")
    if ok_creds:
        _save_sts_creds(args.out, body["credentials"], domain_id, agency_urn)
        return

    not_found = "cannot be found" in msg or "STS5.1106" in msg
    if not_found and not args.create:
        print("#verdict=need_agency")
        print("账号下没有 SELF_VERIFY 委托。可在 IAM 控制台创建，或加 --create 由脚本创建。")
        return
    if not_found and args.create:
        print("创建自委托 SELF_VERIFY …")
        create = {"agency": {"name": "SELF_VERIFY", "domain_id": domain_id, "trust_domain_id": domain_id,
                             "description": "least-hw 发布最小权限自委托"}}
        st2, b2 = hw_request("POST", f"https://{IAM_HOST}/v3.0/OS-AGENCY/agencies", ak, sk, body=create)
        if st2 not in (200, 201):
            err(f"创建委托失败（HTTP {st2}）：{_short(b2)}")
            sys.exit(1)
        print("✅ 已创建 SELF_VERIFY")
        st, body = assume()
        if st == 200 and isinstance(body, dict) and body.get("credentials"):
            _save_sts_creds(args.out, body["credentials"], domain_id, agency_urn)
            return
        err(f"AssumeAgency 失败（HTTP {st}）：{_short(body)}")
        sys.exit(1)
    err(f"AssumeAgency 失败（HTTP {st}）：{msg}")
    sys.exit(1)


def add_creds_args(parser):
    # default=SUPPRESS：子命令未显式提供时不覆盖父解析器已解析的值，
    # 使 --creds-file 放在子命令前或后都能生效。
    parser.add_argument("--ak", default=argparse.SUPPRESS)
    parser.add_argument("--sk", default=argparse.SUPPRESS)
    parser.add_argument("--token", default=argparse.SUPPRESS)
    parser.add_argument("--creds-file", default=argparse.SUPPRESS,
                        help="JSON：{accessKeyId,secretAccessKey,securityToken?}，也兼容 sts-creds.json")


def main():
    p = argparse.ArgumentParser(description="直连高校运营平台（least-hw）")
    add_creds_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("probe", help="连通性 + 凭证探测"); add_creds_args(ps)

    pi = sub.add_parser("inspect", help="读仓库：git 地址/分支/作品名/README/resources")
    pi.add_argument("--dir", default=".", help="作品仓库目录（默认为当前目录）")

    pc = sub.add_parser("camps", help="列训练营"); add_creds_args(pc)
    pc.add_argument("--year", type=int)

    pt = sub.add_parser("sts", help="解析账号 ID，并通过 SELF_VERIFY 委托换 STS 临时凭证")
    add_creds_args(pt)
    pt.add_argument("--create", action="store_true", help="缺失时创建 SELF_VERIFY 自委托（写你的 IAM）")
    pt.add_argument("--out", default="/tmp/sts-creds.json")
    pt.add_argument("--region", default="cn-north-4")

    pp = sub.add_parser("publish", help="发布作品"); add_creds_args(pp)
    pp.add_argument("--camp", required=True)
    pp.add_argument("--name", required=True)
    pp.add_argument("--intro", required=True, help="15~50 CJK 字符")
    pp.add_argument("--cover", required=True)
    pp.add_argument("--zip")
    pp.add_argument("--readme")
    pp.add_argument("--resources")
    pp.add_argument("--zip-out", default=str(Path("/tmp") / "gallery-detail.zip"))
    pp.add_argument("--git", required=True)
    pp.add_argument("--branch", default="main")
    pp.add_argument("--env-url", default="")
    pp.add_argument("--idem")
    pp.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    {"probe": cmd_probe, "inspect": cmd_inspect, "camps": cmd_camps,
     "sts": cmd_sts, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    main()
