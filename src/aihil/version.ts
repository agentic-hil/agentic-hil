import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function packageVersion(): string {
  const packagePath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "package.json");
  try {
    const packageJson = JSON.parse(readFileSync(packagePath, "utf8")) as { version?: unknown };
    if (typeof packageJson.version === "string") {
      return packageJson.version;
    }
  } catch {
    // Fall through to a stable value when running from an unusual layout.
  }
  return "unknown";
}
