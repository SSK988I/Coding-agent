// Native Electron clipboard probe. Never replaces unknown clipboard formats.
const fsSync = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

// Electron42 does not expose a clipboard sequence number. This Windows-only
// probe reads just that counter, so restoring our snapshot cannot overwrite a
// newer file list copied by the user while the native smoke is running.
function clipboardSequence() {
  if (process.platform !== "win32") return null;
  const script = '$ProgressPreference = "SilentlyContinue"; Add-Type -TypeDefinition \'using System.Runtime.InteropServices; public static class ClipboardProbeStamp { [DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber(); }\'; [ClipboardProbeStamp]::GetClipboardSequenceNumber()';
  try {
    const result = require("node:child_process").execFileSync(path.join(process.env.SystemRoot ?? "C:\\Windows",
      "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
    { encoding: "utf8", timeout: 5000, windowsHide: true });
    return /^\d+$/.test(result.trim()) ? result.trim() : null;
  } catch { return null; }
}

// The Node parent waits for Electron's storage handles to close before removing
// the probe profile. Electron itself cannot reliably remove its live profile.
if (!process.versions.electron) {
  const directory = fsSync.mkdtempSync(path.join(os.tmpdir(), "coding-agent-clipboard-"));
  const environment = { ...process.env, CODING_AGENT_CLIPBOARD_PROBE_DIR: directory };
  delete environment.ELECTRON_RUN_AS_NODE;
  let status = 1;
  try {
    const result = require("node:child_process").spawnSync(require("electron"), [__filename], {
      stdio: "inherit", env: environment,
    });
    if (result.error) throw result.error;
    status = result.status ?? 1;
  } finally {
    assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
    assert(path.basename(directory).startsWith("coding-agent-clipboard-"));
    fsSync.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
  process.exit(status);
}

const { app, BrowserWindow, Menu, clipboard, nativeImage, session } = require("electron");

async function main() {
  app.disableHardwareAcceleration();
  const directory = process.env.CODING_AGENT_CLIPBOARD_PROBE_DIR;
  assert(directory, "Run this probe through Node so its temporary profile can be cleaned up");
  assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
  assert(path.basename(directory).startsWith("coding-agent-clipboard-"));
  app.setPath("userData", path.join(directory, "profile"));
  await app.whenReady();
  Menu.setApplicationMenu(null);
  session.defaultSession.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*"] },
    (_details, callback) => callback({ cancel: true }));
  const window = new BrowserWindow({ show: false, width: 1280, height: 900,
    webPreferences: { preload: path.join(__dirname, "vision.preload.cjs"),
      contextIsolation: true, backgroundThrottling: false, offscreen: true } });
  const evaluate = (source) => window.webContents.executeJavaScript(source);
  const waitFor = async (source) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await evaluate(source)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const diagnostic = await evaluate(`({
      images: document.querySelectorAll('.pending-image').length,
      files: Array.from(document.querySelectorAll('.pending-file')).map(file => file.textContent),
      dimensions: Array.from(document.querySelectorAll('.pending-image img')).map(image => ({
        width: image.naturalWidth, height: image.naturalHeight, complete: image.complete })),
      error: document.querySelector('[role=alert]')?.textContent,
      reading: document.querySelector('.attachment-hint')?.textContent,
      paste: window.clipboardProbe })`);
    throw Error(`Clipboard assertion timed out: ${source}\n${JSON.stringify(diagnostic)}`);
  };
  const formats = clipboard.availableFormats();
  const commonFormats = new Set(["text/plain", "text/html", "text/rtf", "image/png"]);
  let restore = null;
  try {
    await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
    await waitFor("document.querySelector('.attachment-trigger') && !document.querySelector('.attachment-trigger').disabled");
    await evaluate(`document.addEventListener('paste', event => {
      window.clipboardProbe = { types: Array.from(event.clipboardData.types),
        files: event.clipboardData.files.length,
        names: Array.from(event.clipboardData.files).map(file => file.name),
        items: Array.from(event.clipboardData.items).map(item => ({ kind: item.kind, type: item.type })) };
    }, true); document.querySelector('textarea').focus();`);
    console.log(JSON.stringify({ nativeFormats: formats }));
    if (formats.some((format) => !commonFormats.has(format))) {
      // Do not read or overwrite application-specific clipboard payloads.
      console.log("SKIP native clipboard replacement: existing custom formats must be preserved.");
      return;
    }
    const saved = {};
    if (formats.includes("text/plain")) saved.text = clipboard.readText();
    if (formats.includes("text/html")) saved.html = clipboard.readHTML();
    if (formats.includes("text/rtf")) saved.rtf = clipboard.readRTF();
    if (formats.includes("image/png")) saved.image = clipboard.readImage();
    // Use a valid bitmap instead of relying on a fixture PNG decoder. A failed
    // write must never turn this into a test of the user's existing screenshot.
    const fixture = nativeImage.createFromBitmap(Buffer.from([40, 80, 120, 255]), { width: 1, height: 1 });
    assert(!fixture.isEmpty(), "Clipboard fixture must decode to a native image");
    const fixtureBytes = fixture.toBitmap();
    let ownsClipboard = () => {
      const currentFormats = clipboard.availableFormats();
      return !currentFormats.some((format) => format !== "image/png")
        && clipboard.readImage().toBitmap().equals(fixtureBytes);
    };
    restore = () => {
      // A user may copy something while the probe runs. Never replace their
      // newer clipboard content with our saved snapshot in that case.
      if (!ownsClipboard()) {
        console.log("Clipboard changed during probe; preserving newer clipboard content.");
        return;
      }
      if (formats.length) clipboard.write(saved);
      else clipboard.clear();
    };
    clipboard.writeImage(fixture);
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.deepEqual(clipboard.readImage().getSize(), { width: 1, height: 1 }, "Clipboard write must replace prior content");
    assert(clipboard.readImage().toBitmap().equals(fixtureBytes), "Clipboard must contain only the generated fixture");
    window.webContents.paste();
    await waitFor("window.clipboardProbe");
    console.log(JSON.stringify({ nativePaste: await evaluate("window.clipboardProbe") }));
    await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && document.querySelector('.pending-image img')?.naturalHeight === 1");
    assert.equal(await evaluate("document.querySelectorAll('.pending-image').length"), 1);
    console.log("PASS native clipboard image -> webContents.paste -> composer attachment");
    await evaluate("document.querySelector('.pending-image button').click(); window.clipboardProbe = null; document.querySelector('textarea').focus()");
    window.webContents.focus();
    window.webContents.sendInputEvent({ type: "keyDown", keyCode: "V", modifiers: ["control"] });
    window.webContents.sendInputEvent({ type: "keyUp", keyCode: "V", modifiers: ["control"] });
    await waitFor("window.clipboardProbe");
    console.log(JSON.stringify({ keyboardPaste: await evaluate("window.clipboardProbe"), focused: window.isFocused() }));
    await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && document.querySelector('.pending-image img')?.naturalHeight === 1");
    assert.equal(await evaluate("document.querySelectorAll('.pending-image').length"), 1);
    console.log("PASS native clipboard image -> Ctrl+V -> composer attachment");
    await evaluate(`document.querySelector('.pending-image button').click();
      window.clipboardProbe = null;
      document.querySelector('textarea').focus();
      document.querySelector('textarea').addEventListener('keydown', event => {
        if (event.ctrlKey && event.key.toLowerCase() === 'v') event.preventDefault();
      }, { capture: true, once: true });`);
    // Simulate a host that consumes the native paste action while still
    // delivering the keyboard gesture to the composer. The fallback must use
    // Chromium's real Clipboard API, not a synthetic DataTransfer fixture.
    // This hidden/offscreen test window needs browser focus emulation for the
    // Clipboard API; normal foreground application windows get real focus.
    window.webContents.debugger.attach("1.3");
    await window.webContents.debugger.sendCommand("Emulation.setFocusEmulationEnabled", { enabled: true });
    window.webContents.sendInputEvent({ type: "keyDown", keyCode: "V", modifiers: ["control"] });
    window.webContents.sendInputEvent({ type: "keyUp", keyCode: "V", modifiers: ["control"] });
    await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && document.querySelector('.pending-image img')?.naturalHeight === 1");
    assert.equal(await evaluate("window.clipboardProbe"), null);
    assert.equal(await evaluate("document.querySelectorAll('.pending-image').length"), 1);
    console.log("PASS real Clipboard API fallback without a native paste event");
    await evaluate("document.querySelector('.pending-image button').click()");

    const files = [path.join(directory, "中文 说明.md"), path.join(directory, "sample.json")];
    fsSync.writeFileSync(files[0], "# Native file attachment\n中文内容");
    fsSync.writeFileSync(files[1], '{"native":true}');
    const expectedNames = files.map(file => path.basename(file));
    if (process.platform === "win32" && clipboardSequence() !== null && ownsClipboard()) {
      // This Electron test API writes the real CF_HDROP native format, exactly
      // as Explorer copy does. No synthetic ClipboardEvent/File substitutes.
      assert.equal(typeof clipboard._writeFilesForTesting, "function");
      clipboard._writeFilesForTesting(files);
      const copiedSequence = clipboardSequence();
      let verified = false;
      ownsClipboard = () => verified && copiedSequence !== null && clipboardSequence() === copiedSequence;
      await evaluate("window.clipboardProbe = null; document.querySelector('textarea').focus()");
      window.webContents.paste();
      await waitFor("window.clipboardProbe?.files === 2");
      const nativeFiles = await evaluate("window.clipboardProbe");
      assert.deepEqual(nativeFiles.names, expectedNames);
      verified = true;
      await waitFor("document.querySelectorAll('.pending-file').length === 2 && !document.querySelector('.send-button').disabled");
      assert.equal(await evaluate("document.querySelectorAll('.pending-image').length"), 0);
      console.log("PASS native copied multiple files -> webContents.paste -> composer attachments");
      await evaluate("document.querySelectorAll('.pending-file button').forEach(button => button.click()); window.clipboardProbe = null; document.querySelector('textarea').focus()");
      window.webContents.sendInputEvent({ type: "keyDown", keyCode: "V", modifiers: ["control"] });
      window.webContents.sendInputEvent({ type: "keyUp", keyCode: "V", modifiers: ["control"] });
      await waitFor("window.clipboardProbe?.files === 2 && document.querySelectorAll('.pending-file').length === 2");
      assert.deepEqual((await evaluate("window.clipboardProbe")).names, expectedNames);
      console.log("PASS native copied multiple files -> Ctrl+V -> composer attachments");
      await evaluate("document.querySelectorAll('.pending-file button').forEach(button => button.click())");
    } else {
      console.log("SKIP Windows copied-file clipboard replacement: safe ownership counter unavailable or clipboard changed.");
    }
    const { root } = await window.webContents.debugger.sendCommand("DOM.getDocument");
    const { nodeId } = await window.webContents.debugger.sendCommand("DOM.querySelector", { nodeId: root.nodeId, selector: "input[type=file]" });
    await window.webContents.debugger.sendCommand("DOM.setFileInputFiles", { nodeId, files });
    await waitFor("document.querySelectorAll('.pending-file').length === 2 && !document.querySelector('.send-button').disabled");
    await evaluate("document.querySelector('.send-button').click()");
    await waitFor("window.visionSmoke.calls().length === 1 && !document.querySelector('.pending-file')");
    const fileCall = (await evaluate("window.visionSmoke.calls()"))[0];
    assert.deepEqual(fileCall.files.map(file => file.name), expectedNames);
    assert.equal(Buffer.from(fileCall.files[0].data, "base64").toString("utf8"), "# Native file attachment\n中文内容");
    assert.equal(Buffer.from(fileCall.files[1].data, "base64").toString("utf8"), '{"native":true}');
    console.log("PASS real file chooser File objects -> FileReader -> files RPC with exact content");
  } finally {
    if (restore) restore();
    window.destroy();
    app.quit();
  }
}
main().catch((error) => { console.error(error); app.exit(1); });
