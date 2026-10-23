// Real Chromium metrics smoke: hidden window, temporary profile, offline bridge.
// Run after pnpm build: pnpm exec electron tests/usage.smoke.cjs
const { app, BrowserWindow, session } = require("electron");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-usage-"));
  app.setPath("userData", path.join(output, "profile"));
  await app.whenReady();
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
    (_details, callback) => callback({ cancel: true }),
  );
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const window = new BrowserWindow({
    show: false, width: 1280, height: 900,
    webPreferences: {
      preload: path.join(__dirname, "usage.preload.cjs"),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false, offscreen: true,
    },
  });
  const errors = [];
  window.webContents.on("console-message", (_event, details) => {
    if (typeof details === "object" && details.level === "error") errors.push(details.message);
  });
  const evaluate = (source) => window.webContents.executeJavaScript(source);
  const waitFor = async (source) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await evaluate(source)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw Error(`UI assertion timed out: ${source}\n${await evaluate("document.body.innerText")}`);
  };
  const paint = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const capture = async (name) => {
    await paint();
    await fs.writeFile(path.join(output, name), (await window.webContents.capturePage()).toPNG());
  };
  const assertLayout = async () => {
    assert(await evaluate(`(() => {
      const strip = document.querySelector('.usage-metrics');
      const rect = strip.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.left >= 0 && rect.right <= innerWidth
        && rect.top >= 0 && rect.bottom <= innerHeight
        && document.documentElement.scrollWidth <= innerWidth
        && strip.scrollWidth <= strip.clientWidth + 1
        && [...strip.children].every(child => {
          const bounds = child.getBoundingClientRect();
          return bounds.left >= rect.left && bounds.right <= rect.right + 1;
        });
    })()`), "Usage strip must be visible and contained without horizontal overflow");
  };

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor("document.querySelector('[data-testid=cache-hit-rate]')?.textContent === '90%'");
  assert.equal(await evaluate("document.querySelector('[data-testid=context-usage]').textContent"), "82K / 100K · 82%");
  assert(await evaluate("document.querySelector('.usage-context').classList.contains('warning')"));
  assert.equal(await evaluate("document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')"), "82");
  await assertLayout();
  await capture("usage-wide.png");

  window.setSize(820, 680);
  await paint();
  await assertLayout();
  await capture("usage-narrow.png");

  await evaluate("window.usageSmoke.update()");
  await waitFor("document.querySelector('[data-testid=cache-hit-rate]')?.textContent === '94%'");
  assert(await evaluate("document.querySelector('.usage-context').classList.contains('critical')"));
  assert.equal(await evaluate("document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')"), "91");
  await assertLayout();
  await capture("usage-updated.png");

  await evaluate("window.usageSmoke.switchSession()");
  await waitFor("document.querySelector('[data-testid=cache-hit-rate]')?.textContent === '25%'");
  await evaluate("window.usageSmoke.staleUpdate()");
  await paint();
  assert.equal(await evaluate("document.querySelector('[data-testid=cache-hit-rate]').textContent"), "25%");
  await evaluate("window.usageSmoke.overflow()");
  await waitFor("document.querySelector('[data-testid=context-usage]')?.textContent.includes('125%')");
  assert.equal(await evaluate("document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')"), "100");
  await assertLayout();
  await capture("usage-overflow.png");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output, screenshots: [
    "usage-wide.png", "usage-narrow.png", "usage-updated.png", "usage-overflow.png",
  ].map((name) => path.join(output, name)) }));
  window.destroy();
  app.quit();
}

main().catch((error) => { console.error(error); app.exit(1); });
