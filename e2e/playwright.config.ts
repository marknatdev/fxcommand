import { defineConfig, devices } from "@playwright/test";
import { mkdirSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";

const PORT = 8765;
const tmp = path.resolve(__dirname, ".tmp");
// The config is evaluated by the runner and by every worker: pick the fresh DB once, workers inherit it.
if (!process.env.FXC_E2E_DB) {
  mkdirSync(tmp, { recursive: true });
  for (const f of readdirSync(tmp)) {
    try {
      rmSync(path.join(tmp, f), { force: true });
    } catch {
      /* still locked by a previous server: harmless, the new run uses a new file */
    }
  }
  process.env.FXC_E2E_DB = path.join(tmp, `e2e-${Date.now()}.db`);
}

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1, // one backend, one engine: tests share its state and run in file order
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    // The real app (engine + API + built dashboard) against the simulated broker, clock stopped:
    // tests move the market deterministically through /api/sim/advance and /api/sim/shock.
    command: "uv run --no-sync --directory ../backend fxcommand",
    url: `http://127.0.0.1:${PORT}/api/status`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
    env: {
      BROKER: "sim",
      FXC_PORT: String(PORT),
      FXC_DB: process.env.FXC_E2E_DB!,
      FXC_SIM_SPEED: "0",
      FXC_SIM_SEED: "42",
      FXC_SIM_START: "1704700800", // Monday 2024-01-08 08:00 server time
      FXC_STATIC: path.resolve(__dirname, "../frontend/dist"),
    },
  },
});
