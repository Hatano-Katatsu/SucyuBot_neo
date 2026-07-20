import { readdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const staticRoot = path.join(projectRoot, "telegram_comfyui_selfie", "static");
const entries = await readdir(staticRoot, { withFileTypes: true });
const scripts = entries
  .filter(entry => entry.isFile() && /\.(?:js|mjs|cjs)$/.test(entry.name))
  .map(entry => path.join(staticRoot, entry.name))
  .sort();

let failed = false;
for (const script of scripts) {
  const result = spawnSync(process.execPath, ["--check", script], {
    cwd: projectRoot,
    encoding: "utf8",
  });
  if (result.status === 0) continue;
  failed = true;
  process.stderr.write(result.stdout || "");
  process.stderr.write(result.stderr || "");
}

if (failed) process.exitCode = 1;
else process.stdout.write(`Checked ${scripts.length} frontend scripts.\n`);
