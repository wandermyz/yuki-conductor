// UI smoke test for the Plugins tab and the four-tab + More layout.
// Run against a daemon started with a throwaway YUKI_CONDUCTOR_DATA_DIR:
//   node web/e2e-plugins.mjs [baseUrl]

import { chromium } from "playwright";

const BASE = process.argv[2] || "http://localhost:2444";
const shots = [];
let failures = 0;

function check(label, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
page.on("pageerror", (e) => {
  console.log(`FAIL  console error — ${e.message}`);
  failures++;
});

async function shot(name) {
  const path = `web/.e2e/${name}.png`;
  await page.screenshot({ path });
  shots.push(path);
}

await page.goto(`${BASE}/#chat`, { waitUntil: "networkidle" });

// --- Tab bar: exactly four, last one is the overflow -----------------------
const tabLabels = await page.locator(".tabbar .tab .tab-label").allTextContents();
check("tab bar has 4 tabs", tabLabels.length === 4, tabLabels.join(", "));
check(
  "primary tabs are Chat/Projects/Automations/More",
  JSON.stringify(tabLabels) ===
    JSON.stringify(["Chat", "Projects", "Automations", "More"]),
  tabLabels.join(", "),
);
await shot("01-tabbar");

// --- More overflow lists the three folded tabs -----------------------------
await page.locator(".tabbar .tab", { hasText: "More" }).click();
await page.waitForSelector(".more-list");
const moreItems = await page.locator(".more-list .more-label").allTextContents();
check(
  "More lists Sessions/Status/Plugins",
  JSON.stringify(moreItems) === JSON.stringify(["Sessions", "Status", "Plugins"]),
  moreItems.join(", "),
);
await shot("02-more");

// --- Plugins view ----------------------------------------------------------
await page.locator(".more-list button", { hasText: "Plugins" }).click();
await page.waitForSelector(".plugin-list li");

// The More tab stays active and renames itself to the child tab.
const activeLabel = await page.locator(".tabbar .tab.active .tab-label").textContent();
check("More tab stays active for a child", activeLabel === "Plugins", activeLabel);
check("deep hash is #plugins", page.url().endsWith("#plugins"), page.url());

const names = await page.locator(".plugin-list .plugin-name").allTextContents();
check("slack builtin is listed", names.includes("slack"), names.join(", "));
await shot("03-plugins-list");

// --- Detail pane + enable toggle ------------------------------------------
await page.locator(".plugin-list li", { hasText: "slack" }).click();
await page.waitForSelector(".plugin-detail");
const detailTitle = await page.locator(".plugin-detail h2").textContent();
check("detail opens for slack", detailTitle === "slack");

const removeCount = await page
  .locator(".plugin-detail .delete-btn")
  .count();
check("builtin has no Remove button", removeCount === 0);

const toggle = page.locator(".plugin-detail .plugin-actions .action-btn").first();
const before = await toggle.textContent();
await toggle.click();
await page.waitForFunction(
  (prev) =>
    document.querySelector(".plugin-detail .action-btn")?.textContent !== prev,
  before,
);
const after = await toggle.textContent();
check("enable/disable toggles", before !== after, `${before} -> ${after}`);
await page.waitForSelector(".plugin-banner");
const banner = await page.locator(".plugin-banner").first().textContent();
check(
  "restart-required banner appears",
  /restart/i.test(banner),
  banner?.trim(),
);
await shot("04-plugin-detail");

// Put it back so the daemon's state is unchanged by the test.
await toggle.click();
await page.waitForFunction(
  (prev) =>
    document.querySelector(".plugin-detail .action-btn")?.textContent !== prev,
  after,
);

// --- Add-plugin validation surfaces the server's reason --------------------
// The sidebar stays visible next to the detail pane at desktop width (Back is
// mobile-only, matching the other views), so the form is reachable directly.
await page.locator(".plugin-add input").fill("C:/definitely/not/here");
await page.locator(".plugin-add button").click();
await page.waitForSelector(".plugin-add .plugin-error-line");
const addError = await page.locator(".plugin-add .plugin-error-line").textContent();
check("bad path shows server error", /Not a directory/.test(addError), addError);
await shot("05-add-error");

// --- Mobile layout: detail takes over and Back returns to the list ---------
// Reload so nothing is selected — at narrow width an open detail hides the
// sidebar entirely, which is the behaviour being verified.
await page.goto(`${BASE}/#plugins`, { waitUntil: "networkidle" });
await page.reload({ waitUntil: "networkidle" });
await page.setViewportSize({ width: 420, height: 860 });
await page.waitForSelector(".plugin-list li");
await page.locator(".plugin-list li", { hasText: "slack" }).click();
await page.waitForSelector(".plugin-detail");
await shot("06-mobile-detail");
const backVisible = await page.locator(".plugin-detail .back-btn").isVisible();
check("Back button shows on narrow viewport", backVisible);
if (backVisible) {
  await page.locator(".plugin-detail .back-btn").click();
  await page.waitForSelector(".plugin-list li");
  check("Back returns to the list", await page.locator(".plugin-list").isVisible());
}
await page.setViewportSize({ width: 1200, height: 900 });

// --- Deep link to a folded tab still works ---------------------------------
await page.goto(`${BASE}/#status`, { waitUntil: "networkidle" });
const statusActive = await page.locator(".tabbar .tab.active .tab-label").textContent();
check("#status deep link highlights More", statusActive === "Status", statusActive);
await shot("07-status-deeplink");

await browser.close();
console.log(`\nScreenshots: ${shots.join(", ")}`);
console.log(failures ? `\n${failures} check(s) failed` : "\nAll checks passed");
process.exit(failures ? 1 : 0);
