import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { delimiter, join } from "node:path";

const isWindows = process.platform === "win32";
const venvPython = join(".venv", isWindows ? "Scripts/python.exe" : "bin/python");
const bootstrap = isWindows ? ["py", ["-3"]] : ["python3", []];

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

if (!existsSync(venvPython)) run(bootstrap[0], [...bootstrap[1], "-m", "venv", ".venv"]);
run(venvPython, ["-m", "pip", "install", "--disable-pip-version-check", "-r", "backend/requirements.txt"]);
console.log(`Backend test environment ready: ${venvPython}`);
