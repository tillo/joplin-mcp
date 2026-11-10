// Build-time smoke test: act as a 2025-11-25 MCP client against the supergateway
// bridge and require a real tool list back. supergateway's --stateful streamable
// HTTP only opens a session on `initialize`, so an MCP package that refuses that
// handshake (as horizon-mcp 3.x did) leaves the image unusable even
// though it builds and starts. Usage: node smoke.mjs <port>
const url = `http://127.0.0.1:${process.argv[2]}/mcp`;
const PROTOCOL = "2025-11-25";

async function rpc(body, session) {
  const headers = { "content-type": "application/json", accept: "application/json, text/event-stream" };
  if (session) headers["mcp-session-id"] = session;
  const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  const text = await res.text();
  if (!("id" in body)) return { session };
  // A streamable HTTP reply is either one JSON object or an SSE stream of `data:` lines.
  const lines = text.startsWith("{") ? [text] : text.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5));
  const msg = lines.map((l) => JSON.parse(l)).find((m) => m.id === body.id);
  if (!msg) throw new Error(`HTTP ${res.status}, no reply to ${body.method}: ${text.slice(0, 300)}`);
  if (msg.error) throw new Error(`${body.method} rejected: ${JSON.stringify(msg.error)}`);
  return { result: msg.result, session: session ?? res.headers.get("mcp-session-id") };
}

async function smoke() {
  const init = await rpc({
    jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: { name: "image-smoke", version: "0" } },
  });
  await rpc({ jsonrpc: "2.0", method: "notifications/initialized" }, init.session);
  const list = await rpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }, init.session);
  const n = list.result.tools.length;
  if (n === 0) throw new Error("tools/list returned no tools");
  console.log(`smoke OK: ${JSON.stringify(init.result.serverInfo)} protocol ${init.result.protocolVersion}, ${n} tools`);
}

// supergateway takes a moment to listen; retry only while the port is not up yet.
for (let attempt = 1; ; attempt++) {
  try {
    await smoke();
    break;
  } catch (e) {
    const notUp = e.cause?.code === "ECONNREFUSED";
    if (!notUp || attempt >= 30) {
      console.error(`smoke FAILED: ${e.message}`);
      process.exit(1);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}
