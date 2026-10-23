import { pathToFileURL } from "node:url";
import readline from "node:readline";

const pluginPath = process.argv[2];
if (!pluginPath) {
  throw new Error("JavaScript plugin path is required");
}

const loaded = await import(`${pathToFileURL(pluginPath).href}?t=${Date.now()}`);
let api = loaded.default ?? loaded;
if (typeof api === "function") {
  api = new api();
} else if (api && typeof api.Spider === "function") {
  api = new api.Spider();
} else if (api && api.rule && typeof api.rule === "object") {
  api = api.rule;
}

const aliases = {
  init: ["init"],
  getName: ["getName", "name"],
  homeContent: ["homeContent", "home"],
  categoryContent: ["categoryContent", "category"],
  detailContent: ["detailContent", "detail"],
  searchContent: ["searchContent", "search"],
  playerContent: ["playerContent", "play"],
  action: ["action"],
};

function methodFor(name) {
  for (const candidate of aliases[name] ?? [name]) {
    if (api && typeof api[candidate] === "function") return api[candidate];
  }
  return null;
}

async function invoke(name, args) {
  const method = methodFor(name);
  if (!method) {
    if (name === "getName" && typeof api?.name === "string") {
      return api.name;
    }
    if (name === "init") return null;
    throw new Error(`JavaScript method is unavailable: ${name}`);
  }
  return await method.apply(api, args);
}

const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

for await (const line of input) {
  let request;
  try {
    request = JSON.parse(line);
    const result = await invoke(request.method, request.args ?? []);
    process.stdout.write(JSON.stringify({
      id: request.id,
      ok: true,
      result,
    }) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({
      id: request?.id ?? "",
      ok: false,
      error: {
        code: "javascript_plugin_error",
        message: error instanceof Error ? error.message : String(error),
      },
    }) + "\n");
  }
}
