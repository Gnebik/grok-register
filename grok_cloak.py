#!/usr/bin/env python
"""
Grok 注册 · 指纹浏览器版 (CloakBrowser + LuckMail)
─────────────────────────────────────────────────
与 grok_free.py 的差异（只换两处，其余复用）：
  · Turnstile: DrissionPage  →  CloakBrowser 隐身 Chromium
  · 邮箱:      Gmail 硬编码  →  LuckMail (outlook.com)

不复用 YesCaptcha（协议驱动 + 付费打码）。

依赖: Python 3.12 (需 cloakbrowser + DrissionPage + curl_cffi)
用法:
  python grok_cloak.py --count 1              # 注册 1 个
  python grok_cloak.py --count 1 --headless   # 无头模式
  python grok_cloak.py --browser-only         # 只测浏览器过 Turnstile，不注册
"""
import argparse
import os
import re
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from curl_cffi import requests as cf_req
import requests

# 复用 grok_free.py 的协议层（gRPC 编码 + 发码/验码 + 工具函数）
from grok_free import (
    SITE_URL, FALLBACK_SITE_KEY, UA,
    send_email_code_grpc, verify_email_code_grpc,
    rand_str, rand_name,
)
from email_service import EmailService

PROXY = os.getenv("GROK_PROXY") or "http://127.0.0.1:7897"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "keys")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def log(msg):
    print(msg, flush=True)


def _mail_call(fn, *args, **kwargs):
    """在独立线程里调用邮箱 SDK。

    cloakbrowser.launch() 会留下一个运行中的事件循环，导致 luckmail SDK 的
    _is_async_context() 误判为异步上下文，返回未 await 的协程。
    子线程没有事件循环 → 判定为同步 → 正常返回结果。
    """
    box = {}

    def run():
        try:
            box["ok"] = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box.get("ok")


# ═══════════════════════ CloakBrowser ═══════════════════════

def launch_browser(headless=False):
    """启动 CloakBrowser 隐身 Chromium。"""
    from cloakbrowser import launch
    kwargs = {"headless": headless}
    if PROXY:
        kwargs["proxy"] = PROXY
    browser = launch(**kwargs)
    log(f"[Browser] CloakBrowser 已启动 (headless={headless})")
    return browser


def get_action_id():
    """从 x.ai 前端 JS 里提取 Next.js Action ID。"""
    sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        sess.proxies = {"http": PROXY, "https": PROXY}
    html = sess.get(f"{SITE_URL}/sign-up?redirect=grok-com", timeout=20).text
    for js_path in re.findall(r"/_next/static/chunks/[^\"'\s>]+\.js", html):
        url = js_path if js_path.startswith("http") else f"{SITE_URL}{js_path}"
        try:
            js = sess.get(url, timeout=15).text
            m = re.search(r'release[:\s]*["\']([a-fA-F0-9]{40})["\']', js)
            if not m:
                m = re.search(r"7f[a-fA-F0-9]{40}", js)
            if m:
                return m.group(1) if m.lastindex else m.group(0)
        except Exception:
            continue
    return None


def solve_turnstile(page, site_key, timeout=70):
    """在 CloakBrowser 页面里渲染并等待 Turnstile token。"""
    page.evaluate("""(sitekey) => {
        if (document.querySelector("[name=cf-turnstile-response]")) return;
        const div = document.createElement("div");
        div.style.cssText = "position:fixed;top:10px;right:10px;z-index:99999";
        document.body.appendChild(div);
        turnstile.render(div, {
            sitekey: sitekey, theme: "light",
            callback: function(token) {
                let h = document.querySelector("[name=cf-turnstile-response]");
                if (!h) { h = document.createElement("input"); h.type = "hidden";
                          h.name = "cf-turnstile-response"; document.body.appendChild(h); }
                h.value = token;
            }
        });
    }""", site_key)

    deadline = time.time() + timeout
    while time.time() < deadline:
        token = page.evaluate('document.querySelector("[name=cf-turnstile-response]")?.value || ""')
        if token and len(token) > 50:
            return token
        time.sleep(0.5)

    # 兜底：点击 Turnstile iframe
    try:
        frame = page.frame_locator("iframe[src*='turnstile'], iframe[src*='challenges']").first
        if frame.locator("body").count() > 0:
            frame.locator("body").click()
            deadline = time.time() + 30
            while time.time() < deadline:
                token = page.evaluate('document.querySelector("[name=cf-turnstile-response]")?.value || ""')
                if token and len(token) > 50:
                    return token
                time.sleep(0.5)
    except Exception:
        pass
    return ""


def browser_prepare(browser, need_token=True):
    """打开注册页 → 取 site_key / action_id / state_tree (+ 首次 Turnstile token)。"""
    page = browser.new_page()
    log("[Browser] 打开注册页...")
    page.goto(f"{SITE_URL}/sign-up?redirect=grok-com", timeout=60000, wait_until="domcontentloaded")
    time.sleep(4)

    html = page.content()
    if not html:
        raise RuntimeError("页面加载失败")

    site_key = FALLBACK_SITE_KEY
    m = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
    if m:
        site_key = m.group(1)
    log(f"[Browser] SiteKey: {site_key}")

    state_tree = ""
    m = re.search(r'next-router-state-tree":"([^"]+)"', html)
    if m:
        state_tree = m.group(1)

    action_id = get_action_id()
    if not action_id:
        raise RuntimeError("未找到 Action ID")
    log(f"[Browser] ActionID: {action_id}")

    # 点击邮箱注册选项
    log("[Browser] 点击邮箱注册选项...")
    page.evaluate("""() => {
        const all = document.querySelectorAll('button,[role=button]');
        for (const b of all) {
            if (!b.offsetParent) continue;
            const t = (b.innerText || '').trim();
            if (t.includes('邮箱') || t.toLowerCase().includes('email')) { b.click(); return; }
        }
    }""")
    time.sleep(3)

    ts_token = ""
    if need_token:
        log("[Browser] 求解 Turnstile...")
        ts_token = solve_turnstile(page, site_key)
        if ts_token:
            log(f"[Browser] Turnstile OK ({len(ts_token)} chars)")
        else:
            log("[Browser] Turnstile 失败")

    return {"page": page, "site_key": site_key, "action_id": action_id,
            "state_tree": state_tree, "ts_token": ts_token}


# ═══════════════════════ 注册 ═══════════════════════

def register_one(cfg, provider):
    """注册单个账号 → (email, password, sso) 或 None"""
    page = cfg["page"]

    # ── 邮箱 ──
    log(f"[Mail] 创建 {provider} 邮箱...")
    try:
        svc = EmailService(proxies={"http": PROXY, "https": PROXY} if PROXY else None,
                           provider=provider)
        token_like, email = _mail_call(svc.create_email)
        if not email:
            log("[Mail] 邮箱创建失败")
            return None
    except Exception as e:
        log(f"[Mail] 异常: {e}")
        return None
    log(f"[Mail] {email}")

    # ── curl_cffi session (协议层) ──
    sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        sess.proxies = {"http": PROXY, "https": PROXY}
    try:
        sess.get(SITE_URL, timeout=10)
    except Exception:
        pass

    # ── 发码 ──
    log(f"[{email}] 发送验证码 (gRPC)...")
    if not send_email_code_grpc(sess, email):
        log(f"[{email}] 发送验证码失败")
        return None
    log(f"[{email}] 验证码已发送")

    # ── 收码 ──
    log(f"[{email}] 等待验证码...")
    code = None
    for i in range(36):  # 180s
        time.sleep(5)
        try:
            content = _mail_call(svc.fetch_first_email, token_like)
        except Exception as e:
            log(f"[{email}] 收信异常: {e}")
            content = None
        if content:
            m = re.search(r"([A-Z0-9]{3})-?([A-Z0-9]{3})", content)
            if m:
                code = m.group(1) + m.group(2)
                break
        if i and i % 6 == 0:
            log(f"[{email}] 等待中... ({i*5}s)")
    if not code:
        log(f"[{email}] 未收到验证码（超时）")
        return None
    log(f"[{email}] 验证码: {code}")

    # ── 验码 ──
    log(f"[{email}] 验证验证码 (gRPC)...")
    if not verify_email_code_grpc(sess, email, code):
        log(f"[{email}] 验证码无效")
        return None
    log(f"[{email}] 验证码正确")

    # ── 刷新 Turnstile（每次注册用新 token）──
    log(f"[{email}] 刷新 Turnstile...")
    ts_token = solve_turnstile(page, cfg["site_key"])
    if not ts_token:
        log(f"[{email}] Turnstile 刷新失败")
        return None
    log(f"[{email}] Turnstile 已刷新（{len(ts_token)} chars）")

    # ── 注册 POST ──
    password = rand_str(14) + "Aa1!"
    payload = [{
        "emailValidationCode": code,
        "createUserAndSessionRequest": {
            "email": email,
            "givenName": rand_name(),
            "familyName": rand_name(),
            "clearTextPassword": password,
            "tosAcceptedVersion": "$undefined",
        },
        "turnstileToken": ts_token,
        "promptOnDuplicateEmail": True,
    }]
    try:
        sess.get(SITE_URL, timeout=10)
    except Exception:
        pass
    headers = {
        "user-agent": UA,
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": SITE_URL,
        "referer": f"{SITE_URL}/sign-up",
        "cookie": f"__cf_bm={sess.cookies.get('__cf_bm','')}",
        "next-router-state-tree": cfg["state_tree"],
        "next-action": cfg["action_id"],
    }
    log(f"[{email}] 提交注册...")
    try:
        r = sess.post(f"{SITE_URL}/sign-up", json=payload, headers=headers, timeout=30)
        log(f"[{email}] POST 状态: {r.status_code}")
    except Exception as e:
        log(f"[{email}] POST 异常: {e}")
        return None
    if r.status_code != 200:
        log(f"[{email}] 注册失败: {r.text[:300]}")
        return None

    # ── 提取 SSO ──
    sso_url = None
    for pat in [r'(https://[^"\s]+set-cookie\?q=[^:"\s]+)1:',
                r'(https://[^"\s]+set-cookie\?q=[^"\s]+)',
                r'https://[^"\s]*set-cookie[^"\s]*']:
        m = re.search(pat, r.text)
        if m:
            sso_url = m.group(0).rstrip("1:")
            break
    if not sso_url:
        log(f"[{email}] 响应中无 SSO URL，前300字符:")
        log(f"  {r.text[:300]}")
        return None

    sso_url = re.sub(r"[:\d]*$", "", sso_url) if sso_url.endswith(("1:", "2:", "3:")) else sso_url
    log(f"[{email}] SSO URL: {sso_url[:100]}...")
    sso = None
    try:
        rs = requests.Session()
        if PROXY:
            rs.proxies = {"http": PROXY, "https": PROXY}
        rs.get(sso_url, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
        sso = rs.cookies.get("sso")
    except Exception as e:
        log(f"[{email}] SSO 请求异常: {e}")
        try:
            sess.get(sso_url, allow_redirects=True, timeout=15)
            sso = sess.cookies.get("sso")
        except Exception as e2:
            log(f"[{email}] SSO 回退也失败: {e2}")

    if sso:
        log(f"[{email}] ✅ SSO: {sso[:30]}...")
        with open(os.path.join(OUTPUT_DIR, "grok.txt"), "a", encoding="utf-8") as f:
            f.write(sso + "\n")
        with open(os.path.join(OUTPUT_DIR, "accounts.txt"), "a", encoding="utf-8") as f:
            f.write(f"{email}:{password}:{sso}\n")
        return (email, password, sso)
    log(f"[{email}] 无 SSO cookie")
    return None


# ═══════════════════════ 主程序 ═══════════════════════

def main():
    ap = argparse.ArgumentParser(description="Grok 注册 · 指纹浏览器版")
    ap.add_argument("--count", type=int, default=1, help="注册数量")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--provider", default="luckmail", help="邮箱源 (默认 luckmail)")
    ap.add_argument("--min-delay", type=int, default=10, help="注册间隔最小值(秒)")
    ap.add_argument("--max-delay", type=int, default=25, help="注册间隔最大值(秒)")
    ap.add_argument("--browser-only", action="store_true", help="只测浏览器过 Turnstile，不注册")
    args = ap.parse_args()

    log("=" * 55)
    log(f"Grok 注册 · 指纹浏览器版 (CloakBrowser + {args.provider})")
    log(f"数量: {args.count}  代理: {PROXY}  headless={args.headless}")
    log("=" * 55)

    browser = None
    try:
        browser = launch_browser(headless=args.headless)
        cfg = browser_prepare(browser, need_token=True)

        if args.browser_only:
            log("")
            log("=" * 55)
            log(f"[browser-only] Turnstile: {'✅ 成功 ' + str(len(cfg['ts_token'])) + ' chars' if cfg['ts_token'] else '❌ 失败'}")
            log("=" * 55)
            return

        success = fail = 0
        t0 = time.time()
        for i in range(1, args.count + 1):
            log("")
            log("─" * 40)
            log(f"第 {i}/{args.count} 次注册")
            log("─" * 40)
            r = register_one(cfg, args.provider)
            if r:
                success += 1
            else:
                fail += 1
            if i < args.count:
                d = 0
                time.sleep(d)

        log("")
        log("=" * 55)
        log(f"结束。成功={success} 失败={fail} 耗时={time.time()-t0:.0f}s")
        if success:
            log(f"SSO 已保存至: {OUTPUT_DIR}")
        log("=" * 55)
    finally:
        if browser:
            try:
                browser.close()
                log("[Browser] 已关闭")
            except Exception:
                pass


if __name__ == "__main__":
    main()
