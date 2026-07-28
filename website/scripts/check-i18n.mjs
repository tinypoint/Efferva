import { readdir } from "node:fs/promises";
import { relative, resolve } from "node:path";

const englishRoot = resolve("src/content/docs/docs");
const chineseRoot = resolve("src/content/docs/zh-cn/docs");

const [english, chinese] = await Promise.all([
  collectContentFiles(englishRoot),
  collectContentFiles(chineseRoot),
]);

const missingChinese = english.filter((path) => !chinese.includes(path));
const orphanedChinese = chinese.filter((path) => !english.includes(path));

if (missingChinese.length || orphanedChinese.length) {
  if (missingChinese.length) {
    console.error(
      `Missing zh-CN pages:\n${missingChinese.map((path) => `  - ${path}`).join("\n")}`,
    );
  }
  if (orphanedChinese.length) {
    console.error(
      `Missing English source pages:\n${orphanedChinese.map((path) => `  - ${path}`).join("\n")}`,
    );
  }
  process.exitCode = 1;
} else {
  console.log(`i18n parity OK: ${english.length} English/Chinese page pairs`);
}

async function collectContentFiles(root) {
  const paths = [];
  await walk(root, paths);
  return paths.sort();
}

async function walk(directory, paths) {
  const entries = await readdir(directory, { withFileTypes: true });
  await Promise.all(
    entries.map(async (entry) => {
      const absolute = resolve(directory, entry.name);
      if (entry.isDirectory()) {
        await walk(absolute, paths);
      } else if (entry.name.endsWith(".md") || entry.name.endsWith(".mdx")) {
        paths.push(relative(directory.includes("/zh-cn/") ? chineseRoot : englishRoot, absolute));
      }
    }),
  );
}
