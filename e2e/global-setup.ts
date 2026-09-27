import { request, type FullConfig } from "@playwright/test";

/** The fast M1 test strategy costs ~0.5R a trade, far above the real 0.15R Cost Check: lift the
 *  limit for this throwaway database, through the normal settings API (there is no bypass). */
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0].use.baseURL as string;
  const ctx = await request.newContext({ baseURL });
  const res = await ctx.put("/api/settings", { data: { cost_check_max_r: 10 } });
  if (!res.ok()) throw new Error(`global setup: PUT /api/settings -> ${res.status()} ${await res.text()}`);
  await ctx.dispose();
}
