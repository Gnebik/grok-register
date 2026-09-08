"""
CloakBrowser Turnstile Solver — Stealth Chromium 本地求解器
────────────────────────────────────────────────────────────
使用 CloakBrowser (https://github.com/CloakHQ/CloakBrowser) 的隐身 Chromium
自动通过 Cloudflare Turnstile 验证，无需付费 API。

前置条件:
  pip install cloakbrowser
  cloakbrowser install          # 下载隐身 Chromium 二进制
  cloakbrowser login            # 可选：绑定 license key（免费版可用）

用法:
  python cloakbrowser_solver.py                     # 启动 HTTP 服务
  python cloakbrowser_solver.py --once --url ... --key ...  # 单次求解
"""
import sys, os, time, json, asyncio, argparse
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PROXY = os.getenv("GROK_PROXY") or ""


async def solve_turnstile(site_url: str, site_key: str,
                          headless: bool = False,
                          max_attempts: int = 3,
                          page_timeout: int = 30,
                          captcha_timeout: int = 60,
                          trigger_selector: str = None) -> dict:
    """
    用 CloakBrowser 自动求解 Turnstile。

    参数:
      site_url: 目标页面 URL
      site_key: Turnstile site key
      trigger_selector: 触发 Turnstile 的 CSS 选择器（如点击按钮后出现）

    返回 {"token": "...", "elapsed": float} 或 {"error": "..."}
    """
    from cloakbrowser import launch_async

    t0 = time.time()
    last_error = None
    proxy_arg = PROXY if PROXY else None

    for attempt in range(1, max_attempts + 1):
        browser = None
        try:
            launch_kwargs = {"headless": headless}
            if proxy_arg:
                launch_kwargs["proxy"] = {"server": proxy_arg}

            browser = await launch_async(**launch_kwargs)
            page = await browser.new_page()

            print(f"  [CBSolver] 尝试 {attempt}/{max_attempts}: 加载 {site_url[:60]}...")
            await page.goto(site_url, timeout=page_timeout * 1000, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # 如果指定了触发器，点击触发 Turnstile
            if trigger_selector:
                try:
                    if trigger_selector == "__xai_email__":
                        # xAI 注册页：点击邮箱按钮触发 Next.js 路由
                        await page.evaluate('''() => {
                            const btns = document.querySelectorAll('button,[role=button]');
                            for (const b of btns) {
                                if (!b.offsetParent) continue;
                                const t = (b.innerText || '').trim();
                                if (t.includes('邮箱') || t.toLowerCase().includes('email')) {
                                    b.click(); return;
                                }
                            }
                        }''')
                        await asyncio.sleep(3)
                        # xAI 需要手动渲染 Turnstile（页面不自动加载 widget）
                        print(f"  [CBSolver] 手动渲染 xAI Turnstile...")
                        await page.evaluate(f'''() => {{
                            if (document.querySelector("[name=cf-turnstile-response]")) return;
                            const div = document.createElement("div");
                            div.style.cssText = "position:fixed;top:10px;right:10px;z-index:99999";
                            document.body.appendChild(div);
                            turnstile.render(div, {{
                                sitekey: "{site_key}", theme: "light",
                                callback: function(token) {{
                                    let h = document.querySelector("[name=cf-turnstile-response]");
                                    if (!h) {{ h = document.createElement("input"); h.type="hidden"; h.name="cf-turnstile-response"; document.body.appendChild(h); }}
                                    h.value = token;
                                }}
                            }});
                        }}''')
                    else:
                        btn = page.locator(trigger_selector).first
                        if await btn.count() > 0:
                            print(f"  [CBSolver] 点击触发器: {trigger_selector}")
                            await btn.click()
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"  [CBSolver] 触发器失败: {e}")

            # 等待 Turnstile 解决
            print(f"  [CBSolver] 等待 Turnstile (最多 {captcha_timeout}s)...")
            poll_interval = 0.5
            for _ in range(int(captcha_timeout / poll_interval)):
                token = await page.evaluate(
                    'document.querySelector("[name=cf-turnstile-response]")?.value || ""'
                )
                if len(token) > 50:
                    elapsed = time.time() - t0
                    print(f"  [CBSolver] OK ({elapsed:.1f}s)")
                    return {"token": token, "elapsed": elapsed}
                await asyncio.sleep(poll_interval)

            # 尝试点击 Turnstile iframe
            try:
                frame = page.frame_locator("iframe[src*='turnstile'], iframe[src*='challenges']").first
                body = frame.locator("body")
                if await body.count() > 0:
                    await body.click()
                    await asyncio.sleep(2)
                    for _ in range(int(captcha_timeout / poll_interval)):
                        token = await page.evaluate(
                            'document.querySelector("[name=cf-turnstile-response]")?.value || ""'
                        )
                        if len(token) > 50:
                            elapsed = time.time() - t0
                            print(f"  [CBSolver] OK ({elapsed:.1f}s)")
                            return {"token": token, "elapsed": elapsed}
                        await asyncio.sleep(poll_interval)
            except Exception:
                pass

            print(f"  [CBSolver] 尝试 {attempt} 超时")
            last_error = f"Timeout after {captcha_timeout}s"

        except Exception as e:
            last_error = str(e)[:200]
            print(f"  [CBSolver] 尝试 {attempt} 错误: {last_error}")

        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

        if attempt < max_attempts:
            await asyncio.sleep(3)

    return {"error": last_error or "All attempts exhausted"}


# ═══════════════════════ HTTP 服务（可选） ═══════════════════════

def run_server(port=8089, secret=None):
    if secret is None:
        secret = os.getenv("SOLVER_SECRET") or "grok-solver"
    """启动简单的 HTTP API 服务"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._json({"status": "ok"})
            elif self.path == "/solve":
                self._json({"error": "Use POST /solve with JSON body"})
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.path != "/solve":
                return self._json({"error": "Not found"}, 404)

            if self.headers.get("secret") != secret:
                return self._json({"error": "Forbidden"}, 403)

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
            except Exception:
                return self._json({"error": "Invalid JSON"}, 400)

            site_url = body.get("site_url")
            site_key = body.get("site_key")
            if not site_url or not site_key:
                return self._json({"error": "site_url and site_key required"}, 400)

            print(f"[Server] Solving: {site_url[:60]}...")
            result = asyncio.run(solve_turnstile(
                site_url=site_url,
                site_key=site_key,
                max_attempts=body.get("max_attempts", 3),
                page_timeout=body.get("page_timeout", 30),
                captcha_timeout=body.get("captcha_timeout", 60),
                trigger_selector=body.get("trigger_selector"),
            ))

            if "error" in result:
                return self._json({"status": "error", "message": result["error"]}, 500)
            else:
                return self._json({"status": "OK", "token": result["token"],
                                    "elapsed": str(result["elapsed"])})

        def _json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[Server] CloakBrowser Turnstile Solver on http://0.0.0.0:{port}")
    print(f"[Server] Proxy: {PROXY or '(none)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# ═══════════════════════ CLI ═══════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CloakBrowser 本地 Turnstile 求解器")
    parser.add_argument("--port", type=int, default=8089, help="HTTP 服务端口")
    parser.add_argument("--secret", default="grok-solver", help="API 密钥")
    parser.add_argument("--server", action="store_true", default=True, help="启动 HTTP 服务")
    parser.add_argument("--once", action="store_true", help="单次求解")
    parser.add_argument("--url", help="目标 URL（--once 模式）")
    parser.add_argument("--key", dest="site_key", help="Turnstile site key（--once 模式）")
    parser.add_argument("--headless", action="store_true", help="无头模式（可能影响成功率）")
    args = parser.parse_args()

    if args.once and args.url and args.site_key:
        result = asyncio.run(solve_turnstile(
            site_url=args.url,
            site_key=args.site_key,
            headless=args.headless,
        ))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.server:
        run_server(port=args.port, secret=args.secret)
    else:
        parser.print_help()
