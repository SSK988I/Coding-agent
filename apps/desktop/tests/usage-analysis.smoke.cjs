// Real Chromium + Ant Design + ECharts; offline, hidden window, throwaway profile.
// Run after the desktop build: electron tests/usage-analysis.smoke.cjs
const { app, BrowserWindow, ipcMain, session } = require("electron");
const fs = require("node:fs/promises");
const { createServer } = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { fileURLToPath } = require("node:url");
const assert = require("node:assert/strict");
const { NativeWorkbench } = require("../dist-electron/main/workbench.js");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-usage-analysis-"));
  app.setPath("userData", path.join(output, "profile"));
  await app.whenReady();
  const assets = await fs.readdir(path.join(__dirname, "../dist-renderer/assets"));
  // Fall back to the old combined chunk to reproduce its empty first-open
  // panel before the drawer/chart loading boundary is separated.
  const drawerBundle = assets.find((name) => /^UsageAnalysisDrawer-[^/]+\.js$/.test(name));
  const deferredBundle = assets.find((name) => /^UsageCharts-[^/]+\.js$/.test(name))
    ?? assets.find((name) => /^UsageAnalysisDrawer-[^/]+\.js$/.test(name));
  assert(deferredBundle, "Production usage module must exist");
  let holdDrawer = drawerBundle !== deferredBundle;
  let holdCharts = true;
  let drawerLoadCount = 0;
  let chartLoadCount = 0;
  const pendingDrawerLoads = [];
  const pendingChartLoads = [];
  assert(session.defaultSession.protocol.interceptFileProtocol("file", (request, callback) => {
    const file = fileURLToPath(request.url);
    if (path.basename(file) === drawerBundle && drawerBundle !== deferredBundle) {
      drawerLoadCount++;
      if (holdDrawer) { pendingDrawerLoads.push(() => callback({ path: file })); return; }
    }
    if (path.basename(file) === deferredBundle) {
      chartLoadCount++;
      if (holdCharts) { pendingChartLoads.push(() => callback({ path: file })); return; }
    }
    callback({ path: file });
  }), "Fixture must be able to delay the production chart chunk");
  const server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end("<!doctype html><title>Usage overlay local page</title><h1 id='native-usage-page'>Local native browser</h1>");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browserUrl = `http://127.0.0.1:${server.address().port}/`;
  const blockedRequests = [];
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
    (details, callback) => { blockedRequests.push(details.url); callback({ cancel: true }); },
  );
  session.fromPartition("persist:coding-agent-workbench-browser").webRequest.onBeforeRequest(
    { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
    (details, callback) => {
      const allowed = details.url.startsWith(browserUrl);
      if (!allowed) blockedRequests.push(details.url);
      callback({ cancel: !allowed });
    },
  );
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const window = new BrowserWindow({ show: false, width: 1280, height: 900,
    webPreferences: { preload: path.join(__dirname, "usage-analysis.preload.cjs"), contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false, offscreen: true },
  });
  const errors = [];
  // Chromium omits some file:// module loads from Resource Timing. Observe
  // scripts parsed by the engine instead of treating an empty timing list as
  // proof that the lazy module was never loaded.
  const drawerScripts = new Set();
  const chartScripts = new Set();
  window.webContents.on("console-message", (_event, details) => {
    if (typeof details === "object" && details.level === "error") errors.push(details.message);
  });
  window.webContents.on("render-process-gone", (_event, details) => errors.push(`Renderer exited: ${details.reason}`));
  const evaluate = (source) => window.webContents.executeJavaScript(source);
  const nativeCalls = [];
  const native = new NativeWorkbench(window, async (method) => {
    assert.equal(method, "workbench.scope");
    return evaluate("window.usageAnalysisSmoke.scope()");
  });
  ipcMain.handle("usage-analysis:native", (event, method, params) => {
    assert.equal(event.sender, window.webContents);
    nativeCalls.push({ method, params });
    return native.dispatch(method, params);
  });
  const waitFor = async (source) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await evaluate(source)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw Error(`UI assertion timed out: ${source}\n${await evaluate("document.body.innerText")}\n${JSON.stringify(await evaluate("window.usageAnalysisSmoke.state()"))}`);
  };
  const waitForNative = async (condition, description) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await condition()) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw Error(`Native assertion timed out: ${description}\n${JSON.stringify(nativeCalls)}\n${await evaluate(`JSON.stringify({
      dialogs: [...document.querySelectorAll('[role=dialog]')].map(node => ({ text: node.textContent.slice(0, 200),
        display: getComputedStyle(node).display, visibility: getComputedStyle(node).visibility })),
      drawers: [...document.querySelectorAll('.ant-drawer')].map(node => node.className),
      loading: !!document.querySelector('.usage-loader-mask'),
      focus: document.activeElement?.getAttribute('aria-label'),
    })`)}`);
  };
  const paint = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const click = async (selector) => {
    await waitFor(`(() => { const element = document.querySelector(${JSON.stringify(selector)});
      return !!element && !element.disabled && !element.classList.contains('ant-btn-loading'); })()`);
    await evaluate("Promise.all([...document.querySelectorAll('.ant-drawer-content-wrapper')].flatMap(node => node.getAnimations()).map(animation => animation.finished.catch(() => undefined)))");
    const point = await evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) throw Error('Missing click target: ' + ${JSON.stringify(selector)});
      element.scrollIntoView({ block: 'nearest' });
      const bounds = element.getBoundingClientRect();
      return { x: Math.round(bounds.left + bounds.width / 2), y: Math.round(bounds.top + bounds.height / 2) };
    })()`);
    window.webContents.sendInputEvent({ type: "mouseMove", ...point });
    window.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...point });
    window.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...point });
  };
  const clickText = async (selector, text) => {
    await waitFor(`[...document.querySelectorAll(${JSON.stringify(selector)})]
      .some(item => item.textContent.trim() === ${JSON.stringify(text)})`);
    await evaluate(`(() => {
      const element = [...document.querySelectorAll(${JSON.stringify(selector)})]
        .find(item => item.textContent.trim() === ${JSON.stringify(text)});
      if (!element) throw Error('Missing text target: ' + ${JSON.stringify(text)});
      element.click();
    })()`);
    if (selector === "[role=tab]") await waitFor(`[...document.querySelectorAll('[role=tab]')]
      .find(item => item.textContent.trim() === ${JSON.stringify(text)})?.getAttribute('aria-selected') === 'true'`);
  };
  const capture = async (name) => {
    await evaluate("Promise.all([...document.querySelectorAll('.ant-drawer')].flatMap(node => node.getAnimations({ subtree: true })).filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => undefined)))");
    await paint();
    await fs.writeFile(path.join(output, name), (await window.webContents.capturePage()).toPNG());
  };
  const visible = "(() => { const root = document.querySelector('[data-testid=usage-analysis-drawer]'); return !!root && root.getBoundingClientRect().width > 0 && getComputedStyle(root).visibility !== 'hidden'; })()";
  const charts = "[...document.querySelectorAll('[data-testid=usage-analysis-chart]')].filter(node => node.getBoundingClientRect().width > 0)";
  const waitForChart = () => waitFor(`${charts}.some(node => node.querySelector('svg')?.querySelectorAll('path').length > 3)`);
  const assertLayout = async () => {
    const fits = `(() => {
      const dialog = document.querySelector('.ant-drawer-content-wrapper');
      if (!dialog) return false;
      const rect = dialog.getBoundingClientRect();
      return document.documentElement.scrollWidth <= innerWidth && rect.left >= -1 && rect.right <= innerWidth + 1
        && [...document.querySelectorAll('[data-testid=usage-analysis-chart]')].every(chart => {
          const bounds = chart.getBoundingClientRect();
          const svg = chart.querySelector('svg');
          return !bounds.width || (bounds.left >= rect.left - 1 && bounds.right <= rect.right + 1
            && (!svg || svg.getBoundingClientRect().width <= bounds.width + 1));
        });
    })()`;
    await waitFor(fits);
    assert(await evaluate(fits), "Drawer and chart SVGs must fit the window without horizontal page overflow");
  };
  const open = async () => {
    await click('[aria-label="查看用量分析"]');
    await waitFor(visible);
    await waitFor("document.querySelector('[role=dialog]')?.textContent.includes('会话用量分析')");
  };
  const close = async () => {
    await click('[aria-label="关闭用量分析"]');
    await waitFor(`!(${visible})`);
  };

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  // Navigate first so the hidden window has a live renderer for CDP commands.
  window.webContents.debugger.attach("1.3");
  window.webContents.debugger.on("message", (_event, method, params) => {
    if (method === "Debugger.scriptParsed" && /UsageAnalysisDrawer-[^/]+\.js/.test(params.url ?? "")) {
      drawerScripts.add(params.url);
    }
    if (method === "Debugger.scriptParsed" && /UsageCharts-[^/]+\.js/.test(params.url ?? "")) {
      chartScripts.add(params.url);
    }
  });
  await window.webContents.debugger.sendCommand("Debugger.enable");
  await waitFor("document.querySelector('[aria-label=\"查看用量分析\"]') !== null");
  assert.equal(await evaluate("window.usageAnalysisSmoke.state().calls.filter(call => call.method === 'session.usage').length"), 0);
  assert.equal(await evaluate("document.querySelector('[data-testid=usage-analysis-drawer]')"), null);
  assert.equal(drawerScripts.size, 0);
  assert.equal(chartLoadCount, 0);

  await click('[aria-label="打开工具栏"]');
  await waitFor("document.querySelector('.workbench-launcher button')");
  await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(button => button.textContent.includes('浏览器')).click()");
  await waitFor("document.querySelector('[aria-label=刷新网页]') && !document.querySelector('[aria-label=刷新网页]').disabled");
  await evaluate(`(() => {
    const input = document.querySelector('.browser-address input');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(browserUrl)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await waitFor("!document.querySelector('.browser-address button[type=submit]').disabled");
  await evaluate("document.querySelector('.browser-address').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))");
  await waitForNative(() => native.browser?.webContents.getURL() === browserUrl, "local browser navigation");
  const browser = native.browser;
  const browserKey = native.browserKey;
  const page = browser.webContents;
  await waitForNative(() => page.executeJavaScript("!!document.querySelector('#native-usage-page')"), "local native page load");
  await page.executeJavaScript("window.usageOverlayRetained = 'same-page'");
  await waitForNative(() => browser.getVisible() && browser.getBounds().width > 0 && browser.getBounds().height > 0, "native browser initially visible");
  assert.equal(nativeCalls.filter((call) => call.method === "browser.open").length, 1);
  await click('[aria-label="查看用量分析"]');
  if (holdDrawer) {
    await waitForNative(() => pendingDrawerLoads.length > 0, "drawer module held before Ant Design loads");
    await waitFor("document.querySelectorAll('.usage-analysis-kpi').length === 4");
    assert(await evaluate("!!document.querySelector('[aria-label=\"当前上下文容量\"]')"));
    assert(await evaluate("!!document.querySelector('.usage-analysis-skeleton')"));
    assert.equal(drawerScripts.size, 0);
    assert.equal(chartLoadCount, 0);
    await capture("usage-analysis-immediate-loading.png");
    await click('[aria-label="关闭用量分析"]');
    await waitFor("!document.querySelector('.usage-loader-mask')");
    await waitForNative(() => browser.getVisible(), "cancel during module loading restores native browser");
    await waitFor("document.activeElement?.getAttribute('aria-label') === '查看用量分析'");
    await click('[aria-label="查看用量分析"]');
    assert.equal(drawerLoadCount, 1, "Reopening must reuse the pending drawer import");
    holdDrawer = false;
    for (const release of pendingDrawerLoads.splice(0)) release();
  }
  await waitForNative(() => pendingChartLoads.length > 0, "chart chunk request held before it can render");
  await paint();
  await capture("usage-analysis-deferred-charts.png");
  const pendingPanel = await evaluate(`({
    kpis: document.querySelectorAll('.usage-analysis-kpi').length,
    context: !!document.querySelector('[aria-label="当前上下文容量"]'),
    charts: document.querySelectorAll('[data-testid=usage-analysis-chart] svg').length,
  })`);
  assert.equal(pendingPanel.kpis, 4, "First open must show the existing usage metrics while chart code is still loading");
  assert.equal(pendingPanel.context, true, "Current context must remain visible while charts load");
  assert.equal(pendingPanel.charts, 0);
  await waitFor(visible);
  await waitFor("document.querySelector('[role=tab]')");
  await clickText("[role=tab]", "请求明细");
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer] table')?.textContent.includes('fixture-flash')");
  await close();
  await waitForNative(() => browser.getVisible(), "native browser restored even when chart code has not loaded");
  assert.equal(pendingChartLoads.length, 1);
  assert.equal(chartScripts.size, 0);
  await open();
  await clickText("[role=tab]", "用量趋势");
  assert.equal(chartLoadCount, 1, "Reopening must reuse the pending chart import");
  holdCharts = false;
  for (const release of pendingChartLoads.splice(0)) release();
  await waitForNative(() => drawerScripts.size > 0, "lazy drawer module parsed after first open");
  await waitForNative(() => chartScripts.size === 1, "separate chart module parsed only after release");
  await waitForNative(() => !browser.getVisible() && browser.getBounds().width === 0 && browser.getBounds().height === 0, "native browser hidden behind drawer");
  assert.equal(native.browser, browser);
  assert.equal(page.isDestroyed(), false);
  await waitForChart();
  await assertLayout();
  assert.equal(await evaluate("document.querySelectorAll('[data-testid=usage-analysis-chart] svg').length"),
    await evaluate("document.querySelectorAll('[data-testid=usage-analysis-chart]').length"));
  await capture("usage-analysis-overview-wide.png");
  await evaluate("document.querySelector('.usage-analysis-cache-panel').scrollIntoView({ block: 'nearest' })");
  await capture("usage-analysis-cache-wide.png");
  await evaluate("document.querySelector('.usage-analysis-content').scrollIntoView({ block: 'start' })");

  await clickText(".ant-segmented-item", "估算费用");
  await waitFor("document.querySelector('.ant-segmented-item-selected')?.textContent.trim() === '估算费用'");
  await waitForChart();
  await capture("usage-analysis-cost-wide.png");
  await clickText("[role=tab]", "请求明细");
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer] table')?.textContent.includes('fixture-unknown')");
  assert(await evaluate("document.querySelector('[data-testid=usage-analysis-drawer] table').textContent.includes('fixture-flash')"));
  assert.equal(await evaluate("/NaN|undefined|Infinity/.test(document.querySelector('[data-testid=usage-analysis-drawer]').textContent)"), false);
  await capture("usage-analysis-details-wide.png");

  window.setSize(820, 680);
  await paint();
  await assertLayout();
  await capture("usage-analysis-details-narrow.png");
  await clickText("[role=tab]", "用量趋势");
  await waitForChart();
  await assertLayout();
  await capture("usage-analysis-overview-narrow.png");

  const previousCalls = await evaluate("window.usageAnalysisSmoke.state().calls.filter(call => call.method === 'session.usage').length");
  await evaluate("window.usageAnalysisSmoke.addRequest()");
  await waitFor(`window.usageAnalysisSmoke.state().calls.filter(call => call.method === 'session.usage').length > ${previousCalls}`);
  await clickText("[role=tab]", "请求明细");
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer] table')?.textContent.includes('fixture-live')");
  await close();
  await waitFor("document.activeElement?.getAttribute('aria-label') === '查看用量分析'");
  await waitForNative(() => browser.getVisible() && browser.getBounds().width > 0 && browser.getBounds().height > 0, "same native browser restored after drawer close");
  assert.equal(native.browser, browser);
  assert.equal(native.browserKey, browserKey);
  assert.equal(browser.webContents, page);
  assert.equal(await page.executeJavaScript("window.usageOverlayRetained"), "same-page");
  assert.equal(page.getURL(), browserUrl);
  assert.equal(await evaluate("document.querySelector('.browser-address input').value"), browserUrl);
  assert.equal(nativeCalls.filter((call) => call.method === "browser.open").length, 1);
  assert.equal(nativeCalls.filter((call) => call.method === "browser.close").length, 0);
  await click('[aria-label="关闭工具栏"]');
  await waitForNative(() => !browser.getVisible(), "native browser hidden when workbench closes");
  await open();
  await clickText("[role=tab]", "用量趋势");
  await waitForChart();
  assert.equal(chartLoadCount, 1, "Chart code must stay cached across subsequent drawer opens");

  await evaluate("window.usageAnalysisSmoke.delayNextUsage()");
  await click('[aria-label="刷新用量分析"]');
  await waitFor("window.usageAnalysisSmoke.state().pendingUsage === 1");
  await evaluate("window.usageAnalysisSmoke.switchSession('analysis-other')");
  await waitFor(`!(${visible})`);
  await open();
  await clickText("[role=tab]", "请求明细");
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer] table')?.textContent.includes('fixture-alt')");
  await evaluate("window.usageAnalysisSmoke.resolvePending()");
  await paint();
  assert.equal(await evaluate("document.querySelector('[data-testid=usage-analysis-drawer]').textContent.includes('fixture-flash')"), false);
  assert.equal(await evaluate("document.querySelector('[data-testid=usage-analysis-drawer]').textContent.includes('fixture-live')"), false);
  await assertLayout();

  await evaluate("window.usageAnalysisSmoke.switchSession('analysis-current')");
  await waitFor(`!(${visible})`);
  await open();
  await clickText("[role=tab]", "请求明细");
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer] table')?.textContent.includes('fixture-live')");
  await close();
  await click('[aria-label="新建会话"]');
  await waitFor("window.usageAnalysisSmoke.state().selectedId === 'analysis-empty'");
  await open();
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer]')?.textContent.match(/暂无|还没有|尚无|没有/)");
  assert.equal(await evaluate("document.querySelectorAll('[data-testid=usage-analysis-chart] svg').length"), 0);
  await assertLayout();
  await capture("usage-analysis-empty.png");

  await close();
  await evaluate("window.usageAnalysisSmoke.failNextUsage()");
  await open();
  await waitFor("document.querySelector('[data-testid=usage-analysis-drawer]')?.textContent.includes('离线夹具模拟读取失败')");
  await clickText("[data-testid=usage-analysis-drawer] button", "重试");
  await waitFor("!document.querySelector('[data-testid=usage-analysis-drawer]')?.textContent.includes('离线夹具模拟读取失败')");
  await close();
  const finalState = await evaluate("window.usageAnalysisSmoke.state()");
  assert.equal(finalState.calls.filter((call) => call.method === "run.start").length, 0);
  assert.deepEqual(blockedRequests, []);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output, lazyDrawerBundle: { beforeOpen: false, afterOpen: true },
    deferredModules: { immediateSummaryAndSkeleton: true, cancelDuringLoad: true, drawerRequests: drawerLoadCount,
      summaryBeforeChartLoad: true, tableAndCloseUsable: true, chartRequests: chartLoadCount },
    nativeOverlay: { hiddenDuringDrawer: true, restoredSameView: true, browserOpenCount: 1 },
    analysisCalls: finalState.calls.filter((call) => call.method === "session.usage").length,
    screenshots: ["usage-analysis-immediate-loading.png", "usage-analysis-deferred-charts.png", "usage-analysis-overview-wide.png", "usage-analysis-cache-wide.png", "usage-analysis-cost-wide.png", "usage-analysis-details-wide.png",
      "usage-analysis-details-narrow.png", "usage-analysis-overview-narrow.png", "usage-analysis-empty.png"].map((name) => path.join(output, name)) }));
  await native.dispose();
  ipcMain.removeHandler("usage-analysis:native");
  await new Promise((resolve) => server.close(resolve));
  window.webContents.debugger.detach();
  session.defaultSession.protocol.uninterceptProtocol("file");
  window.destroy();
  app.quit();
}

const watchdog = setTimeout(() => { console.error("Usage analysis smoke timed out"); app.exit(1); }, 45_000);
main().then(() => clearTimeout(watchdog)).catch((error) => { console.error(error); app.exit(1); });
