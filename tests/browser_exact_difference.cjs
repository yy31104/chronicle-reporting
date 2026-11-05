/* Real Chromium check of the side-by-side stored/exact difference columns.
 * Uses only explicitly synthetic datasets: chronicle-synthetic-v1 (make demo-synthetic)
 * and chronicle-synthetic-exact-v1 (scripts/seed_exact_difference_demo.py).
 * Credentials stay in this process and are never logged. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.env.BASE_URL || "http://127.0.0.1:8787";
const origin = new URL(base);
assert(["127.0.0.1", "localhost"].includes(origin.hostname), "Credentials require a local endpoint");
let credentials;
try {
  credentials = JSON.parse(fs.readFileSync(path.resolve(process.env.CREDENTIALS_PATH), "utf8"));
  if (typeof credentials.viewer !== "string" || credentials.viewer.length < 24) throw new Error();
} catch {
  process.stderr.write("Credentials unavailable or invalid; values omitted.\n");
  process.exit(1);
}
const secrets = Object.values(credentials).filter(value => typeof value === "string");
const output = path.resolve(process.env.SCREENSHOT_DIR || ".");
fs.mkdirSync(output, { recursive: true });
const summary = { browser: "Chromium", base_url: origin.origin, synthetic_only: true, checks: [], screenshots: [] };
let browser;

(async () => {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: "en-US" });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const scriptErrors = [];
  const endpoints = [];
  page.on("pageerror", error => scriptErrors.push(error.name));
  page.on("request", request => { if (request.method() === "POST") endpoints.push(new URL(request.url()).pathname); });
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#token").fill(credentials.viewer);
  await page.locator("#login-form button").click();
  await page.locator("#workspace").waitFor({ state: "visible" });
  assert.deepEqual(await page.locator("thead th").allTextContents(),
    ["Metric / month", "Left", "Right", "DifferenceSaved output subtraction", "Exact differenceRational subtraction, rounded once"]);
  summary.checks.push("header_shows_stored_and_exact_difference_columns");

  async function run({ dataset, seriesLabel, formula }) {
    await page.locator("#dataset").selectOption(dataset);
    await page.waitForFunction(() => !document.querySelector("#series").disabled && !document.querySelector("#run").disabled);
    await page.locator("#series").selectOption({ label: seriesLabel });
    await page.locator("#formula").selectOption(formula);
    await page.locator("#month").fill("2026-01");
    await page.locator("#mode").selectOption("system_snapshot");
    await page.locator("#left-snapshot").selectOption("S1");
    await page.locator("#right-snapshot").selectOption("S2");
    await page.locator("#run").click();
    await page.waitForFunction(() => !document.querySelector("#run").disabled &&
      (!document.querySelector("#results").hidden || document.querySelector("#notice").textContent.trim()));
    assert.equal((await page.locator("#notice").textContent()).trim(), "", "Report should not display an error");
    assert.equal(await page.locator("#rows tr").count(), 1);
    return page.locator("#rows td").evaluateAll(cells => cells.slice(1).map(cell => cell.firstChild.textContent.trim()));
  }

  assert.deepEqual(await run({ dataset: "chronicle-synthetic-v1", seriesLabel: "Employment · Synthetic", formula: "employment_delta" }),
    ["10", "13", "3", "3"]);
  assert.match(await page.locator("#rows td").nth(4).textContent(), /Exact integer/);
  assert.doesNotMatch(await page.locator("#rows td").nth(4).textContent(), /Difference from stored result:/);
  summary.checks.push("synthetic_employment_stored_3_exact_3_no_gap");

  assert.deepEqual(await run({ dataset: "chronicle-synthetic-exact-v1", seriesLabel: "Industrial production · Synthetic", formula: "production_mean3" }),
    ["102.666666666667", "102.633333333333", "-0.033333333334", "-0.033333333333"]);
  const exactCell = await page.locator("#rows td").nth(4).textContent();
  assert.match(exactCell, /Exact value -1\/30/);
  assert.match(exactCell, /Difference from stored result: -0\.000000000001 \(stored result rounds twice\)/);
  assert(endpoints.some(p => p.endsWith("/exact-comparisons")), "UI must call the exact comparison endpoint");
  summary.checks.push("synthetic_mean3_stored_minus0.033333333334_exact_minus0.033333333333_gap_shown");
  const desktop = path.join(output, "exact-difference-desktop.png");
  await page.screenshot({ path: desktop, fullPage: true });
  summary.screenshots.push(path.basename(desktop));

  await page.setViewportSize({ width: 390, height: 844 });
  await run({ dataset: "chronicle-synthetic-exact-v1", seriesLabel: "Industrial production · Synthetic", formula: "production_mean3" });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
    "Mobile page must not overflow horizontally");
  const mobile = path.join(output, "exact-difference-mobile.png");
  await page.screenshot({ path: mobile, fullPage: true });
  summary.screenshots.push(path.basename(mobile));
  summary.checks.push("390px_mobile_without_page_overflow");

  assert.deepEqual(scriptErrors, []);
  summary.status = "passed";
  summary.executed_at = new Date().toISOString();
  fs.writeFileSync(path.join(output, "exact-difference-browser.json"), JSON.stringify(summary, null, 2) + "\n");
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  await context.close();
})().catch(error => {
  let message = String(error?.stack || error);
  for (const secret of secrets) message = message.split(secret).join("[REDACTED]");
  process.stderr.write(message + "\n");
  process.exitCode = 1;
}).finally(async () => { if (browser) await browser.close(); });
