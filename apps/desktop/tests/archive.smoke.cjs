// Real Chromium archive smoke: hidden window, temporary profile, offline bridge.
// Run after pnpm build: pnpm exec electron tests/archive.smoke.cjs
const { app, BrowserWindow, session } = require("electron");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-archive-"));
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
      preload: path.join(__dirname, "archive.preload.cjs"), contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false, offscreen: true,
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
    const layout = await evaluate(`(() => {
      const timeline = document.querySelector('.timeline');
      return { viewport: { width: innerWidth, height: innerHeight },
        timelineBounds: timeline?.getBoundingClientRect().toJSON(),
        timeline: timeline?.outerHTML.slice(0, 4000) };
    })()`);
    throw Error(`UI assertion timed out: ${source}\n${await evaluate("document.body.innerText")}\n${JSON.stringify(layout)}`);
  };
  const paint = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const capture = async (name) => {
    await paint();
    await fs.writeFile(path.join(output, name), (await window.webContents.capturePage()).toPNG());
  };
  const moveTo = async (selector) => {
    const point = await evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)});
      if (!element) throw Error('Missing mouse target');
      const bounds = element.getBoundingClientRect();
      return { x: Math.round(bounds.left + bounds.width / 2), y: Math.round(bounds.top + bounds.height / 2) };
    })()`);
    window.webContents.sendInputEvent({ type: "mouseMove", ...point });
    await paint();
    return point;
  };
  const click = async (selector) => {
    const point = await moveTo(selector);
    window.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...point });
    window.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...point });
  };
  const assertLayout = async () => {
    assert(await evaluate(`(() => {
      const banner = document.querySelector('.session-archive-banner');
      return document.documentElement.scrollWidth <= innerWidth
        && !document.querySelector('button button')
        && [...document.querySelectorAll('.session-row')].every(row => {
          const bounds = row.getBoundingClientRect();
          const action = row.querySelector('.session-archive-action').getBoundingClientRect();
          return bounds.width > 0 && bounds.left >= 0 && action.right <= bounds.right + 1;
        }) && (!banner || (banner.getBoundingClientRect().right <= innerWidth
          && banner.scrollWidth <= banner.clientWidth + 1));
    })()`), "Archive controls must fit without horizontal overflow or nested buttons");
  };
  const currentAction = '.session-row[aria-label="整理会话归档"] .session-archive-action';
  const retainedMessageVisible = "document.body.innerText.includes('整理会话归档的讨论记录已保留。')";

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor(`document.querySelector(${JSON.stringify(currentAction)}) !== null`);
  const initialTimelineBounds = await evaluate("document.querySelector('.timeline')?.getBoundingClientRect().toJSON()");
  // Initialize ResizeObserver with a real native resize in this hidden fixture.
  window.setSize(1281, 900);
  await paint();
  window.setSize(1280, 900);
  await paint();
  await waitFor(retainedMessageVisible);
  await moveTo(currentAction);
  assert.equal(await evaluate(`getComputedStyle(document.querySelector(${JSON.stringify(currentAction)})).opacity`), "1");
  await assertLayout();
  await capture("archive-active-wide.png");
  const original = await evaluate("window.archiveSmoke.state().records.map(record => record.messages)");

  await click(currentAction);
  await waitFor("document.querySelector('.session-archive-banner') && document.querySelector('textarea')?.disabled");
  await waitFor("!document.querySelector('.session-row[aria-label=\"整理会话归档\"]')");
  // The archive banner resizes Virtuoso's viewport; wait for its measured rows.
  await waitFor(retainedMessageVisible);
  assert.equal(await evaluate("window.archiveSmoke.state().calls.filter(call => call.method === 'session.archive').length"), 1);
  await capture("archive-readonly-wide.png");

  await click('.session-item');
  await waitFor("window.archiveSmoke.state().selectedId === 'archive-demo-other' && !document.querySelector('.session-archive-banner')");
  await click('[aria-label="查看已归档会话"]');
  await waitFor("document.querySelector('[aria-label=\"已归档会话\"]')?.innerText.includes('整理会话归档')");
  await evaluate("[...document.querySelectorAll('.session-item')].find(item => item.innerText.includes('整理会话归档')).click()");
  await waitFor("window.archiveSmoke.state().selectedId === 'archive-demo-current' && document.querySelector('.session-archive-banner') && document.querySelector('textarea')?.disabled");
  await waitFor(retainedMessageVisible);
  await assertLayout();
  await capture("archive-list-wide.png");

  window.setSize(820, 680);
  await paint();
  await assertLayout();
  await capture("archive-readonly-narrow.png");
  await click('.session-archive-banner button');
  await waitFor("!document.querySelector('.session-archive-banner') && !document.querySelector('textarea')?.disabled");
  await waitFor("!document.querySelector('.session-row[aria-label=\"整理会话归档\"]')");
  await assertLayout();
  await click('[aria-label="返回会话"]');
  await waitFor("document.querySelector('.session-row[aria-label=\"整理会话归档\"]') !== null");
  window.setSize(1280, 900);
  await paint();
  await assertLayout();
  await capture("archive-restored-wide.png");

  await click('[aria-label="查看已归档会话"]');
  await waitFor("document.querySelector('.session-row[aria-label=\"旧版界面方案\"]') !== null");
  await click('.session-row[aria-label="旧版界面方案"] .session-archive-action');
  await waitFor("document.querySelector('.empty-sidebar')?.innerText === '暂无已归档会话。'");
  assert.equal(await evaluate("window.archiveSmoke.state().selectedId"), "archive-demo-current");
  const finalState = await evaluate("window.archiveSmoke.state()");
  assert.deepEqual(finalState.records.map((record) => record.messages), original);
  assert.equal(finalState.calls.filter((call) => call.method === "session.archive").length, 1);
  assert.equal(finalState.calls.filter((call) => call.method === "session.unarchive").length, 2);
  assert.equal(finalState.calls.filter((call) => call.method === "run.start").length, 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output, initialTimelineBounds, screenshots: [
    "archive-active-wide.png", "archive-readonly-wide.png", "archive-list-wide.png",
    "archive-readonly-narrow.png", "archive-restored-wide.png",
  ].map((name) => path.join(output, name)) }));
  window.destroy();
  app.quit();
}

main().catch((error) => { console.error(error); app.exit(1); });
