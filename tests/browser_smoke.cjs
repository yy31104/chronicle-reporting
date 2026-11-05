/* Real Chromium UI smoke. Credentials stay in this process and are never logged. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.env.BASE_URL || "http://127.0.0.1:8787";
const origin = new URL(base);
assert(["127.0.0.1", "localhost"].includes(origin.hostname), "Smoke credentials require a local endpoint");
const credentialsPath = path.resolve(process.env.CREDENTIALS_PATH || "var/credentials.json");
let credentials;
try {
  credentials = JSON.parse(fs.readFileSync(credentialsPath, "utf8"));
  if (!credentials || typeof credentials.viewer !== "string" || credentials.viewer.length < 24) throw new Error();
} catch {
  process.stderr.write("Credentials unavailable or invalid; values omitted.\n");
  process.exit(1);
}
const secrets = Object.values(credentials).filter(value => typeof value === "string");
const screenshots = path.resolve(process.env.SCREENSHOT_DIR || "var/browser");
fs.mkdirSync(screenshots, { recursive: true });
const summary = { browser: "Chromium", base_url: origin.origin, checks: [], screenshots: [] };
let browser;

function sanitized(error) {
  let message = String(error?.stack || error);
  for (const secret of secrets) message = message.split(secret).join("[REDACTED]");
  return message;
}

(async () => {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: "en-US", acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const scriptErrors = [];
  let downloadCount = 0;
  page.on("pageerror", error => scriptErrors.push(error.name));
  page.on("download", () => { downloadCount++; });
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#token").fill(credentials.viewer);
  await page.locator("#login-form button").click();
  await page.locator("#workspace").waitFor({ state: "visible" });
  assert.equal(await page.locator("#identity").textContent(), "Viewer");
  assert.equal(await page.locator("#token").inputValue(), "");
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal((await context.cookies()).length, 0);
  summary.checks.push("viewer_login_without_persistent_token_storage");

  async function runReport({ dataset, seriesLabel, formula, month, left, right, mode = "system_snapshot", cutoff }) {
    await page.locator("#dataset").selectOption(dataset);
    await page.waitForFunction(() => !document.querySelector("#series").disabled && !document.querySelector("#run").disabled);
    await page.locator("#series").selectOption({label:seriesLabel});
    await page.locator("#formula").selectOption(formula);
    await page.locator("#month").fill(month);
    await page.locator("#mode").selectOption(mode);
    await page.locator("#left-snapshot").selectOption(left);
    await page.locator("#right-snapshot").selectOption(right);
    if (cutoff) await page.locator("#cutoff").fill(cutoff);
    await page.locator("#run").click();
    await page.waitForFunction(() => !document.querySelector("#run").disabled &&
      (!document.querySelector("#results").hidden || document.querySelector("#notice").textContent.trim()));
    assert.equal((await page.locator("#notice").textContent()).trim(), "", "Report should not display an error");
    assert.equal(await page.locator("#rows tr").count(), 1);
    return page.locator("#rows td").evaluateAll(cells => cells.slice(1).map(cell => cell.firstChild.textContent.trim()));
  }

  const synthetic = { dataset: "chronicle-synthetic-v1", month: "2026-01", left: "S1", right: "S2" };
  assert.deepEqual(await runReport({ ...synthetic, formula: "employment_delta", seriesLabel: "Employment · Synthetic" }),
    ["10", "13", "3", "3"]);
  assert.equal(await page.locator("#left-evidence details").count(), 2);
  assert.equal(await page.locator("#right-evidence details").count(), 2);
  await page.locator("#left-evidence details").nth(1).locator("summary").click();
  await page.locator("#right-evidence details").nth(1).locator("summary").click();
  assert.match(await page.locator("#left-evidence details").nth(1).textContent(), /2026-01 · 100/);
  assert.match(await page.locator("#right-evidence details").nth(1).textContent(), /2026-01 · 103/);
  assert.match(await page.locator("#right-evidence details").nth(1).textContent(), /B2.*2026-03-15T00:00:00\.000000Z/s);
  summary.checks.push("synthetic_employment_10_13_difference_3_with_batch_receipt_evidence");
  const desktop = path.join(screenshots, "C4-ui-desktop.png");
  await page.screenshot({ path: desktop, fullPage: true });
  summary.screenshots.push(path.relative(process.cwd(), desktop));

  const downloaded = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloaded;
  assert.equal(download.suggestedFilename(), "chronicle-comparison.json");
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  const saved = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  assert.equal(saved.left.rows[0].value, "10.000000000000");
  assert.equal(saved.right.rows[0].value, "13.000000000000");
  summary.checks.push("synthetic_export_download_contains_server_authorized_results");

  assert.deepEqual(await runReport({ ...synthetic, formula: "production_mean3", seriesLabel: "Industrial production · Synthetic" }),
    ["102.666666666667", "102.666666666667", "0", "0"]);
  assert.equal(await page.locator("#left-evidence details").count(), 3);
  summary.checks.push("synthetic_three_calendar_month_production_mean");

  if (process.env.SYNTHETIC_ONLY !== "1") {
  assert.deepEqual(await runReport({
    dataset: "macro-archive-frb-v1", month: "2024-01",
    left: "FRB-2024-02-15", right: "FRB-2024-03-15", formula: "production_mean3", seriesLabel: "Industrial production · Federal Reserve G.17"
  }), ["102.666666666667", "102.633333333333", "-0.033333333334", "-0.033333333333"]);
  assert.equal(await page.locator("#right-evidence details").count(), 3);
  assert.equal(await page.locator("#namespace").count(), 0);
  assert.equal(await page.locator("#series").evaluate(node => node.tagName), "SELECT");
  assert.equal(await page.locator("#left-snapshot").evaluate(node => node.tagName), "SELECT");
  assert.match(await page.locator("#left-evidence").textContent(), /Named snapshot/);
  assert.match(await page.locator("#rows td").nth(1).textContent(), /Index/);
  summary.checks.push("real_frb_dropdown_only_selection_and_canonical_difference");
  const frbScreenshot = path.join(screenshots, "C4-ui-frb-dropdowns.png");
  await page.screenshot({ path: frbScreenshot, fullPage: true });
  summary.screenshots.push(path.relative(process.cwd(), frbScreenshot));
  const beforeDenied = downloadCount;
  const deniedResponse = page.waitForResponse(response => response.url().endsWith("/comparison-export"));
  await page.locator("#export").click();
  assert.equal((await deniedResponse).status(), 403);
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("RIGHTS_DENIED"));
  assert.equal(downloadCount, beforeDenied, "Rights denial must not trigger a cached/fake download");
  summary.checks.push("real_source_export_denied_without_download");

  const blsAvailable = await page.locator('#dataset option[value="macro-archive-bls-v1"]').count();
  if (blsAvailable) {
    assert.deepEqual(await runReport({dataset:"macro-archive-bls-v1",month:"2024-01",
      left:"BLS-2024-02-02",right:"BLS-2024-03-08",formula:"employment_delta",seriesLabel:"Employment · BLS"}),
      ["353", "229", "-124", "-124"]);
    assert.match(await page.locator("#rows td").nth(1).textContent(), /Thousand persons/);
    assert.equal(await page.locator("#left-evidence details").count(), 2);
    assert.equal(await page.locator("#right-evidence details").count(), 2);
    await page.locator("#left-evidence details").nth(1).locator("summary").click();
    await page.locator("#right-evidence details").nth(1).locator("summary").click();
    assert.match(await page.locator("#left-evidence details").nth(1).textContent(), /2024-01 · 157700/);
    assert.match(await page.locator("#right-evidence details").nth(1).textContent(), /2024-01 · 157533/);
    assert.match(await page.locator("#right-evidence details").nth(1).textContent(), /bls-2024-03-08/);
    assert.match(await page.locator("#right-evidence details").nth(1).textContent(), /2026-10-29T\d{2}:\d{2}:\d{2}\.\d{6}Z/);
    const blsScreenshot = path.join(screenshots, "C4-ui-bls-dropdowns.png");
    await page.screenshot({ path: blsScreenshot, fullPage: true });
    summary.screenshots.push(path.relative(process.cwd(), blsScreenshot));
    summary.checks.push("real_bls_dropdown_only_353_229_difference_minus124");
    summary.checks.push("real_bls_original_and_corrected_levels_with_actual_receipt_evidence");
  } else {
    assert.notEqual(process.env.REQUIRE_BLS, "1", "BLS dropdown acceptance requires imported BLS data");
    summary.not_run = ["BLS dropdown: macro-archive-bls-v1 is not available to this viewer"];
  }

  }

  await page.route("**/v1/datasets/chronicle-synthetic-v1/series", route => route.fulfill({
    status:400,contentType:"text/plain",headers:{"X-Request-ID":"smoke-nonjson"},body:"Invalid host header"
  }));
  await page.locator("#dataset").selectOption("chronicle-synthetic-v1");
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("HTTP 400"));
  assert.match(await page.locator("#notice").textContent(), /Request smoke-nonjson/);
  assert.equal(await page.locator("#run").isDisabled(), true);
  await page.unroute("**/v1/datasets/chronicle-synthetic-v1/series");
  summary.checks.push("non_json_error_shows_http_status_and_request_id_without_parse_exception");

  await page.setViewportSize({ width: 390, height: 844 });
  assert.deepEqual(await runReport({ ...synthetic, formula: "employment_delta", seriesLabel: "Employment · Synthetic" }),
    ["10", "13", "3", "3"]);
  await page.locator("#right-evidence details").nth(1).locator("summary").click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
    "Mobile page must not overflow horizontally");
  const mobile = path.join(screenshots, "C4-ui-mobile.png");
  await page.screenshot({ path: mobile, fullPage: true });
  summary.screenshots.push(path.relative(process.cwd(), mobile));
  summary.checks.push("390px_mobile_layout_and_evidence_without_page_overflow");

  await page.locator("#logout").click();
  await page.locator("#login").waitFor({ state: "visible" });
  assert.equal(await page.evaluate(() => token === ""), true);
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal((await context.cookies()).length, 0);
  assert.equal(await page.evaluate(async () => (await fetch("/v1/datasets")).status), 401);
  assert.deepEqual(scriptErrors, []);
  summary.checks.push("logout_clears_memory_and_unauthenticated_requests_are_denied");
  summary.status = "passed";
  summary.executed_at = new Date().toISOString();
  fs.writeFileSync(path.join(screenshots, "C4-browser-smoke.json"), JSON.stringify(summary, null, 2) + "\n");
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  await context.close();
})().catch(error => {
  process.stderr.write(sanitized(error) + "\n");
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
});
