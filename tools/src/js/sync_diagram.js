#!/usr/bin/env node
// Синхронизирует триаду файлов одной диаграммы: <name>.mmd (исходник
// mermaid), <name>.md (тот же исходник в ```mermaid-блоке для просмотра)
// и <name>.svg (рендер через @mermaid-js/mermaid-cli — работает независимо
// от того, какие превью-расширения установлены в редакторе).
//
// Передавайте ЛИБО .mmd, ЛИБО .md — тот файл, который только что правили
// руками, становится источником истины для этого запуска: второй текстовый
// файл и .svg перегенерируются из него. Это важно, потому что человек,
// правящий диаграмму в VS Code, естественным образом редактирует .md (у него
// живой предпросмотр) — эта правка должна попасть обратно в .mmd, а не быть
// молча затёртой при следующем прогоне, если считать .mmd канонiчным.
//
// Использование: node sync_diagram.js <path/to/diagram.mmd|diagram.md> ["Заголовок"]

const fs = require("fs");
const path = require("path");
const { execFileSync, execSync } = require("child_process");

// Держим версию @mermaid-js/mermaid-cli зафиксированной здесь и в
// tools/package.json (devDependencies), чтобы рендер диаграмм был
// воспроизводим независимо от того, что попадёт из npm registry "latest".
const MERMAID_CLI_VERSION = "11.17.0";

function titleFromExistingMd(mdPath) {
  if (!fs.existsSync(mdPath)) return null;
  const text = fs.readFileSync(mdPath, "utf8");
  const match = text.match(/^#\s+(.+)$/m);
  return match ? match[1].trim() : null;
}

function titleFromName(basename) {
  return basename
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function extractMermaidBlock(mdText) {
  const match = mdText.match(/```mermaid\n([\s\S]*?)```/);
  if (!match) {
    throw new Error("No ```mermaid fenced block found in the .md file.");
  }
  return match[1].replace(/\n$/, "");
}

function main() {
  const [, , inputArg, titleArg] = process.argv;
  if (!inputArg) {
    console.error('Usage: node sync_diagram.js <diagram.mmd|diagram.md> ["Title"]');
    process.exit(1);
  }

  const inputPath = path.resolve(inputArg);
  const dir = path.dirname(inputPath);
  const base = path.basename(inputPath).replace(/\.(mmd|md)$/i, "");
  const mmdPath = path.join(dir, `${base}.mmd`);
  const mdPath = path.join(dir, `${base}.md`);
  const svgPath = path.join(dir, `${base}.svg`);

  let mermaidSource;
  if (inputPath.toLowerCase().endsWith(".md")) {
    mermaidSource = extractMermaidBlock(fs.readFileSync(inputPath, "utf8"));
  } else {
    mermaidSource = fs.readFileSync(inputPath, "utf8");
  }
  mermaidSource = mermaidSource.replace(/\s+$/, "") + "\n";

  const title = titleArg || titleFromExistingMd(mdPath) || titleFromName(base);

  fs.writeFileSync(mmdPath, mermaidSource);
  fs.writeFileSync(mdPath, `# ${title}\n\n\`\`\`mermaid\n${mermaidSource}\`\`\`\n`);

  console.log(`Wrote ${mmdPath}`);
  console.log(`Wrote ${mdPath}`);

  console.log(`Rendering SVG via @mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION} ...`);
  try {
    const args = ["-y", `@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}`, "-i", mmdPath, "-o", svgPath];
    if (process.platform === "win32") {
      // npx на Windows резолвится в npx.cmd, который spawnSync не может
      // запустить напрямую (EINVAL) — нужен shell. Экранируем каждый
      // аргумент сами, чтобы shell:true не скатился в небезопасную
      // конкатенацию неэкранированных аргументов.
      const quoted = args.map((a) => `"${a.replace(/"/g, '""')}"`).join(" ");
      execSync(`npx ${quoted}`, { stdio: "inherit", shell: true });
    } else {
      execFileSync("npx", args, { stdio: "inherit" });
    }
    console.log(`Wrote ${svgPath}`);
  } catch (err) {
    console.error(
      "Mermaid render failed - .mmd/.md уже обновлены выше, " +
        "но проверьте вывод ошибки mermaid-cli и поправьте синтаксис."
    );
    process.exit(1);
  }
}

main();
