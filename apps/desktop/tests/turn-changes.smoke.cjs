// Real Chromium verifies turn cards and exact immutable review selection offline.
const { app, BrowserWindow, session } = require("electron");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-turn-changes-"));
  app.setPath("userData", path.join(output, "profile"));
  await app.whenReady();
  const blockedRequests = [];
  session.defaultSession.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
    (details, callback) => { blockedRequests.push(details.url); callback({ cancel: true }); });
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const window = new BrowserWindow({ show: false, width: 1440, height: 1040,
    webPreferences: { preload: path.join(__dirname, "turn-changes.preload.cjs"), contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false, offscreen: true } });
  const errors = [];
  window.webContents.on("console-message", (_event, details) => {
    if (typeof details === "object" && details.level === "error") errors.push(details.message);
  });
  window.webContents.on("render-process-gone", (_event, details) => errors.push(`Renderer exited: ${details.reason}`));
  const evaluate = (source) => window.webContents.executeJavaScript(source);
  const paint = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const waitFor = async (source) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await evaluate(source)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw Error(`UI assertion timed out: ${source}\n${await evaluate("document.body.innerText")}\n${JSON.stringify(await evaluate("window.turnChangesSmoke.state()"))}`);
  };
  const firstCard = '.turn-changes-card[aria-label="本轮变更 4 个文件"]';
  const secondCard = '.turn-changes-card[aria-label="本轮变更 1 个文件"]';
  const click = async (selector) => {
    await waitFor(`!!document.querySelector(${JSON.stringify(selector)})`);
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ block: 'center' })`);
    await paint();
    const point = await evaluate(`(() => {
      const button = document.querySelector(${JSON.stringify(selector)});
      if (!button || button.disabled) throw Error('Missing or disabled click target');
      const rect = button.getBoundingClientRect();
      return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
    })()`);
    window.webContents.sendInputEvent({ type: "mouseMove", ...point });
    window.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...point });
    window.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...point });
  };
  const capture = async (name, selector) => {
    await waitFor(`(() => { const node = document.querySelector(${JSON.stringify(selector)});
      if (!node) return false; const rect = node.getBoundingClientRect(); return rect.width > 0 && rect.height > 0; })()`);
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ block: 'center' })`);
    await paint();
    await fs.writeFile(path.join(output, name), (await window.webContents.capturePage()).toPNG());
  };
  const lastReview = () => evaluate("window.turnChangesSmoke.state().calls.filter(call => call.method === 'workbench.changes').at(-1)?.params");
  const fits = "document.documentElement.scrollWidth <= innerWidth";
  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor(`document.querySelectorAll('.turn-changes-card').length === 2 && document.querySelector(${JSON.stringify(firstCard)})`);
  assert(await evaluate(`(() => {
    const rows = [...document.querySelectorAll('.timeline-row')];
    const firstAnswer = rows.findIndex(row => row.textContent.includes('第一轮已完成'));
    const firstChange = rows.findIndex(row => row.querySelector(${JSON.stringify(firstCard)}));
    const nextQuestion = rows.findIndex(row => row.textContent.includes('第二轮继续修改'));
    const secondAnswer = rows.findIndex(row => row.textContent.includes('第二轮已完成'));
    const secondChange = rows.findIndex(row => row.querySelector(${JSON.stringify(secondCard)}));
    return firstAnswer >= 0 && firstAnswer < firstChange && firstChange < nextQuestion
      && secondAnswer >= 0 && secondAnswer < secondChange;
  })()`), "Restored cards must follow their own response rather than the end of the whole chat");
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(firstCard)}).querySelectorAll('.turn-changes-file').length`), 3);
  await click(`${firstCard} .turn-changes-expand`);
  await waitFor(`document.querySelector(${JSON.stringify(firstCard)}).querySelectorAll('.turn-changes-file').length === 4`);
  await capture("turn-changes-card-wide.png", firstCard);
  await click(`${firstCard} [aria-label="查看 src/fourth.ts 的变更"]`);
  await waitFor("document.querySelector('.changes-panel')?.textContent.includes('first-run:src/fourth.ts')");
  assert.deepEqual(await lastReview(), { source: "turn", runId: "first-run", workspacePath: "G:\\offline-turn-changes", sessionId: "turns-current" });
  assert(await evaluate("[...document.querySelectorAll('.changed-file')].some(node => node.open && node.querySelector('.changed-file-path')?.textContent === 'src/fourth.ts')"));
  await capture("turn-changes-details-wide.png", ".changes-panel");
  await click('[aria-label="关闭工具栏"]');
  await click(`${secondCard} .turn-changes-view`);
  await waitFor("document.querySelector('.changes-panel')?.textContent.includes('second-run:src/shared.ts')");
  await click('[aria-label="关闭工具栏"]');
  await click(`${firstCard} .turn-changes-view`);
  await waitFor("document.querySelector('.changes-panel')?.textContent.includes('first-run:src/first.ts')");
  assert.equal(await evaluate("document.querySelector('.changes-panel').textContent.includes('second-run:src/shared.ts')"), false);
  assert.equal((await lastReview()).runId, "first-run");
  window.setSize(820, 720);
  await paint();
  await waitFor(fits);
  await capture("turn-changes-details-narrow.png", ".changes-panel");
  assert(await evaluate("(() => { const rect = document.querySelector('.workbench').getBoundingClientRect(); return rect.left >= -1 && rect.right <= innerWidth + 1; })()"));
  await click('[aria-label="关闭工具栏"]');
  await capture("turn-changes-card-narrow.png", firstCard);
  window.setSize(1440, 1040);
  await paint();
  await evaluate("window.turnChangesSmoke.liveTurn(false)");
  await waitFor("document.body.textContent.includes('本轮只有解释，没有文件变更。')");
  assert.equal(await evaluate("document.querySelectorAll('.turn-changes-card').length"), 2);
  await evaluate("window.turnChangesSmoke.liveTurn(true)");
  await waitFor("document.querySelectorAll('.turn-changes-card [aria-label=\"查看 src/live.ts 的变更\"]').length === 1");
  assert(await evaluate("(() => { const nodes = [...document.querySelectorAll('.timeline-row')]; const answer = nodes.findIndex(node => node.textContent.includes('实时修改已结束。')); const card = nodes.findIndex(node => node.querySelector('.turn-changes-card [aria-label=\"查看 src/live.ts 的变更\"]')); return answer >= 0 && card > answer; })()"));
  await evaluate("[...document.querySelectorAll('.session-item')].find(node => node.textContent.includes('另一个会话')).click()");
  await waitFor("document.body.textContent.includes('这个会话没有代码变更。') && document.querySelectorAll('.turn-changes-card').length === 0");
  await evaluate("window.turnChangesSmoke.staleChange()");
  await paint();
  assert.equal(await evaluate("document.querySelectorAll('.turn-changes-card').length"), 0);
  await evaluate("[...document.querySelectorAll('.session-item')].find(node => node.textContent.includes('逐轮变更审阅')).click()");
  await waitFor("document.querySelectorAll('.turn-changes-card').length === 3");
  for (const stopReason of ["error", "aborted"]) {
    await evaluate(`window.turnChangesSmoke.restoreFailed(${JSON.stringify(stopReason)})`);
    await waitFor("document.querySelectorAll('.turn-changes-card').length === 1 && document.querySelector('[aria-label=\"查看 code.py 的变更\"]')");
    assert(await evaluate("(() => { const rows = [...document.querySelectorAll('.timeline-row')]; const tool = rows.findIndex(node => node.querySelector('.tool-card-view')); const card = rows.findIndex(node => node.querySelector('.turn-changes-card')); return tool >= 0 && card > tool; })()"),
      "Saved changes must survive an invisible terminal message and follow the tool result");
    await click(".turn-changes-view");
    const expectedDiff = JSON.stringify(`failed-${stopReason}:code.py`);
    await waitFor(`document.querySelector('.changes-panel')?.textContent.includes(${expectedDiff})`);
    await click('[aria-label="关闭工具栏"]');
  }
  const finalState = await evaluate("window.turnChangesSmoke.state()");
  assert.equal(finalState.calls.filter((call) => call.method === "run.start").length, 0);
  assert.deepEqual(blockedRequests, []);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output, restoredTurnPositions: true, expandedFiles: true,
    exactReviewAndFile: true, olderReviewPreserved: true, noChangeAddsNoCard: true, duplicateLiveChangeIdempotent: true,
    staleSessionIgnored: true, hiddenErrorAndAbortRestored: true,
    reviewCalls: finalState.calls.filter((call) => call.method === "workbench.changes").map((call) => call.params.runId),
    screenshots: ["turn-changes-card-wide.png", "turn-changes-details-wide.png", "turn-changes-details-narrow.png", "turn-changes-card-narrow.png"].map((name) => path.join(output, name)) }));
  window.destroy();
  app.quit();
}

const watchdog = setTimeout(() => { console.error("Turn changes smoke timed out"); app.exit(1); }, 45_000);
main().then(() => clearTimeout(watchdog)).catch((error) => { console.error(error); app.exit(1); });
