import { readFileSync } from "node:fs";

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function stable(value) {
  if (Array.isArray(value)) {
    return value.map(stable);
  }

  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, stable(nested)]),
    );
  }

  return value;
}

function assertEqual(label, actual, expected) {
  if (JSON.stringify(stable(actual)) !== JSON.stringify(stable(expected))) {
    throw new Error(`${label} in npm-shrinkwrap.json does not match package.json`);
  }
}

const pkg = readJson("package.json");
const shrinkwrap = readJson("npm-shrinkwrap.json");
const root = shrinkwrap.packages?.[""];

if (!root) {
  throw new Error("npm-shrinkwrap.json is missing the root package entry");
}

assertEqual("name", shrinkwrap.name, pkg.name);
assertEqual("version", shrinkwrap.version, pkg.version);
assertEqual("root name", root.name, pkg.name);
assertEqual("root version", root.version, pkg.version);
assertEqual("dependencies", root.dependencies ?? {}, pkg.dependencies ?? {});
assertEqual("devDependencies", root.devDependencies ?? {}, pkg.devDependencies ?? {});
assertEqual("bin", root.bin ?? {}, pkg.bin ?? {});
assertEqual("engines", root.engines ?? {}, pkg.engines ?? {});

console.log("npm-shrinkwrap.json is aligned with package.json");
