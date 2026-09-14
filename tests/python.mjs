import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";

const python = join(".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
if (!existsSync(python)) {
  console.error("Backend test environment is missing. Run: npm run setup:backend-test");
  process.exit(1);
}
const result = spawnSync(python, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
