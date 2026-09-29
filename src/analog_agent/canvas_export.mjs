// Structural export only. PDK bindings must already be reviewed in the
// Analog Canvas project; site-specific remapping belongs in a private adapter.
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [root, source, output] = process.argv.slice(2);
if (!root || !source || !output) throw new Error("Expected CANVAS_ROOT PROJECT OUTPUT");
const moduleUrl = (relative) => pathToFileURL(join(root, relative)).href;
const { parseProject } = await import(moduleUrl("packages/project-protocol/dist/index.js"));
const { createDesignNetlistExport, unfinishedDrawingDiagnostics } = await import(
  moduleUrl("packages/netlist/dist/index.js"));
const project = parseProject(await readFile(source, "utf8"));
const result = createDesignNetlistExport(project, { format: "spectre" });
const blockers = result.status === "blocked"
  ? result.diagnostics
  : unfinishedDrawingDiagnostics(result.diagnostics);
if (blockers.length) {
  throw new Error(`Analog Canvas export blocked: ${JSON.stringify(blockers.map(
    ({ code, message }) => ({ code, message })))}`);
}
await writeFile(output, result.file.text, "utf8");
