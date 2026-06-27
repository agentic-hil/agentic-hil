import { readFileSync } from "node:fs";

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

const tag = process.argv[2] ?? process.env.RELEASE_TAG ?? process.env.GITHUB_REF_NAME;

if (!tag) {
  throw new Error("Release tag is required");
}

const strictSemverTag = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

if (!strictSemverTag.test(tag)) {
  throw new Error(`Release tag must be strict SemVer prefixed with v, got ${tag}`);
}

const pkg = readJson("package.json");
const shrinkwrap = readJson("npm-shrinkwrap.json");
const rootVersion = shrinkwrap.packages?.[""]?.version;

if (tag !== `v${pkg.version}`) {
  throw new Error(`Release tag ${tag} does not match package.json version ${pkg.version}`);
}

if (shrinkwrap.version !== pkg.version || rootVersion !== pkg.version) {
  throw new Error(`npm-shrinkwrap.json version does not match package.json version ${pkg.version}`);
}

console.log(`Release tag ${tag} matches package metadata`);
