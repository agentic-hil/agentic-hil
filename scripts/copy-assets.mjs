import { cpSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const assetDirectories = ["schemas", "templates"];

for (const directory of assetDirectories) {
  const source = resolve(root, "src", "aihil", directory);
  const target = resolve(root, "dist", directory);
  if (existsSync(source)) {
    mkdirSync(dirname(target), { recursive: true });
    cpSync(source, target, { recursive: true });
  }
}
